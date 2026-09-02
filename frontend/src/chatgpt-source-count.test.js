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

test("the bridge labels inventory as source signals and never treats skipped or closed as rejected", () => {
  const start = appSource.indexOf("function ensureRecruitmentSyncPanel(");
  const end = appSource.indexOf("\nfunction radarCollection", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const renderer = appSource.slice(start, end);

  assert.match(renderer, /数字是各来源当前保留的信号记录，不是去重后的岗位数/);
  assert.match(renderer, /\["已核验信号", "accepted"/);
  assert.match(renderer, /\["待官网核验信号", "pending"/);
  assert.match(renderer, /\["未通过核验信号", "rejected"/);
  assert.match(renderer, /待核验＝官网证据暂不完整 · 未通过＝明确不符合规则或链接无效/);
  assert.match(renderer, /"transport_state", "status", "state", "bridge_status"/);
  assert.match(renderer, /visualState === "synced" && verificationState === "pending"/);
  assert.match(renderer, /回传完成 · 待核验/);
  assert.match(renderer, /同步完成 · 含未通过信号/);
  assert.doesNotMatch(renderer, /"rejected", "skipped"/);
  assert.doesNotMatch(renderer, /"rejected_count", "skipped_count"/);
  assert.doesNotMatch(renderer, /counts\.rejected", "counts\.skipped/);
});
