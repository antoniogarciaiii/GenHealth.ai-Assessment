"""Document intelligence: PDF -> structured DME order fields via Claude (native PDF input)."""
import base64
import json
import re
from datetime import date, datetime

import anthropic

from . import config
from .retry import with_backoff

REQUIRED_FIELDS = [
    "patient_first_name",
    "patient_last_name",
    "patient_dob",
    "ordering_provider",
    "equipment_requested",
    "date_of_service",
]

SYSTEM_PROMPT = """You are a medical document data-extraction engine for a Durable Medical Equipment (DME) supplier.
You receive one inbound document (often a faxed/scanned order or prescription) and return ONE JSON object.

Rules:
- Extract only what is explicitly present in the document. Never guess or invent values.
- If a field is missing or illegible, use null (never an empty string, never a placeholder).
- Dates must be ISO format YYYY-MM-DD. If a date is ambiguous or illegible, use null.
- "ordering_provider" is the practitioner who prescribed/signed the order (the "Prescriber"/"Ordering Physician", name + credentials, e.g. "Jane Doe, MD"). If a different "Referring Physician" also appears, do NOT use them as ordering provider; mention them in warnings only if roles are unclear. Not the patient, not the DME supplier.
- "equipment_requested" is a concise description of the item(s) ordered (include HCPCS codes in hcpcs_codes if present).
- "date_of_service": priority order -> (1) an explicitly labeled "Date of Service"/"DOS"; (2) the clinical visit/encounter date that supports the order; (3) the prescriber's signature/order date. Set date_of_service_basis accordingly ("explicit_dos" | "visit_date" | "order_date"). Never output an impossible calendar date (e.g. Feb 30) - skip that source and use the next one.
- "warnings": list every data-quality problem a human should know about, e.g. impossible dates (quote them as written, e.g. "Signature date '2/30/24' is not a valid date"), conflicting values for the same field (e.g. two different visit dates), implausible ages, missing signature, illegible fields, multiple patients in one document. Empty list if none.
- "is_dme_order": true only if the document is actually an order/prescription for medical equipment or supplies.
- Output ONLY the raw JSON object. No markdown, no code fences, no commentary.

JSON shape:
{
  "is_dme_order": true | false,
  "patient_first_name": string | null,
  "patient_last_name": string | null,
  "patient_dob": "YYYY-MM-DD" | null,
  "ordering_provider": string | null,
  "provider_npi": string | null,
  "equipment_requested": string | null,
  "hcpcs_codes": [string],
  "date_of_service": "YYYY-MM-DD" | null,
  "date_of_service_basis": "explicit_dos" | "visit_date" | "order_date" | null,
  "physician_signature_present": true | false,
  "warnings": [string],
  "notes": string | null
}"""


class ExtractionError(Exception):
    """The document itself could not be interpreted (not retryable -> NEEDS_REVIEW)."""


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # SDK retries disabled: our own with_backoff() owns retry policy so it is explicit + logged.
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=0, timeout=120)
    return _client


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError,
                        anthropic.RateLimitError, anthropic.InternalServerError)):
        return True
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code in (408, 409, 429, 500, 502, 503, 504, 529):
        return True
    return False


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ExtractionError("Model did not return JSON")
    return json.loads(m.group(0))


def _call_claude(pdf_bytes: bytes) -> dict:
    resp = _get_client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": "Extract the DME order fields from this document as JSON."},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_json(text)


def _fake_extract(pdf_bytes: bytes) -> dict:
    """Deterministic stand-in used only by the local smoke test (EXTRACTOR=fake)."""
    body = pdf_bytes.decode("latin-1")
    if "MALFORMED" in body:
        return {"is_dme_order": False}
    m = re.search(r"PATIENT:(\w+)\s+(\w+)", body)
    first, last = (m.group(1), m.group(2)) if m else ("John", "Doe")
    return {"is_dme_order": True, "patient_first_name": first, "patient_last_name": last,
            "patient_dob": "1975-05-14", "ordering_provider": "Jane Smith, MD", "provider_npi": None,
            "equipment_requested": "CPAP machine", "hcpcs_codes": ["E0601"], "date_of_service": "2026-09-30",
            "date_of_service_basis": "explicit_dos", "physician_signature_present": True,
            "warnings": ["Signature date '2/30/24' is not a valid date"] if "WARN" in body else [], "notes": None}


def extract(pdf_bytes: bytes, log) -> dict:
    if config.EXTRACTOR == "fake":
        return _fake_extract(pdf_bytes)
    try:
        return with_backoff(lambda: _call_claude(pdf_bytes), is_transient=_is_transient,
                            label="claude.extract", log=log)
    except anthropic.BadRequestError as e:
        # e.g. corrupt/encrypted PDF the model cannot read -> human review, not a retry loop
        raise ExtractionError(f"LLM rejected document: {e.message}") from e
    except json.JSONDecodeError as e:
        raise ExtractionError(f"LLM returned invalid JSON: {e}") from e


# ---------- normalization & validation ----------

def _norm_name(v):
    if not v or not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", v).strip()
    return v.title() if (v.isupper() or v.islower()) else v


def _norm_date(v):
    if not v or not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            d = datetime.strptime(v.strip(), fmt).date()
            if date(1900, 1, 1) <= d <= date(2100, 1, 1):
                return d.isoformat()
        except ValueError:
            continue
    return None


def normalize(raw: dict) -> tuple[dict, list[str]]:
    """Returns (clean_fields, problems). Any problem => NEEDS_REVIEW instead of a record."""
    f = {
        "patient_first_name": _norm_name(raw.get("patient_first_name")),
        "patient_last_name": _norm_name(raw.get("patient_last_name")),
        "patient_dob": _norm_date(raw.get("patient_dob")),
        "ordering_provider": (raw.get("ordering_provider") or "").strip() or None,
        "provider_npi": (str(raw.get("provider_npi")).strip() if raw.get("provider_npi") else None),
        "equipment_requested": (raw.get("equipment_requested") or "").strip() or None,
        "hcpcs_codes": [str(c).strip().upper() for c in (raw.get("hcpcs_codes") or []) if c],
        "date_of_service": _norm_date(raw.get("date_of_service")),
        "date_of_service_basis": raw.get("date_of_service_basis"),
        "physician_signature_present": bool(raw.get("physician_signature_present")),
        "warnings": [str(w) for w in (raw.get("warnings") or []) if w],
        "notes": raw.get("notes"),
    }
    # code-side sanity checks (don't trust the model alone)
    for k in ("patient_dob", "date_of_service"):
        if raw.get(k) and not f[k]:
            f["warnings"].append(f"{k} value '{raw.get(k)}' is not a valid date")
    if f["patient_dob"]:
        age = (date.today() - date.fromisoformat(f["patient_dob"])).days // 365
        if age > 110:
            f["warnings"].append(f"Implausible patient age ({age}) from DOB {f['patient_dob']}")
        if f["date_of_service"] and f["date_of_service"] < f["patient_dob"]:
            f["warnings"].append("Date of service is before date of birth")
    if raw.get("is_dme_order") is not False and not f["physician_signature_present"]:
        f["warnings"].append("No prescriber signature detected")
    problems = []
    if raw.get("is_dme_order") is False:
        problems.append("Document does not appear to be a DME order")
    for k in REQUIRED_FIELDS:
        if not f.get(k):
            problems.append(f"Missing/unreadable field: {k}")
    return f, problems
