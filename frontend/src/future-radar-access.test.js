import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const indexSource = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("Future Radar manual scan uses the signed-in session without an admin token", () => {
  const start = appSource.indexOf("async function runFutureRadarNow(");
  const end = appSource.indexOf("\nfunction renderRecruitmentProfile", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const functionSource = appSource.slice(start, end);

  assert.match(functionSource, /api\("\/future-radar\/run"/);
  assert.doesNotMatch(functionSource, /adminUsageToken|X-Admin-Token|管理员操作/);
});

test("the shared API client attaches the JWT and preserves server retry metadata", () => {
  const start = appSource.indexOf("async function api(");
  const end = appSource.indexOf("\nfunction showToast", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const apiSource = appSource.slice(start, end);

  assert.match(apiSource, /headers\.set\("Authorization", `Bearer \$\{state\.token\}`\)/);
  assert.match(apiSource, /requestError\.status = response\.status/);
  assert.match(apiSource, /requestError\.retryAfter = response\.headers\.get\("Retry-After"\)/);
});

test("manual Quick and Deep scans use per-type run state and a short completion debounce", () => {
  const start = appSource.indexOf("async function runFutureRadarNow(");
  const end = appSource.indexOf("\nfunction renderRecruitmentProfile", start);
  const functionSource = appSource.slice(start, end);

  assert.match(indexSource, /id="future-radar-run"[\s\S]*QUICK SCAN/);
  assert.match(indexSource, /id="future-radar-deep-run"[\s\S]*DEEP SCAN/);
  assert.match(appSource, /FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS = 20/);
  assert.doesNotMatch(appSource, /FUTURE_RADAR_MANUAL_COOLDOWN_SECONDS|5 \* 60/);
  assert.match(functionSource, /JSON\.stringify\(\{ scan_type: scanType \}\)/);
  assert.match(functionSource, /runStarting\[scanType\]/);
  assert.match(functionSource, /setFutureRadarLoading\(true, ""\)/);
  assert.match(functionSource, /setFutureRadarActionStatus/);
  assert.match(functionSource, /futureRadarRunSuccessCopy/);
  assert.match(functionSource, /futureRadarRunErrorCopy/);
  assert.match(functionSource, /startFutureRadarRunDelay\(scanType, FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS/);
  assert.match(functionSource, /Number\(error\.status\) === 409/);
});

test("dashboard run locks restore each scan button independently after refresh", () => {
  const start = appSource.indexOf("function renderFutureRadarRunAvailability(");
  const end = appSource.indexOf("\nfunction startFutureRadarRunDelay", start);
  const availabilitySource = appSource.slice(start, end);

  assert.match(availabilitySource, /syncFutureRadarActiveRuns/);
  assert.match(availabilitySource, /activeRunTypes\.has\(scanType\)/);
  assert.match(availabilitySource, /runStarting\[scanType\]/);
});

test("a timed-out scan immediately restores the server lock and polls to terminal state", () => {
  const runStart = appSource.indexOf("async function runFutureRadarNow(");
  const runEnd = appSource.indexOf("\nfunction renderRecruitmentProfile", runStart);
  const functionSource = appSource.slice(runStart, runEnd);
  const pollStart = appSource.indexOf("async function pollFutureRadarRunUntilTerminal(");
  const pollEnd = appSource.indexOf("\nfunction startFutureRadarRunStatusPolling", pollStart);
  const pollSource = appSource.slice(pollStart, pollEnd);

  assert.match(functionSource, /timedOut = \/请求超时/);
  assert.match(functionSource, /startFutureRadarRunStatusPolling\(scanType\)/);
  assert.match(pollSource, /readFutureRadarDashboard\(\)/);
  assert.match(pollSource, /futureRadarActiveRunTypes\(dashboard\)\.includes\(scanType\)/);
  assert.match(pollSource, /scheduleFutureRadarRunStatusPoll\(scanType\)/);
  assert.match(pollSource, /loadFutureRadarSnapshot\(\)/);
  assert.match(pollSource, /startFutureRadarRunDelay\(scanType, FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS/);
});

test("skipped sources downgrade completion feedback to warning", () => {
  const start = appSource.indexOf("function futureRadarRunTone(");
  const end = appSource.indexOf("\nfunction markFutureRadarRunActive", start);
  const toneSource = appSource.slice(start, end);

  assert.match(toneSource, /Number\(run\.sources_skipped \|\| 0\) > 0/);
  assert.match(toneSource, /return "warning"/);
});

test("chat and search discoveries appear in the default unified opportunity pool", () => {
  assert.match(indexSource, /data-radar-tab="jobs"[^>]*>全部机会/);
  assert.doesNotMatch(indexSource, /data-radar-tab="candidates"/);
  assert.match(indexSource, /id="future-radar-opportunity-coverage"/);
  assert.match(appSource, /api\(`\/future-radar\/opportunities\?\$\{query\}`,\s*\{\s*timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS/);
  assert.doesNotMatch(appSource, /api\(`\/future-radar\/jobs\?/);
  const start = appSource.indexOf("function renderRecruitmentJobs(");
  const end = appSource.indexOf("\nasync function addRecruitmentWatchFromJob", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const renderer = appSource.slice(start, end);
  assert.match(renderer, /opportunityStats\.tier_counts/);
  assert.match(renderer, /futureRadarOpportunitySource\(job\)/);
  assert.match(renderer, /createFutureRadarOpportunityDetail\(job\)/);
});

test("unified opportunities refresh without needing a verified public event", () => {
  const start = appSource.indexOf("async function pollFutureRadarEvents(");
  const end = appSource.indexOf("\nfunction stopFutureRadarPolling", start);
  const pollingSource = appSource.slice(start, end);
  assert.match(pollingSource, /api\(`\/future-radar\/opportunities\?\$\{opportunityQuery\}`,\s*\{\s*timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS/);
  assert.match(pollingSource, /state\.futureRadar\.jobsRequestId === jobsRequestId/);
  assert.match(pollingSource, /futureRadarJobsQuery\(\) === opportunityQuery/);
  assert.match(pollingSource, /applyFutureRadarJobsPayload\(opportunityPayload, opportunityQuery\)/);
  assert.ok(pollingSource.indexOf("applyFutureRadarJobsPayload") < pollingSource.indexOf("if (novel.length)"));
});

test("T-tier filters go to the unified backend and detail uses the same pool", () => {
  assert.match(appSource, /futureRadarTierQuery\(state\.recruitmentTierFilter\)/);
  assert.match(appSource, /api\(`\/future-radar\/opportunities\/\$\{encodeURIComponent\(job\.id\)\}`,\s*\{\s*timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS/);
  assert.match(indexSource, /id="future-radar-filter-verification"[^>]*><option value="">全部机会/);
});
