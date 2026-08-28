import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");

test("Future Radar manual scan uses the signed-in session without an admin token", () => {
  const start = appSource.indexOf("async function runFutureRadarNow()");
  const end = appSource.indexOf("\nfunction renderRecruitmentProfile", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const functionSource = appSource.slice(start, end);

  assert.match(functionSource, /api\("\/future-radar\/run"/);
  assert.doesNotMatch(functionSource, /adminUsageToken|X-Admin-Token|管理员操作/);
});

test("the shared API client attaches the JWT and preserves cooldown metadata", () => {
  const start = appSource.indexOf("async function api(");
  const end = appSource.indexOf("\nfunction showToast", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const apiSource = appSource.slice(start, end);

  assert.match(apiSource, /headers\.set\("Authorization", `Bearer \$\{state\.token\}`\)/);
  assert.match(apiSource, /requestError\.status = response\.status/);
  assert.match(apiSource, /requestError\.retryAfter = response\.headers\.get\("Retry-After"\)/);
});

test("manual scan exposes loading, persistent result, and cooldown feedback", () => {
  const start = appSource.indexOf("async function runFutureRadarNow()");
  const end = appSource.indexOf("\nfunction renderRecruitmentProfile", start);
  const functionSource = appSource.slice(start, end);

  assert.match(functionSource, /setFutureRadarLoading\(true, ""\)/);
  assert.match(functionSource, /setFutureRadarActionStatus/);
  assert.match(functionSource, /futureRadarRunSuccessCopy/);
  assert.match(functionSource, /futureRadarRunErrorCopy/);
  assert.match(functionSource, /startFutureRadarRunCooldown/);
});
