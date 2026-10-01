"""Google Sheets = downstream system of record (what operations / fulfillment works from)."""
import re
import threading

import requests

from . import config
from .retry import with_backoff

HEADERS = [
    "Order ID", "Received At", "Channel", "Patient First Name", "Patient Last Name", "Patient DOB",
    "Ordering Provider", "Provider NPI", "Equipment Requested", "HCPCS Codes", "Date of Service",
    "DOS Basis", "Review Flags", "Source File", "Pipeline Run Link",
]

_lock = threading.Lock()
_ws = None


_gid = None


def _use_apps_script() -> bool:
    return bool(config.SHEETS_WEBHOOK_URL and config.SHEETS_WEBHOOK_SECRET)


def enabled() -> bool:
    return bool(config.GOOGLE_SHEET_ID and (_use_apps_script() or config.google_service_account_info()))


class SheetsHTTPError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, SheetsHTTPError):
        return exc.status in (408, 429) or exc.status >= 500
    import gspread
    if isinstance(exc, gspread.exceptions.APIError):
        code = getattr(exc.response, "status_code", 0)
        return code in (408, 429) or code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _worksheet():
    global _ws
    with _lock:
        if _ws is not None:
            return _ws
        import gspread
        gc = gspread.service_account_from_dict(config.google_service_account_info())
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet(config.SHEET_TAB)
        except gspread.WorksheetNotFound:
            first = sh.sheet1
            if not any(first.get_all_values()):
                first.update_title(config.SHEET_TAB)  # reuse the blank default "Sheet1"
                ws = first
            else:
                ws = sh.add_worksheet(config.SHEET_TAB, rows=1000, cols=len(HEADERS))
        if ws.row_values(1) != HEADERS:
            ws.update([HEADERS], "A1")
            ws.format("A1:O1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        _ws = ws
        return ws


def row_url(row: int | None) -> str:
    base = f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}/edit"
    gid = _gid if _gid is not None else (_ws.id if _ws is not None else None)
    if not row or gid is None:
        return base
    return f"{base}#gid={gid}&range=A{row}:O{row}"


def _append_via_apps_script(values: list) -> int:
    global _gid
    r = requests.post(config.SHEETS_WEBHOOK_URL, json={"secret": config.SHEETS_WEBHOOK_SECRET,
                                                       "values": ["" if v is None else str(v) for v in values]},
                      timeout=45, allow_redirects=True)
    if r.status_code != 200:
        raise SheetsHTTPError(r.status_code, r.text[:200])
    try:
        data = r.json()
    except ValueError:
        raise SheetsHTTPError(502, "Apps Script returned non-JSON (check deployment access = Anyone)")
    if not data.get("ok"):
        raise RuntimeError(f"Apps Script writer error: {data.get('error')}")
    _gid = data.get("gid")
    return int(data["row"])


def append_order(values: list, log) -> int:
    """Appends one order row; returns the sheet row number."""
    def _do():
        if _use_apps_script():
            return _append_via_apps_script(values)
        ws = _worksheet()
        resp = ws.append_row(values, value_input_option="RAW", insert_data_option="INSERT_ROWS",
                             table_range="A1")
        rng = resp.get("updates", {}).get("updatedRange", "")
        m = re.search(r"![A-Z]+(\d+)", rng)
        return int(m.group(1)) if m else None

    return with_backoff(_do, is_transient=_is_transient, label="sheets.append", log=log)
