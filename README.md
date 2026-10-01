# DME Order Intake Automation

Inbound DME order documents (PDF) are turned into structured, deduplicated records in Google Sheets, with team notifications, failure alerts and a public status dashboard.

- **Inbound webhook:** `POST https://<host>/ingest` (header `X-API-Key`)
- **Email channel:** send a PDF attachment to the intake Gmail inbox. A Zapier Zap forwards it to the same webhook.
- **Status dashboard:** `https://<host>/` (public; patient identifiers are masked)
- **System of record:** Google Sheet, tab `Orders` (read-only link shared separately)

## How to trigger it

```bash
# multipart upload (recommended)
curl -X POST https://<host>/ingest -H "X-API-Key: <key>" -F "file=@order.pdf"

# also accepted: raw body, JSON with a URL, or JSON with base64
curl -X POST https://<host>/ingest -H "X-API-Key: <key>" -H "Content-Type: application/pdf" --data-binary @order.pdf
curl -X POST https://<host>/ingest -H "X-API-Key: <key>" -H "Content-Type: application/json" \
     -d '{"file_url":"https://.../order.pdf"}'
```

Responses:
- `202 {"run_id", "status":"RECEIVED", "status_url"}`: accepted and queued.
- `200 {"status":"DUPLICATE","duplicate_of"}`: the identical file was already processed.
- `401`: missing or invalid API key.
- `415` / `413` / `422`: the input isn't a PDF, is too large, or is empty. It is still logged as `REJECTED` and alerted.

Processing usually takes a few seconds. Follow `status_url` to watch the run.

## Architecture

```
 Webhook (curl, any system) ─────────────┐
                                         ▼
 Gmail ─► Zapier (filter: .pdf) ─► POST /ingest ─► validate (auth, %PDF, size) ─► REJECTED + alert
                                         │
                                 sha256(file) already seen? ──yes──► DUPLICATE (no LLM spend)
                                         │ no
                                 runs table (status RECEIVED) = durable queue in Postgres
                                         │  worker threads claim with FOR UPDATE SKIP LOCKED
                                         ▼
                        Claude (native PDF input) ─► JSON ─► normalize + validate
                                         │                        └─ missing fields / not an order ─► NEEDS_REVIEW + alert
                        content key (name+DOB+DOS+equipment) seen? ──yes──► DUPLICATE
                                         │ no
                        Google Sheet row ─► SUCCESS ─► Gmail notification (deep link to row)
          any unexpected error ─► run-level retry w/ backoff (30s, 60s, 120s) ─► FAILED + alert email
```

**Components**

| Piece | Tool | Why |
|---|---|---|
| Webhook, queue, extraction, idempotency, retries, dashboard | Python (FastAPI) on Railway | This is the core reliability logic. Code makes it testable, versioned and portable, and keeps the retry policy explicit instead of hidden in a vendor's UI. |
| Run log, idempotency keys, durable queue | Postgres (Railway) | Unique constraints make deduplication atomic even when two copies arrive at the same moment. The queue survives restarts and redeploys. |
| Email intake channel | Zapier (Gmail → Webhooks by Zapier) | The Gmail login (OAuth) and inbox polling are commodity glue. Zapier does that well, and it hands off to the same `/ingest` endpoint, so every channel shares one pipeline. |
| Document understanding | Claude (`claude-sonnet-5-5`, configurable) | Claude reads PDFs natively (text and scanned images), so no separate OCR step is needed. A strict JSON contract is followed by validation in code. |
| System of record | Google Sheets | The business-facing record. Easy to share read-only. |
| Notifications and alerts | Gmail SMTP | New-order notices go to the team. Failure and review alerts go to on-call. |

## Reliability design

- **Idempotency in two layers.**
  - (1) A SHA-256 hash of the file bytes is stored in `documents` (primary key). A re-sent file is marked `DUPLICATE` before any LLM call, which also saves cost. If the earlier run of that file `FAILED`, a resend counts as a retry instead.
  - (2) A content key built from the patient name, DOB, date of service and equipment is unique in `orders`. This catches the same order arriving as a re-scan, a re-fax or through another channel.
  - Both checks are enforced by database constraints, not by "check then insert" logic, so they hold up under concurrent requests.
- **Retries with backoff at two levels.**
  - Call level: every external call (Claude, Sheets, SMTP) retries transient errors (429, 5xx, timeouts) up to 4 times with exponential backoff and jitter.
  - Run level: if a run still fails, it is rescheduled in Postgres (30s → 60s → 120s, `MAX_RUN_ATTEMPTS=4`) before it becomes `FAILED`. Because the schedule lives in Postgres, it survives restarts.
- **Partial-failure safety.** A replayed run reuses its existing order row and only redoes the steps that didn't finish. It never writes a second Sheet row.
- **Crash recovery.** Runs left `PROCESSING` by a restart or redeploy are re-queued at boot, and a janitor thread checks for them every minute.
- **Monitoring and alerting.**
  - `FAILED` runs (system problems) email the alert list. `NEEDS_REVIEW` and `REJECTED` (document problems) email a review request.
  - The dashboard banner turns red on any failure or stuck run in the last 24 hours.
  - `/healthz` checks the database for an external uptime monitor.
- **Manual replay.** `POST /runs/{id}/replay` re-runs a `FAILED` or `NEEDS_REVIEW` document. The PDF is kept so it never has to be re-sent.
- **Bad documents never become bad records.** Missing required fields, or a document that isn't a DME order, goes to `NEEDS_REVIEW` with the reason. The LLM returns `null` for missing values (no guessing), and dates are validated and normalized in code.

## Security and patient data (PHI)

- All secrets are environment variables in Railway. None are in the repo (see `.env.example`).
- The webhook requires an `X-API-Key` header, compared in constant time. Only PDFs are accepted (checked by their `%PDF` header), with a 10 MB limit.
- The public dashboard and API mask patient identifiers: first initial, last-name initial, and DOB year only. Notification emails contain initials only and link to the access-controlled Sheet.
- PDFs are stored in Postgres so failed runs can be replayed. In production they would go to encrypted object storage with a retention policy, and all vendors would be under a BAA.

## Status meanings

| Status | Meaning | Action |
|---|---|---|
| Recorded (`SUCCESS`) | Order written to the Sheet; team notified | None |
| Duplicate | Same file or same order already processed | None |
| Needs review | The document is readable but incomplete, or isn't a DME order | Fix the document or contact the sender, then replay |
| Rejected | The input wasn't a usable PDF | Ask the sender to resend |
| Retrying | A temporary error occurred; a retry is scheduled | Usually none |
| Failed | All retries were used up (system problem) | See RUNBOOK.md |

## What I'd do with more time

- Move PDFs to encrypted object storage (S3 or GCS) and keep only references in Postgres. Add a retention policy and audit logging.
- Accept HMAC-signed webhooks per client (rotating secrets) instead of one shared API key, plus per-client rate limits.
- Use per-field confidence scores and send low-confidence extractions to a human-review queue with a side-by-side UI.
- Build an evaluation set of real orders (scans, handwriting, multi-page documents) to measure accuracy whenever the prompt or model changes.
- Add multi-tenant config: per-client Sheets or CRMs, field mappings and notification lists, so a new client is a config row instead of a code change.
- Add a dead-letter view on the dashboard with one-click replay (behind login).
- Move the email channel off Zapier to a direct Gmail API push or an inbound-parse service, to cut a vendor dependency (see the caveat below).

## What breaks first at 100x volume

1. **The Google Sheets API.** Per-minute write quotas, and a Sheet isn't a database. Fix: Postgres or a CRM becomes the record, and Sheets becomes a batched mirror or report.
2. **LLM throughput and cost.** Rate limits (429s) and spend scale with volume. Fix: concurrency limits per provider, cheaper models first (escalating to the bigger model on low confidence), and the Batch API for anything that isn't urgent.
3. **Processing inside the web server.** Worker threads share the API process. Fix: separate worker service(s). The Postgres `SKIP LOCKED` queue already supports multiple workers, or it can move to SQS or Redis.
4. **PDF bytes in Postgres.** The database grows quickly. Fix: object storage (see above).
5. **Zapier task cost** on the email channel.

## Known caveat

The email channel runs on a Zapier Professional **trial** that ends in about a week. After that, the Webhooks by Zapier step stops and only the direct webhook keeps working. A production setup would use a paid plan or replace that step with a direct Gmail API integration.

## Running locally

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://... INGEST_API_KEY=dev EXTRACTOR=fake   # fake = no LLM, for plumbing tests
uvicorn app.main:app --reload
```

## Project layout

```
app/main.py      HTTP routes: /ingest, /, /runs/{id}, /runs/{id}/replay, /api/runs, /healthz
app/pipeline.py  intake + idempotency, durable queue workers, processing state machine
app/extract.py   Claude extraction prompt, normalization, validation
app/sheets.py    Google Sheets writer (auto-creates headers)
app/notify.py    Gmail notifications and alerts
app/retry.py     exponential backoff + jitter
app/views.py     dashboard + run detail pages (PHI masking)
RUNBOOK.md       on-call guide
```
