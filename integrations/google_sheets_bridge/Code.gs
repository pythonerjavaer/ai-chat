/**
 * FROSTFIRE_CHATGPT_BRIDGE Google Sheets transport.
 *
 * ChatGPT Scheduled Monitors write columns A:E only. Future Radar remains the
 * authority for validation, filtering, deduplication and persistence.
 */
const BRIDGE_HEADERS = [
  'bridge_id', 'generated_at', 'source_thread_id', 'monitor_name',
  'payload_json', 'processing_status', 'processed_at', 'result_json', 'error',
];
const BRIDGE_SHEET_NAME = 'inbox';
const HANDLER_NAME = 'processPendingRows';
const MAX_ERROR_LENGTH = 500;

const RETRYABLE_HTTP_CODES = [402, 408, 429, 500, 502, 503, 504];
const STALE_PROCESSING_MINUTES = 15;
const DEFAULT_BACKOFF_MINUTES = 5;
const QUOTA_BACKOFF_MINUTES = 360;
const MAX_BACKOFF_MINUTES = 360;

function now_() {
  return new Date();
}

function errorState_(cellValue) {
  const raw = String(cellValue || '').trim();
  if (!raw) return {attempts: 0};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') return parsed;
  } catch (error) {}
  const match = raw.match(/attempts=(\d+)/i);
  return {attempts: match ? Number(match[1]) : 0, message: raw};
}

function retryDelayMinutes_(attempts, reason, httpCode) {
  if (httpCode === 402 || /quota|402/i.test(String(reason || ''))) {
    return QUOTA_BACKOFF_MINUTES;
  }
  const exponent = Math.max(0, Math.min(Number(attempts || 0), 6));
  return Math.min(MAX_BACKOFF_MINUTES, DEFAULT_BACKOFF_MINUTES * Math.pow(2, exponent));
}

function retryState_(reason, attempts, httpCode) {
  const delay = retryDelayMinutes_(attempts, reason, httpCode);
  const next = new Date(now_().getTime() + delay * 60 * 1000);
  return {
    classification: 'RETRYABLE',
    reason: String(reason || 'temporary_failure').slice(0, MAX_ERROR_LENGTH),
    http_code: httpCode || null,
    attempts: Number(attempts || 0) + 1,
    next_retry_at: next.toISOString(),
  };
}

function permanentState_(reason, attempts, httpCode) {
  return {
    classification: 'FAILED_PERMANENT',
    reason: String(reason || 'permanent_failure').slice(0, MAX_ERROR_LENGTH),
    http_code: httpCode || null,
    attempts: Number(attempts || 0) + 1,
  };
}

function shouldAttempt_(status, processedAt, errorCell) {
  const normalized = String(status || '').trim().toUpperCase();
  if (!normalized || normalized === 'PENDING') return true;
  if (normalized === 'RETRYABLE' || normalized === 'ERROR') {
    const state = errorState_(errorCell);
    if (!state.next_retry_at) return true;
    return new Date(state.next_retry_at).getTime() <= now_().getTime();
  }
  if (normalized === 'PROCESSING') {
    const startedAt = processedAt instanceof Date ? processedAt : new Date(processedAt);
    if (!startedAt || isNaN(startedAt.getTime())) return true;
    return now_().getTime() - startedAt.getTime() > STALE_PROCESSING_MINUTES * 60 * 1000;
  }
  return false;
}

function classifyHttpFailure_(code, body, attempts) {
  const message = 'HTTP ' + code + ': ' + String(body || '').slice(0, MAX_ERROR_LENGTH);
  if (RETRYABLE_HTTP_CODES.indexOf(code) !== -1) {
    return {status: 'RETRYABLE', state: retryState_(message, attempts, code)};
  }
  return {status: 'FAILED_PERMANENT', state: permanentState_(message, attempts, code)};
}

function classifyException_(error, attempts) {
  const message = String(error || 'unknown_error').slice(0, MAX_ERROR_LENGTH);
  if (/json|syntax/i.test(message)) {
    return {status: 'FAILED_PERMANENT', state: permanentState_(message, attempts, null)};
  }
  return {status: 'RETRYABLE', state: retryState_(message, attempts, null)};
}

function bridgeProperties_() {
  const props = PropertiesService.getScriptProperties();
  const url = (props.getProperty('FUTURE_RADAR_SYNC_URL') || '').trim();
  const token = (props.getProperty('FUTURE_RADAR_SYNC_TOKEN') || '').trim();
  if (!url || !token) {
    throw new Error('Set FUTURE_RADAR_SYNC_URL and FUTURE_RADAR_SYNC_TOKEN in Script Properties.');
  }
  return {url: url, token: token};
}

function inboxSheet_() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(BRIDGE_SHEET_NAME);
  if (!sheet) throw new Error('Missing sheet: ' + BRIDGE_SHEET_NAME);
  return sheet;
}

function ensureHeaders_(sheet) {
  const values = sheet.getRange(1, 1, 1, BRIDGE_HEADERS.length).getValues()[0];
  const matches = BRIDGE_HEADERS.every(function(header, index) {
    return String(values[index] || '').trim() === header;
  });
  if (!matches) sheet.getRange(1, 1, 1, BRIDGE_HEADERS.length).setValues([BRIDGE_HEADERS]);
}

function setupBridge() {
  const workbook = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = workbook.getSheetByName(BRIDGE_SHEET_NAME);
  if (!sheet) sheet = workbook.insertSheet(BRIDGE_SHEET_NAME);
  ensureHeaders_(sheet);
  sheet.setFrozenRows(1);

  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === HANDLER_NAME) ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger(HANDLER_NAME).timeBased().everyMinutes(5).create();
}

function processPendingRows() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    const config = bridgeProperties_();
    const sheet = inboxSheet_();
    ensureHeaders_(sheet);
    const lastRow = sheet.getLastRow();
    if (lastRow < 2) return;
    const range = sheet.getRange(2, 1, lastRow - 1, BRIDGE_HEADERS.length);
    const rows = range.getValues();
    rows.forEach(function(row, offset) {
      const status = String(row[5] || '').trim().toUpperCase();
      const bridgeId = String(row[0] || '').trim();
      const payloadJson = String(row[4] || '').trim();
      if (!bridgeId || !payloadJson) return;
      if (!shouldAttempt_(status, row[6], row[8])) return;
      const rowNumber = offset + 2;
      const previousState = errorState_(row[8]);
      const attempts = Number(previousState.attempts || 0);
      sheet.getRange(rowNumber, 6, 1, 4).setValues([[
        'PROCESSING', now_(), '', '',
      ]]);
      try {
        JSON.parse(payloadJson);
        const response = UrlFetchApp.fetch(config.url, {
          method: 'post',
          contentType: 'application/json',
          headers: {
            Authorization: 'Bearer ' + config.token,
            'Idempotency-Key': bridgeId,
          },
          payload: payloadJson,
          muteHttpExceptions: true,
        });
        const code = response.getResponseCode();
        const body = response.getContentText();
        if (code >= 200 && code < 300) {
          sheet.getRange(rowNumber, 6, 1, 4).setValues([[
            'PROCESSED', now_(), body, '',
          ]]);
        } else {
          const classified = classifyHttpFailure_(code, body, attempts);
          sheet.getRange(rowNumber, 6, 1, 4).setValues([[
            classified.status, now_(), '', JSON.stringify(classified.state),
          ]]);
        }
      } catch (error) {
        const classified = classifyException_(error, attempts);
        sheet.getRange(rowNumber, 6, 1, 4).setValues([[
          classified.status, now_(), '', JSON.stringify(classified.state),
        ]]);
      }
    });
  } finally {
    lock.releaseLock();
  }
}

function testConnection() {
  const config = bridgeProperties_();
  const payload = {
    version: 'FROSTFIRE_SYNC_V1',
    batch_id: 'google-sheets-bridge-test-' + new Date().getTime(),
    source_id: 'google-sheets-bridge-test',
    monitor_name: 'Google Sheets Bridge connection test',
    jobs: [],
    programs: [],
    articles: [],
  };
  const response = UrlFetchApp.fetch(config.url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + config.token,
      'Idempotency-Key': payload.batch_id,
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  if (code < 200 || code >= 300) throw new Error('HTTP ' + code + ': connection test failed.');
  return JSON.parse(response.getContentText());
}
