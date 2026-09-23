# Google Sheets Bridge

This directory is a zero-cost transport for ChatGPT Pro Scheduled Monitor
results. ChatGPT writes structured JSON into a Google Sheet; Apps Script sends
the JSON to Future Radar. Future Radar remains responsible for schema
validation, hard filtering, deduplication, verification, scoring and PostgreSQL
persistence. The sheet is not a business database.

## Create the sheet

1. Create a Google Sheet named `FROSTFIRE_CHATGPT_BRIDGE`.
2. Create a tab named `inbox`.
3. Open **Extensions → Apps Script**, paste `Code.gs`, and save.
4. Run `setupBridge()` once and grant the requested Google permissions. It
   creates the A:I header row and one five-minute time-driven trigger. Running
   it again removes only existing triggers for `processPendingRows` before
   creating one replacement, so triggers do not accumulate.

The columns are:

| Column | Name | Writer |
| --- | --- | --- |
| A | `bridge_id` | ChatGPT |
| B | `generated_at` | ChatGPT |
| C | `source_thread_id` | ChatGPT |
| D | `monitor_name` | ChatGPT |
| E | `payload_json` | ChatGPT |
| F | `processing_status` | Apps Script |
| G | `processed_at` | Apps Script |
| H | `result_json` | Apps Script |
| I | `error` | Apps Script |

ChatGPT should leave F:I untouched. A row can be left blank or set to
`PENDING`; both states are eligible for processing. Apps Script may also use
`PROCESSING`, `PROCESSED`, `RETRYABLE`, and `FAILED_PERMANENT`. Rows are never
deleted. Stale `PROCESSING` rows are retried automatically after the processing
lease window.

## Configure the secret

In Apps Script open **Project Settings → Script Properties** and add:

* `FUTURE_RADAR_SYNC_URL` = `https://frostfire-ai.onrender.com/api/integrations/chatgpt-monitor/sync`
* `FUTURE_RADAR_SYNC_TOKEN` = the Render environment value with the same name

The token is read only from `PropertiesService.getScriptProperties()`. It is
not put in cells, source code, commits, or logs. Do not paste it into this
repository or into ChatGPT messages.

## Test and operate

* Run `testConnection()` from Apps Script to send an empty, harmless
  `FROSTFIRE_SYNC_V1` batch and verify authentication and reachability.
* Run `processPendingRows()` manually for an immediate test; the trigger runs
  it every five minutes afterward.
* `LockService` prevents overlapping runs. Each bridge ID is sent as the
  `Idempotency-Key`, so retries after network errors are safe. Future Radar
  also applies monitor run IDs, external IDs, URLs, fingerprints and database
  constraints.

HTTP 2xx marks a row `PROCESSED` and stores the response. HTTP 402, 408, 429,
500, 502, 503, 504, timeouts and network failures mark the row `RETRYABLE` and
store retry metadata in the error cell, including attempts and `next_retry_at`.
Quota-style 402 responses use a longer backoff; other retryable failures use
bounded exponential backoff. Malformed JSON and permanent client-side failures
mark the row `FAILED_PERMANENT`. Payload JSON is preserved in all cases; rows
are not deleted or silently skipped.
