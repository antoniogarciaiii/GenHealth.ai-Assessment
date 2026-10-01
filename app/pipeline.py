"""Core pipeline: intake (idempotent) -> durable queue -> extract -> validate -> dedupe -> record -> notify."""
import hashlib
import json
import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from . import config, db, extract, notify, sheets

log = logging.getLogger("pipeline")
wake = threading.Event()   # set on new work so workers don't wait for the poll interval

TERMINAL = ("SUCCESS", "DUPLICATE", "NEEDS_REVIEW", "REJECTED", "FAILED")


def _now():
    return datetime.now(timezone.utc)


def _event(run_id: str, msg: str):
    """Append a timestamped line to the run's event log (visible on the run detail page)."""
    log.info("[%s] %s", run_id, msg)
    with db.conn() as c:
        c.execute("UPDATE runs SET events = events || %s::jsonb, updated_at = now() WHERE id = %s",
                  (json.dumps([{"t": _now().isoformat(), "msg": msg[:1000]}]), run_id))


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- intake

def reject(channel: str, filename: str | None, reason: str, size: int = 0) -> dict:
    """Malformed input that can't even be queued (not a PDF, empty, too big). Logged + alerted."""
    run_id = _new_run_id()
    with db.conn() as c:
        c.execute("""INSERT INTO runs (id, channel, filename, file_size, status, error, finished_at, duration_ms)
                     VALUES (%s,%s,%s,%s,'REJECTED',%s, now(), 0)""",
                  (run_id, channel, filename, size, reason))
    _event(run_id, f"Rejected at intake: {reason}")
    run = {"id": run_id, "filename": filename, "channel": channel, "attempts": 0}
    _safe_alert(run, "REJECTED", reason)
    return {"run_id": run_id, "status": "REJECTED", "reason": reason}


def submit(pdf: bytes, filename: str | None, channel: str) -> dict:
    """Idempotent intake. Same bytes => DUPLICATE (no LLM spend) unless the prior run FAILED,
    in which case the resend is treated as a retry."""
    sha = hashlib.sha256(pdf).hexdigest()
    run_id = _new_run_id()
    with db.conn() as c:
        c.execute("""INSERT INTO runs (id, channel, filename, file_sha256, file_size, pdf, status, next_attempt_at)
                     VALUES (%s,%s,%s,%s,%s,%s,'RECEIVED', now())""",
                  (run_id, channel, filename, sha, len(pdf), pdf))
        claimed = c.execute("INSERT INTO documents (file_sha256, run_id) VALUES (%s,%s) "
                            "ON CONFLICT (file_sha256) DO NOTHING RETURNING run_id", (sha, run_id)).fetchone()
        duplicate_of = None
        if not claimed:
            prior = c.execute("""SELECT d.run_id, r.status FROM documents d JOIN runs r ON r.id = d.run_id
                                 WHERE d.file_sha256 = %s FOR UPDATE OF d""", (sha,)).fetchone()
            if prior and prior["status"] == "FAILED":
                c.execute("UPDATE documents SET run_id = %s WHERE file_sha256 = %s", (run_id, sha))
            else:
                duplicate_of = prior["run_id"] if prior else None
                c.execute("""UPDATE runs SET status='DUPLICATE', duplicate_of=%s, pdf=NULL, finished_at=now(),
                             duration_ms=0, next_attempt_at=NULL WHERE id=%s""", (duplicate_of, run_id))
    if duplicate_of:
        _event(run_id, f"Duplicate file (sha256 {sha[:12]}…) of {duplicate_of}; skipped, no LLM call")
        return {"run_id": run_id, "status": "DUPLICATE", "duplicate_of": duplicate_of,
                "status_url": f"{config.PUBLIC_BASE_URL}/runs/{run_id}"}
    _event(run_id, f"Received via {channel}: {filename} ({len(pdf)} bytes, sha256 {sha[:12]}…)")
    wake.set()
    return {"run_id": run_id, "status": "RECEIVED", "status_url": f"{config.PUBLIC_BASE_URL}/runs/{run_id}"}


# ---------------------------------------------------------------- processing

def _content_key(f: dict) -> str:
    # Prefer structured HCPCS codes over free-text equipment so LLM wording drift can't defeat dedupe
    equip = ",".join(sorted(f.get("hcpcs_codes") or [])) or f["equipment_requested"]
    parts = [f["patient_first_name"], f["patient_last_name"], f["patient_dob"], f["date_of_service"], equip]
    norm = "|".join(" ".join(str(p).lower().split()) for p in parts)
    return hashlib.sha256(norm.encode()).hexdigest()


def _finish(run_id: str, status: str, **cols):
    sets = ", ".join(f"{k} = %s" for k in cols)
    vals = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in cols.values()]
    with db.conn() as c:
        c.execute(f"""UPDATE runs SET status=%s, finished_at=now(), next_attempt_at=NULL, updated_at=now(),
                      duration_ms = (EXTRACT(EPOCH FROM (now() - COALESCE(started_at, created_at))) * 1000)::int
                      {', ' + sets if sets else ''} WHERE id=%s""", [status, *vals, run_id])


def _safe_alert(run: dict, status: str, reason: str):
    try:
        notify.needs_attention(run, status, reason, log=lambda m: _event(run["id"], m))
    except Exception as e:  # alerting must never crash the worker
        _event(run["id"], f"ALERT EMAIL FAILED: {e}")


def process(run: dict):
    run_id = run["id"]
    elog = lambda m: _event(run_id, m)
    try:
        elog(f"Processing attempt {run['attempts']}/{config.MAX_RUN_ATTEMPTS}")

        # 1. Extract (skip on replay if we already have a good extraction)
        raw = run.get("extracted") if run.get("extracted") and run.get("order_id") else None
        if raw is None:
            raw = extract.extract(bytes(run["pdf"]), log=elog)
        fields, problems = extract.normalize(raw)
        with db.conn() as c:
            c.execute("UPDATE runs SET extracted=%s WHERE id=%s", (Jsonb(fields), run_id))

        # 2. Validate -> human review rather than a bad record
        if problems:
            reason = "; ".join(problems)
            _finish(run_id, "NEEDS_REVIEW", error=reason)
            elog(f"Needs review: {reason}")
            _safe_alert(run, "NEEDS_REVIEW", reason)
            return

        # 3. Content-level idempotency (same order, different file/channel)
        key = _content_key(fields)
        order_id = "ORD-" + _now().strftime("%Y%m%d") + "-" + key[:6].upper()
        with db.conn() as c:
            row = c.execute("""INSERT INTO orders (order_id, content_key, run_id, fields) VALUES (%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING RETURNING order_id""",
                            (order_id, key, run_id, Jsonb(fields))).fetchone()
            existing = None if row else c.execute(
                "SELECT order_id, run_id, sheet_row FROM orders WHERE content_key=%s OR run_id=%s",
                (key, run_id)).fetchone()
        if existing and existing["run_id"] != run_id:
            _finish(run_id, "DUPLICATE", duplicate_of=existing["run_id"], order_id=existing["order_id"])
            elog(f"Duplicate order content of {existing['run_id']} ({existing['order_id']}); no new record")
            return
        if existing:  # replay of our own partially-completed run
            order_id = existing["order_id"]

        # 4. Write to system of record (skip if a previous attempt already did)
        sheet_row = existing["sheet_row"] if existing else None
        if sheets.enabled() and not sheet_row:
            received = run["created_at"].astimezone(ZoneInfo(config.DISPLAY_TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")
            sheet_row = sheets.append_order([
                order_id, received, run["channel"], fields["patient_first_name"], fields["patient_last_name"],
                fields["patient_dob"], fields["ordering_provider"], fields.get("provider_npi") or "",
                fields["equipment_requested"], ", ".join(fields.get("hcpcs_codes") or []),
                fields["date_of_service"], fields.get("date_of_service_basis") or "",
                "; ".join(fields.get("warnings") or []), run.get("filename") or "",
                f"{config.PUBLIC_BASE_URL}/runs/{run_id}",
            ], log=elog)
            with db.conn() as c:
                c.execute("UPDATE orders SET sheet_row=%s WHERE order_id=%s", (sheet_row, order_id))
            elog(f"Recorded in Google Sheet row {sheet_row}")
        link = sheets.row_url(sheet_row) if sheets.enabled() else f"{config.PUBLIC_BASE_URL}/runs/{run_id}"

        warn = fields.get("warnings") or []
        _finish(run_id, "SUCCESS", order_id=order_id, sheet_url=link,
                error=("Review flags: " + "; ".join(warn)) if warn else None)
        elog(f"SUCCESS: {order_id}" + (f" with {len(warn)} review flag(s)" if warn else ""))

        # 5. Notify (a notification failure doesn't undo a recorded order; it is logged + visible)
        try:
            notify.new_order(run, fields, order_id, link, log=elog)
            elog("Team notification sent")
        except Exception as e:
            elog(f"WARNING: team notification failed after retries: {e}")
            with db.conn() as c:
                c.execute("UPDATE runs SET error=%s WHERE id=%s", (f"Notification failed: {e}", run_id))

    except extract.ExtractionError as e:
        _finish(run_id, "NEEDS_REVIEW", error=str(e))
        elog(f"Needs review: {e}")
        _safe_alert(run, "NEEDS_REVIEW", str(e))
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        if run["attempts"] < config.MAX_RUN_ATTEMPTS:
            delay = config.RUN_RETRY_BASE_SECONDS * (2 ** (run["attempts"] - 1))
            with db.conn() as c:
                c.execute("""UPDATE runs SET status='RETRY_SCHEDULED', error=%s,
                             next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s""",
                          (f"{type(e).__name__}: {e}", delay, run_id))
            elog(f"Attempt failed ({type(e).__name__}: {e}); run-level retry in {delay}s")
        else:
            _finish(run_id, "FAILED", error=f"{type(e).__name__}: {e}")
            elog(f"FAILED after {run['attempts']} attempts: {e}\n{tb}")
            _safe_alert(run, "FAILED", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- durable queue workers

def _claim() -> dict | None:
    """Postgres-backed queue: FOR UPDATE SKIP LOCKED lets N workers (or N replicas) share work safely."""
    with db.conn() as c:
        return c.execute("""
            UPDATE runs SET status='PROCESSING', attempts = attempts + 1, started_at = COALESCE(started_at, now()),
                            updated_at = now()
            WHERE id = (SELECT id FROM runs WHERE status IN ('RECEIVED','RETRY_SCHEDULED')
                          AND next_attempt_at <= now()
                        ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""").fetchone()


def _recover_stale():
    """Runs stuck in PROCESSING (process crashed / redeployed mid-run) go back on the queue."""
    with db.conn() as c:
        rows = c.execute("""UPDATE runs SET status='RETRY_SCHEDULED', next_attempt_at=now()
                            WHERE status='PROCESSING' AND updated_at < now() - make_interval(mins => %s)
                            RETURNING id""", (config.STALE_PROCESSING_MINUTES,)).fetchall()
    for r in rows:
        _event(r["id"], "Recovered stale PROCESSING run (worker restart); re-queued")


def worker_loop(n: int):
    while True:
        try:
            run = _claim()
            if run:
                process(run)
                continue
        except Exception:
            log.exception("worker %s loop error", n)
            time.sleep(5)
        wake.wait(timeout=5)
        wake.clear()


def janitor_loop():
    while True:
        try:
            _recover_stale()
        except Exception:
            log.exception("janitor error")
        time.sleep(60)


def start_workers():
    with db.conn() as c:  # on boot, anything mid-flight belonged to the previous process
        c.execute("UPDATE runs SET status='RETRY_SCHEDULED', next_attempt_at=now() WHERE status='PROCESSING'")
    for i in range(config.WORKER_THREADS):
        threading.Thread(target=worker_loop, args=(i,), daemon=True, name=f"worker-{i}").start()
    threading.Thread(target=janitor_loop, daemon=True, name="janitor").start()


def replay(run_id: str) -> dict:
    with db.conn() as c:
        r = c.execute("""UPDATE runs SET status='RETRY_SCHEDULED', next_attempt_at=now(), attempts=0,
                         finished_at=NULL, error=NULL
                         WHERE id=%s AND status IN ('FAILED','NEEDS_REVIEW') AND pdf IS NOT NULL
                         RETURNING id""", (run_id,)).fetchone()
    if not r:
        return {"ok": False, "reason": "Run not found, not in FAILED/NEEDS_REVIEW, or has no stored file"}
    _event(run_id, "Manual replay requested")
    wake.set()
    return {"ok": True, "run_id": run_id, "status": "RETRY_SCHEDULED"}
