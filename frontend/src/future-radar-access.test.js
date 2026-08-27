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
