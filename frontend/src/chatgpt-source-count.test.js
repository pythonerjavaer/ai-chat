import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("the bridge defaults to six sources and prefers the server's configured count", () => {
  assert.match(appSource, /CHATGPT_MONITOR_SOURCE_COUNT = 6;/);
  const start = appSource.indexOf("function renderRecruitmentSyncStatus(");
  const end = appSource.indexOf("\nfunction radarCollection", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const renderer = appSource.slice(start, end);
  assert.match(renderer, /\["expected_source_count", "expected_sources", "configured_source_count"\]/);
  assert.match(renderer, /expectedRaw > 0 \? expectedRaw : CHATGPT_MONITOR_SOURCE_COUNT/);
  assert.match(renderer, /connected >= expected/);
  assert.match(renderer, /`\$\{expected\} 个 ChatGPT 监控源`/);
  for (let index = 1; index <= 6; index += 1) {
    assert.ok(stylesSource.includes(`.recruitment-sync-orbit i:nth-child(${index})`));
  }
});
