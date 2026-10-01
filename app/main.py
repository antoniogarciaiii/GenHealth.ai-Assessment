"""HTTP surface: inbound webhook, public status dashboard, run detail, manual replay, health."""
import base64
import hmac
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import UploadFile

from . import config, db, pipeline
from .views import render_dashboard, render_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="DME Order Intake", docs_url="/docs", redoc_url=None)


@app.on_event("startup")
def _startup():
    db.init()
    pipeline.start_workers()


def _require_key(request: Request):
    supplied = request.headers.get("x-api-key") or ""
    if not config.INGEST_API_KEY or not hmac.compare_digest(supplied, config.INGEST_API_KEY):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


def _channel(request: Request) -> str:
    ch = request.query_params.get("channel") or request.headers.get("x-source-channel") or "webhook"
    ch = re.sub(r"[^a-z0-9_-]", "", ch.lower())[:20]
    return ch or "webhook"


async def _read_document(request: Request) -> tuple[bytes, str | None]:
    """Accepts: multipart/form-data (any file field), raw application/pdf body,
    or JSON {"file_url": "..."} / {"file_base64": "...", "filename": "..."}."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form(max_part_size=config.MAX_UPLOAD_BYTES)
        for _, v in form.multi_items():
            if isinstance(v, UploadFile):
                return await v.read(config.MAX_UPLOAD_BYTES + 1), v.filename
        # Zapier sometimes sends a hydrated file URL as a plain form field
        url = form.get("file_url") or form.get("file")
        if isinstance(url, str) and url.startswith("http"):
            return await _download(url), form.get("filename") or url.rsplit("/", 1)[-1][:120]
        raise HTTPException(422, "No file found in multipart form (send it as field 'file')")
    if ctype.startswith("application/json"):
        body = await request.json()
        if body.get("file_base64"):
            return base64.b64decode(body["file_base64"]), body.get("filename")
        if body.get("file_url"):
            return await _download(body["file_url"]), body.get("filename") or body["file_url"].rsplit("/", 1)[-1][:120]
        raise HTTPException(422, "JSON body must include 'file_url' or 'file_base64'")
    data = await request.body()
    return data, request.headers.get("x-filename") or request.query_params.get("filename")


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content[: config.MAX_UPLOAD_BYTES + 1]


@app.post("/ingest", status_code=202)
async def ingest(request: Request):
    _require_key(request)
    channel = _channel(request)
    try:
        data, filename = await _read_document(request)
    except HTTPException as e:
        res = pipeline.reject(channel, None, e.detail)
        return JSONResponse(res, status_code=e.status_code)
    except Exception as e:
        res = pipeline.reject(channel, None, f"Could not read upload: {e}")
        return JSONResponse(res, status_code=400)

    if not data:
        return JSONResponse(pipeline.reject(channel, filename, "Empty file"), status_code=422)
    if len(data) > config.MAX_UPLOAD_BYTES:
        return JSONResponse(pipeline.reject(channel, filename, "File exceeds size limit", len(data)), status_code=413)
    if not data[:1024].lstrip().startswith(b"%PDF"):
        return JSONResponse(pipeline.reject(channel, filename, "Not a PDF (missing %PDF header)", len(data)),
                            status_code=415)

    res = pipeline.submit(data, filename, channel)
    return JSONResponse(res, status_code=200 if res["status"] == "DUPLICATE" else 202)


@app.post("/runs/{run_id}/replay")
def replay(run_id: str, request: Request):
    _require_key(request)
    res = pipeline.replay(run_id)
    return JSONResponse(res, status_code=200 if res["ok"] else 409)


# ---------------------------------------------------------------- read side

RUN_COLS = """id, created_at, channel, filename, file_size, status, attempts, next_attempt_at, started_at,
              finished_at, duration_ms, error, duplicate_of, extracted, order_id, sheet_url"""


@app.get("/", response_class=HTMLResponse)
def dashboard(status: str | None = None):
    with db.conn() as c:
        q = f"SELECT {RUN_COLS} FROM runs"
        args = []
        if status:
            q += " WHERE status = %s"
            args.append(status.upper())
        runs = c.execute(q + " ORDER BY created_at DESC LIMIT 100", args).fetchall()
        stats = c.execute("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status='SUCCESS') AS success,
                   count(*) FILTER (WHERE status='DUPLICATE') AS duplicate,
                   count(*) FILTER (WHERE status IN ('NEEDS_REVIEW','REJECTED')) AS review,
                   count(*) FILTER (WHERE status='FAILED') AS failed,
                   count(*) FILTER (WHERE status IN ('RECEIVED','PROCESSING','RETRY_SCHEDULED')) AS in_flight,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       FILTER (WHERE status='SUCCESS') AS p50_ms
            FROM runs WHERE created_at > now() - interval '24 hours'""").fetchone()
        last_ok = c.execute("SELECT max(finished_at) AS t FROM runs WHERE status='SUCCESS'").fetchone()["t"]
        last_fail = c.execute("SELECT max(finished_at) AS t FROM runs WHERE status='FAILED'").fetchone()["t"]
        stuck = c.execute("""SELECT count(*) AS n FROM runs WHERE status IN ('RECEIVED','RETRY_SCHEDULED','PROCESSING')
                             AND created_at < now() - interval '15 minutes'""").fetchone()["n"]
    return render_dashboard(runs=runs, stats=stats, last_ok=last_ok, last_fail=last_fail, stuck=stuck,
                            status_filter=status)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: str):
    with db.conn() as c:
        run = c.execute(f"SELECT {RUN_COLS}, events, file_sha256 FROM runs WHERE id=%s", (run_id,)).fetchone()
    if not run:
        raise HTTPException(404, "Run not found")
    return render_run(run)


@app.get("/api/runs")
def api_runs(limit: int = 50):
    """Machine-readable run history (PHI-masked) for integrations/monitors."""
    from .views import mask_fields
    with db.conn() as c:
        rows = c.execute(f"SELECT {RUN_COLS} FROM runs ORDER BY created_at DESC LIMIT %s",
                         (min(limit, 500),)).fetchall()
    for r in rows:
        r["extracted"] = mask_fields(r["extracted"])
    return rows


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    from .views import mask_fields
    with db.conn() as c:
        r = c.execute(f"SELECT {RUN_COLS}, events FROM runs WHERE id=%s", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Run not found")
    r["extracted"] = mask_fields(r["extracted"])
    return r


@app.get("/healthz")
def healthz():
    """Liveness + dependency check, suitable for an external uptime monitor."""
    with db.conn() as c:
        c.execute("SELECT 1")
        fail_1h = c.execute("SELECT count(*) AS n FROM runs WHERE status='FAILED' "
                            "AND finished_at > now() - interval '1 hour'").fetchone()["n"]
    return {"ok": True, "db": "ok", "failed_last_hour": fail_1h,
            "time": datetime.now(timezone.utc).isoformat()}
