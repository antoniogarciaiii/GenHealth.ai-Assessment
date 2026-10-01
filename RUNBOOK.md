# On-Call Runbook: DME Order Intake

**Dashboard:** `https://<host>/`  **Health:** `https://<host>/healthz`  **Logs:** Railway → service → Deployments → Logs

## Alert: "Run Failed"

A run used up all of its retries. This is a system problem, not a document problem.

1. Open the run link in the email and read the **Event log**. The last error names the dependency that failed.
2. Find the cause in the table below and fix it.

   | Error contains | Likely cause | Fix |
   |---|---|---|
   | `AuthenticationError` / 401 from Anthropic | API key revoked or out of credit | Update `ANTHROPIC_API_KEY` or add credit at console.anthropic.com |
   | `RateLimitError` / 529 `overloaded` | Anthropic is busy | Usually clears up by itself, so just replay. If it persists, set `ANTHROPIC_MODEL` to a fallback model |
   | `APIError` 403 from Sheets | The Sheet isn't shared with the service account | Share the Sheet with the `client_email` as an Editor |
   | `APIError` 429 from Sheets | Google write quota | Wait about a minute, then replay |
   | `SMTPAuthenticationError` | Gmail app password revoked | Create a new app password and update `GMAIL_APP_PASSWORD` |
   | database / connection errors | Postgres is down | Check Railway → Postgres service status |

3. Replay the run. It reuses the stored PDF and won't create a duplicate row:
   `curl -X POST https://<host>/runs/<run_id>/replay -H "X-API-Key: $INGEST_API_KEY"`
4. Confirm the run shows **Recorded** on the dashboard.

## Alert: "Document Review"

The pipeline is healthy, but the document is incomplete, unreadable or isn't a DME order.
1. Open the run and read the **Reason** (for example `Missing/unreadable field: patient_dob`).
2. Contact the ordering office for a complete document. When it arrives, the new file is processed normally.
3. If the extraction was wrong and the document is fine, replay it once. If it still fails, enter the order in the Sheet by hand and note the run ID.

## Dashboard shows runs stuck in "Queued" or "Retrying" for over 15 minutes

- Check Railway → service is **Active** and `/healthz` returns `ok`.
- Redeploy or restart the service. On boot, stuck runs are re-queued automatically.

## The email channel stopped working

- Check the Zap history in Zapier (Zap: *Gmail → DME Intake webhook*).
- If the Zapier trial or plan has ended, the direct webhook still works. Senders can use it, or you can upgrade Zapier.

## Rotating the webhook key

Update `INGEST_API_KEY` in Railway, update the header in the Zapier webhook step, then tell any API senders.
