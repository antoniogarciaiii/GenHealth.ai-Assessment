"""Server-rendered status pages. Public, so all patient identifiers are masked (HIPAA "minimum necessary");
full records live only in the access-controlled Google Sheet."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from jinja2 import Environment
from markupsafe import Markup

from . import config

env = Environment(autoescape=True)

STATUS_META = {
    "SUCCESS": ("ok", "Recorded"),
    "DUPLICATE": ("muted", "Duplicate"),
    "NEEDS_REVIEW": ("warn", "Needs review"),
    "REJECTED": ("warn", "Rejected"),
    "FAILED": ("bad", "Failed"),
    "RECEIVED": ("info", "Queued"),
    "PROCESSING": ("info", "Processing"),
    "RETRY_SCHEDULED": ("info", "Retrying"),
}


def mask_fields(f):
    if not f:
        return f
    f = dict(f)
    fn, ln = f.get("patient_first_name"), f.get("patient_last_name")
    f["patient_first_name"] = (fn[0] + ".") if fn else None
    f["patient_last_name"] = (ln[:1] + "•••") if ln else None
    dob = f.get("patient_dob")
    f["patient_dob"] = ("••••-••-•• (" + dob[:4] + ")") if dob else None
    return f


def _tz(dt):
    if not dt:
        return "—"
    return dt.astimezone(ZoneInfo(config.DISPLAY_TZ)).strftime("%b %d, %I:%M:%S %p")


def _ago(dt):
    if not dt:
        return "never"
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _dur(ms):
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f}s"


env.filters.update(tz=_tz, ago=_ago, dur=_dur)
env.globals.update(STATUS_META=STATUS_META, mask=mask_fields, cfg=config)

BASE_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171f;--sub:#5b6474;--line:#e4e7ec;--accent:#2f5bea;
--ok:#0f7b45;--ok-bg:#e3f5ec;--warn:#9a5b00;--warn-bg:#fff2d9;--bad:#b42318;--bad-bg:#fde7e5;
--info:#2f5bea;--info-bg:#e6edff;--muted:#5b6474;--muted-bg:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaef;--sub:#9aa3b2;--line:#262b35;
--accent:#7c9bff;--ok:#4ed497;--ok-bg:#12301f;--warn:#f3b14d;--warn-bg:#33270f;--bad:#ff8a80;--bad-bg:#3a1714;
--info:#7c9bff;--info-bg:#18223d;--muted:#9aa3b2;--muted-bg:#222631}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:0 0 10px}.sub{color:var(--sub);font-size:13px}
.banner{border-radius:12px;padding:16px 18px;margin-bottom:18px;display:flex;gap:14px;align-items:center;font-weight:600}
.banner .dot{width:12px;height:12px;border-radius:50%;flex:none}
.banner.ok{background:var(--ok-bg);color:var(--ok)}.banner.ok .dot{background:var(--ok)}
.banner.warn{background:var(--warn-bg);color:var(--warn)}.banner.warn .dot{background:var(--warn)}
.banner.bad{background:var(--bad-bg);color:var(--bad)}.banner.bad .dot{background:var(--bad)}
.banner small{display:block;font-weight:400;opacity:.85}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.k{color:var(--sub);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.v{font-size:24px;font-weight:650;margin-top:2px}
.tablewrap{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:900px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--sub);font-weight:600;background:var(--card);position:sticky;top:0}
tr:last-child td{border-bottom:none}td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.pill.ok{background:var(--ok-bg);color:var(--ok)}.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.bad{background:var(--bad-bg);color:var(--bad)}.pill.info{background:var(--info-bg);color:var(--info)}
.pill.muted{background:var(--muted-bg);color:var(--muted)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.filters a{padding:4px 10px;border:1px solid var(--line);border-radius:999px;color:var(--sub);background:var(--card);font-size:12.5px}
.filters a.on{border-color:var(--accent);color:var(--accent)}
.err{color:var(--bad);font-size:12.5px;max-width:280px}
dl{display:grid;grid-template-columns:200px 1fr;gap:8px 16px;margin:0}dt{color:var(--sub)}dd{margin:0}
.events{list-style:none;padding:0;margin:0}.events li{padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:14px}
.events li:last-child{border-bottom:none}.events time{color:var(--sub);flex:none;width:150px;font-size:12.5px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:800px){.two{grid-template-columns:1fr}dl{grid-template-columns:130px 1fr}}
footer{margin-top:22px;color:var(--sub);font-size:12px}
"""

DASHBOARD = env.from_string("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="20">
<title>DME Intake Status</title><style>{{ css }}</style></head><body><div class="wrap">
<header><div><h1>DME Order Intake · Status</h1>
<div class="sub">Live pipeline health · auto-refreshes every 20s · times in {{ cfg.DISPLAY_TZ }}</div></div>
<div class="sub">Last successful order: <b>{{ last_ok|ago }}</b></div></header>

{% if health == 'ok' %}<div class="banner ok"><span class="dot"></span><div>All systems operational
<small>No failed runs in the last 24 hours.</small></div></div>
{% elif health == 'warn' %}<div class="banner warn"><span class="dot"></span><div>Operational — documents need human review
<small>{{ stats.review }} document(s) in the last 24h were incomplete or unreadable. The pipeline itself is healthy.</small></div></div>
{% else %}<div class="banner bad"><span class="dot"></span><div>Attention: pipeline failures detected
<small>{{ stats.failed }} failed run(s) in the last 24h{% if stuck %}, {{ stuck }} run(s) stuck in queue{% endif %}. Last failure {{ last_fail|ago }}. On-call has been emailed.</small></div></div>{% endif %}

<div class="grid">
<div class="card"><div class="k">Runs (24h)</div><div class="v">{{ stats.total }}</div></div>
<div class="card"><div class="k">Recorded</div><div class="v" style="color:var(--ok)">{{ stats.success }}</div></div>
<div class="card"><div class="k">Duplicates blocked</div><div class="v">{{ stats.duplicate }}</div></div>
<div class="card"><div class="k">Needs review</div><div class="v" style="color:var(--warn)">{{ stats.review }}</div></div>
<div class="card"><div class="k">Failed</div><div class="v" style="color:var(--bad)">{{ stats.failed }}</div></div>
<div class="card"><div class="k">In flight</div><div class="v">{{ stats.in_flight }}</div></div>
<div class="card"><div class="k">Median time</div><div class="v">{{ stats.p50_ms|dur }}</div></div>
</div>

<h2>Recent runs</h2>
<div class="filters">
<a href="/" class="{{ 'on' if not status_filter }}">All</a>
{% for s in ['SUCCESS','DUPLICATE','NEEDS_REVIEW','REJECTED','FAILED','RETRY_SCHEDULED'] %}
<a href="/?status={{ s }}" class="{{ 'on' if status_filter and status_filter.upper()==s }}">{{ STATUS_META[s][1] }}</a>{% endfor %}
</div>
<div class="tablewrap"><table><thead><tr>
<th>Received</th><th>Status</th><th>Channel</th><th>File</th><th>Patient</th><th>Provider</th>
<th>Equipment</th><th>Date of service</th><th>Time</th><th>Order / detail</th></tr></thead><tbody>
{% for r in runs %}{% set m = STATUS_META.get(r.status, ('muted', r.status)) %}{% set f = mask(r.extracted) or {} %}
<tr><td>{{ r.created_at|tz }}</td>
<td><span class="pill {{ m[0] }}">{{ m[1] }}</span>{% if r.attempts > 1 %}<div class="sub">{{ r.attempts }} attempts</div>{% endif %}</td>
<td>{{ r.channel }}</td><td class="mono">{{ (r.filename or '—')[:40] }}</td>
<td>{{ f.patient_first_name or '' }} {{ f.patient_last_name or '—' }}</td>
<td>{{ f.ordering_provider or '—' }}</td><td>{{ f.equipment_requested or '—' }}</td>
<td>{{ f.date_of_service or '—' }}</td><td>{{ r.duration_ms|dur }}</td>
<td><a href="/runs/{{ r.id }}">{{ r.order_id or r.id }}</a>
{% if r.error and r.status != 'SUCCESS' %}<div class="err">{{ r.error[:140] }}</div>{% endif %}
{% if r.status == 'SUCCESS' and f.warnings %}<div><span class="pill warn">{{ f.warnings|length }} flag{{ 's' if f.warnings|length > 1 }}</span></div>{% endif %}
{% if r.duplicate_of %}<div class="sub">dup of <a href="/runs/{{ r.duplicate_of }}">{{ r.duplicate_of }}</a></div>{% endif %}</td></tr>
{% else %}<tr><td colspan="10" class="sub">No runs yet.</td></tr>{% endfor %}
</tbody></table></div>
<footer>Patient identifiers are masked on this public page. Full records are in the access-controlled order sheet.
· <a href="/api/runs">JSON</a> · <a href="/healthz">healthz</a></footer>
</div></body></html>""")

RUN = env.from_string("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{% if r.status in ('RECEIVED','PROCESSING','RETRY_SCHEDULED') %}<meta http-equiv="refresh" content="5">{% endif %}
<title>Run {{ r.id }}</title><style>{{ css }}</style></head><body><div class="wrap">
<header><div><div class="sub"><a href="/">← Status dashboard</a></div><h1>Run <span class="mono">{{ r.id }}</span></h1></div>
<div><span class="pill {{ m[0] }}" style="font-size:14px">{{ m[1] }}</span></div></header>
<div class="two">
<div class="card"><h2>Run</h2><dl>
<dt>Received</dt><dd>{{ r.created_at|tz }}</dd><dt>Channel</dt><dd>{{ r.channel }}</dd>
<dt>File</dt><dd class="mono">{{ r.filename or '—' }}{% if r.file_size %} · {{ (r.file_size/1024)|round(1) }} KB{% endif %}</dd>
<dt>SHA-256</dt><dd class="mono">{{ (r.file_sha256 or '—')[:16] }}…</dd>
<dt>Attempts</dt><dd>{{ r.attempts }}</dd><dt>Duration</dt><dd>{{ r.duration_ms|dur }}</dd>
{% if r.next_attempt_at and r.status=='RETRY_SCHEDULED' %}<dt>Next retry</dt><dd>{{ r.next_attempt_at|tz }}</dd>{% endif %}
{% if r.order_id %}<dt>Order ID</dt><dd class="mono">{{ r.order_id }}</dd>{% endif %}
{% if r.sheet_url and r.status=='SUCCESS' %}<dt>System of record</dt><dd><a href="{{ r.sheet_url }}" target="_blank">Open row in Google Sheet ↗</a></dd>{% endif %}
{% if r.duplicate_of %}<dt>Duplicate of</dt><dd><a href="/runs/{{ r.duplicate_of }}">{{ r.duplicate_of }}</a></dd>{% endif %}
{% if r.error and r.status != 'SUCCESS' %}<dt>Reason</dt><dd class="err" style="max-width:none">{{ r.error }}</dd>{% endif %}
</dl></div>
<div class="card"><h2>Extracted fields <span class="sub">(identifiers masked)</span></h2>
{% if f %}<dl>
<dt>Patient first name</dt><dd>{{ f.patient_first_name or '—' }}</dd>
<dt>Patient last name</dt><dd>{{ f.patient_last_name or '—' }}</dd>
<dt>Date of birth</dt><dd>{{ f.patient_dob or '—' }}</dd>
<dt>Ordering provider</dt><dd>{{ f.ordering_provider or '—' }}{% if f.provider_npi %} · NPI {{ f.provider_npi }}{% endif %}</dd>
<dt>Equipment</dt><dd>{{ f.equipment_requested or '—' }}{% if f.hcpcs_codes %} <span class="mono">({{ f.hcpcs_codes|join(', ') }})</span>{% endif %}</dd>
<dt>Date of service</dt><dd>{{ f.date_of_service or '—' }}{% if f.date_of_service_basis and f.date_of_service_basis != 'explicit_dos' %} <span class="sub">(from {{ f.date_of_service_basis|replace('_',' ') }})</span>{% endif %}</dd>
<dt>Signature present</dt><dd>{{ 'Yes' if f.physician_signature_present else 'No / unclear' }}</dd>
{% if f.warnings %}<dt>Review flags</dt><dd><ul style="margin:0;padding-left:18px">{% for w in f.warnings %}<li style="color:var(--warn)">{{ w }}</li>{% endfor %}</ul></dd>{% endif %}
{% if f.notes %}<dt>Notes</dt><dd>{{ f.notes }}</dd>{% endif %}
</dl>{% else %}<div class="sub">No extraction for this run.</div>{% endif %}</div>
</div>
<div class="card" style="margin-top:12px"><h2>Event log</h2><ul class="events">
{% for e in events %}<li><time>{{ e.t }}</time><span>{{ e.msg }}</span></li>{% endfor %}</ul></div>
{% if r.status in ('FAILED','NEEDS_REVIEW') %}<div class="card" style="margin-top:12px"><h2>Replay</h2>
<div class="sub">After fixing the cause, operators can re-run this document:</div>
<pre class="mono">curl -X POST {{ cfg.PUBLIC_BASE_URL }}/runs/{{ r.id }}/replay -H "X-API-Key: $INGEST_API_KEY"</pre></div>{% endif %}
</div></body></html>""")


def render_dashboard(*, runs, stats, last_ok, last_fail, stuck, status_filter):
    health = "bad" if (stats["failed"] or stuck) else ("warn" if stats["review"] else "ok")
    return DASHBOARD.render(css=Markup(BASE_CSS), runs=runs, stats=stats, last_ok=last_ok, last_fail=last_fail,
                            stuck=stuck, status_filter=status_filter, health=health)


def render_run(r):
    events = r.get("events") or []
    for e in events:
        try:
            e["t"] = _tz(datetime.fromisoformat(e["t"]))
        except Exception:
            pass
    return RUN.render(css=Markup(BASE_CSS), r=r, m=STATUS_META.get(r["status"], ("muted", r["status"])),
                      f=mask_fields(r.get("extracted")), events=events)
