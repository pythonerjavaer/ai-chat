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
      if (status && status !== 'PENDING') return;
      const bridgeId = String(row[0] || '').trim();
      const payloadJson = String(row[4] || '').trim();
      if (!bridgeId || !payloadJson) return;
      const rowNumber = offset + 2;
      sheet.getRange(rowNumber, 6).setValue('PROCESSING');
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
            'PROCESSED', new Date(), body, '',
          ]]);
        } else {
          sheet.getRange(rowNumber, 6, 1, 4).setValues([[
            'ERROR', '', '', 'HTTP ' + code + ': ' + body.slice(0, MAX_ERROR_LENGTH),
          ]]);
        }
      } catch (error) {
        sheet.getRange(rowNumber, 6, 1, 4).setValues([[
          'ERROR', '', '', String(error).slice(0, MAX_ERROR_LENGTH),
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
