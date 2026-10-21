# Useful job history, reliable monitor push, and revenue forecasts

## Job history

In an opportunity's expanded details, open **岗位变更与历史查询**. The first
time input asks which business state to inspect; the second asks what the
service knew at a particular point. Leave the second blank for current knowledge.

`GET /api/future-radar/jobs/{id}/history?valid_at=<timezone-aware ISO timestamp>&known_at=<timestamp>`
requires the existing user bearer session. Pagination is bounded at 100 entries.
Only verified snapshots are returned. Opportunity details supply `history_job_id`
so a merged legacy/ATS opportunity resolves to the correct versioned job.

The two axes use half-open UTC intervals `[from,to)`:

- `valid_from` / `valid_to`: the business period covered by an assertion;
- `system_from` / `system_to`: when the service held that assertion.

Ordinary source changes take effect at observation time when no confirmed source
effective timestamp exists. Opening and closing dates remain separate job facts.
An administrator can submit a **confirmed past correction** to
`POST /api/future-radar/jobs/{id}/history/corrections`, authenticated with the
existing `X-Admin-Token`. Provide `valid_from`, finite `valid_to`, `reason`, the
`expected_revision` from the history response, and `changes` containing one or
more supported fields (`title`, `city`, `region`, `opening_date`, `closing_date`,
`status`). A changed revision returns 409; reload before correcting.

Corrections split only the affected business interval and preserve the previous
assertion's system interval. Existing jobs get an explicitly labelled migration
baseline at rollout time; earlier knowledge is not invented. Semantic changes
and versions commit together; unchanged polling does not grow history. The
new tables use the existing private PostgreSQL application schema and indexed
history queries; no public Data API grants are added.

## Signed deliveries from existing controlled monitors

The source importer and the ChatGPT bridges that reuse its `submit_payload`
function now prefer signed push when their existing ingest credential is strong
enough. An explicit `FUTURE_RADAR_WEBHOOK_SECRET` overrides key derivation on
both ends. Keep that setting synchronized if an operator chooses an override.

For example, the existing source import command can add `--transport webhook`
to require push, `--transport rest` for a reconciliation import, or leave the
default `auto` to prefer push and fall back on temporary service unavailability.
The same stable event ID and exact body are reused across retries and fallback.
Invalid signatures and payload conflicts are not silently downgraded to REST.
Concurrent writes for the same source return retryable 503, using the same
distributed source lease as polling. The CLI retries transient failures up to
three times. Acknowledgement follows persistence, and accepted deliveries and
replays are recorded in `radar_webhook_deliveries`.

`GET /api/future-radar/integrations/status` requires a user session and returns
receiver readiness, accepted event/delivery counts and the last receipt time;
it never returns a signing key. This is a working transport for the existing
controlled monitor pipeline. It does not imply a third-party ATS has agreed to
send native webhooks; such an ATS still needs its own sender-side configuration.

## Revenue forecasts that affect the displayed result

Short histories keep a transparent trend reference. At 60 months the service
compares last-month value, last-year same-month value, random forest, AdaBoost
and Bayesian ridge. At 120 months it adds a real four-gate LSTM trained with
backpropagation through time and Adam, implemented with the existing NumPy CPU
runtime instead of loading PyTorch into the free server.

Every model is evaluated at the requested forecast horizon: three-month
forecasts use recursive three-month rolling-origin holdouts. The scaler and
model are fitted only to the training prefix. Future holdout values are never
fed into steps 2..H. MAE/RMSE and per-horizon errors are returned; a complex model
must beat the better transparent baseline by at least 5%. Otherwise the baseline
is displayed. The actual selected forecast is used in the chart and caption;
unavailable prediction intervals are not presented as 95% intervals.

Training uses at most 240 monthly observations, one local training slot and an
eight-entry cache keyed by the complete bounded numeric series and horizon.
Real records and explicitly labelled synthetic demonstrations stay separate.
Missing months appear only as zero-valued trend references, with a visible gap
warning; they cannot satisfy the 60/120-month training gates. Model comparison
waits for explicit monthly records, including confirmed zero-income months. A runtime
test on synthetic data demonstrates implementation, not real-business accuracy.

The macOS source sender uses the standard Certifi CA bundle with hostname and
certificate verification enabled, so a Python environment missing its default
OpenSSL CA path does not silently prevent monitor deliveries.
