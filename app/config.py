"""Central configuration. Every secret comes from environment variables (Railway Variables) —
nothing sensitive is ever committed to the repo."""
import json
import os
import base64


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


DATABASE_URL = _env("DATABASE_URL")

# Inbound auth: callers must send header `X-API-Key: <INGEST_API_KEY>`
INGEST_API_KEY = _env("INGEST_API_KEY")

# LLM
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Google Sheets (system of record)
GOOGLE_SHEET_ID = _env("GOOGLE_SHEET_ID")
SHEET_TAB = _env("SHEET_TAB", "Orders")
# Preferred: Apps Script web app writer (no service-account key needed)
SHEETS_WEBHOOK_URL = _env("SHEETS_WEBHOOK_URL")
SHEETS_WEBHOOK_SECRET = _env("SHEETS_WEBHOOK_SECRET")


def google_service_account_info() -> dict | None:
    """Accepts the service-account JSON either raw or base64-encoded."""
    raw = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


# Email: relayed through the Apps Script web app (MailApp) by default; EMAIL_TRANSPORT=smtp to use Gmail SMTP
EMAIL_TRANSPORT = _env("EMAIL_TRANSPORT", "apps_script")
GMAIL_USER = _env("GMAIL_USER")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL_TO = [e.strip() for e in (_env("NOTIFY_EMAIL_TO", GMAIL_USER or "") or "").split(",") if e.strip()]
ALERT_EMAIL_TO = [e.strip() for e in (_env("ALERT_EMAIL_TO", ",".join(NOTIFY_EMAIL_TO)) or "").split(",") if e.strip()]

# Public URL of this service (used in deep links inside notifications)
PUBLIC_BASE_URL = (_env("PUBLIC_BASE_URL", "http://localhost:8000") or "").rstrip("/")

# Limits / tuning
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
WORKER_THREADS = int(_env("WORKER_THREADS", "3"))
MAX_RUN_ATTEMPTS = int(_env("MAX_RUN_ATTEMPTS", "4"))          # run-level attempts (each has its own call-level retries)
RUN_RETRY_BASE_SECONDS = int(_env("RUN_RETRY_BASE_SECONDS", "30"))  # 30s, 60s, 120s ...
STALE_PROCESSING_MINUTES = int(_env("STALE_PROCESSING_MINUTES", "10"))
DISPLAY_TZ = _env("DISPLAY_TZ", "America/Chicago")

# Test hook: EXTRACTOR=fake bypasses the LLM (used by the local smoke test only)
EXTRACTOR = _env("EXTRACTOR", "anthropic")
