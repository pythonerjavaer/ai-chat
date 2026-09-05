import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
test("the bridge defaults to seven sources and uses the server count for its title, orbit and progress", () => {
  const start = appSource.indexOf("function renderRecruitmentSyncStatus(");
  const end = appSource.indexOf("\nfunction radarCollection", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const renderer = appSource.slice(start, end);
  const nodes = new Map();
  const orbit = { children: [], replaceChildren(...children) { this.children = children; } };
  nodes.set(".recruitment-sync-orbit", orbit);
  const panel = { dataset: {}, querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {});
    return nodes.get(selector);
  } };
  const context = { state: {}, ensureRecruitmentSyncPanel: () => panel,
    makeElement: () => ({ style: { setProperty() {} } }) };
  vm.createContext(context);
  vm.runInContext([
    appSource.match(/const CHATGPT_MONITOR_SOURCE_COUNT = \d+;/)[0],
    appSource.slice(appSource.indexOf("function valueAtPaths("), appSource.indexOf("function chatgptSyncFromJobs(")),
    renderer,
  ].join("\n"), context);
  vm.runInContext("renderRecruitmentSyncStatus(null)", context);
  assert.equal(nodes.get("[data-sync-title]").textContent, "7 个 ChatGPT 监控源");
  assert.equal(orbit.children.length, 7);
  vm.runInContext('renderRecruitmentSyncStatus({ expected_source_count: 9, connected_source_count: 8, status: "synced", last_synced_at: "2026-09-05T00:00:00Z" })', context);
  assert.equal(nodes.get("[data-sync-title]").textContent, "9 个 ChatGPT 监控源");
  assert.equal(nodes.get(".recruitment-sync-footer b").textContent, "8 / 9 源已回传");
  assert.equal(panel.dataset.state, "partial");
  assert.equal(orbit.children.length, 9);
  vm.runInContext('renderRecruitmentSyncStatus({ configured_source_count: 7, active_source_count: 7, status: "synced", last_synced_at: "2026-09-05T00:00:00Z" })', context);
  assert.equal(nodes.get(".recruitment-sync-footer b").textContent, "7 / 7 已同步");
  assert.equal(panel.dataset.state, "synced");
  assert.equal(orbit.children.length, 7);
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
