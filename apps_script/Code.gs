/**
 * Google Apps Script web app bound to the Orders Google Sheet.
 * The intake service POSTs {secret, values:[...]} here; this appends one row.
 * Why: avoids long-lived service-account keys (blocked by org policy / Secure-by-Default),
 * the script runs as the Sheet owner, and the shared secret lives in Script Properties.
 *
 * Setup: Extensions > Apps Script > paste this > Project Settings > Script Properties:
 *   SHARED_SECRET = <same value as SHEETS_WEBHOOK_SECRET in Railway>
 * Deploy > New deployment > Web app > Execute as: Me, Who has access: Anyone > copy the /exec URL.
 */
const TAB = 'Orders';
const HEADERS = ['Order ID', 'Received At', 'Channel', 'Patient First Name', 'Patient Last Name', 'Patient DOB',
  'Ordering Provider', 'Provider NPI', 'Equipment Requested', 'HCPCS Codes', 'Date of Service',
  'DOS Basis', 'Review Flags', 'Source File', 'Pipeline Run Link'];

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out({ ok: false, error: 'bad_json' }); }
  const secret = PropertiesService.getScriptProperties().getProperty('SHARED_SECRET');
  if (!secret || body.secret !== secret) return out({ ok: false, error: 'unauthorized' });
  if (!Array.isArray(body.values) || !body.values[0]) return out({ ok: false, error: 'missing_values' });

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);                       // serialize concurrent appends
  try {
    const sh = getSheet_();
    // Sink-level idempotency: an Order ID is written at most once, even if the caller retries.
    const last = sh.getLastRow();
    if (last > 1) {
      const ids = sh.getRange(2, 1, last - 1, 1).getValues().map(r => String(r[0]));
      const i = ids.indexOf(String(body.values[0]));
      if (i >= 0) return out({ ok: true, row: i + 2, gid: sh.getSheetId(), existing: true });
    }
    const row = last + 1;
    sh.getRange(row, 1, 1, body.values.length).setNumberFormat('@').setValues([body.values.map(String)]);
    return out({ ok: true, row: row, gid: sh.getSheetId() });
  } finally {
    lock.releaseLock();
  }
}

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(TAB);
  if (!sh) {
    const first = ss.getSheets()[0];
    if (first.getLastRow() === 0) { first.setName(TAB); sh = first; } else { sh = ss.insertSheet(TAB); }
  }
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
