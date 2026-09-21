import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
test("the bridge defaults to nine sources and uses the server count for its title, orbit and progress", () => {
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
  const dashboardRenders = [];
  const context = { state: { futureRadar: { dashboard: null } }, ensureRecruitmentSyncPanel: () => panel,
    makeElement: () => ({ style: { setProperty() {} } }),
    renderFutureRadarDashboard: (dashboard) => dashboardRenders.push(dashboard) };
  vm.createContext(context);
  vm.runInContext([
    appSource.match(/const CHATGPT_MONITOR_SOURCE_COUNT = \d+;/)[0],
    appSource.slice(appSource.indexOf("function valueAtPaths("), appSource.indexOf("function chatgptSyncFromJobs(")),
    renderer,
  ].join("\n"), context);
  vm.runInContext("renderRecruitmentSyncStatus(null)", context);
  assert.equal(nodes.get("[data-sync-title]").textContent, "9 个 ChatGPT 监控源");
  assert.equal(orbit.children.length, 9);
  vm.runInContext('renderRecruitmentSyncStatus({ expected_source_count: 9, connected_source_count: 8, status: "synced", last_synced_at: "2026-09-05T00:00:00Z" })', context);
  assert.equal(nodes.get("[data-sync-title]").textContent, "9 个 ChatGPT 监控源");
  assert.equal(nodes.get(".recruitment-sync-footer b").textContent, "8 / 9 源已回传");
  assert.equal(panel.dataset.state, "partial");
  assert.equal(orbit.children.length, 9);
  vm.runInContext('renderRecruitmentSyncStatus({ configured_source_count: 7, active_source_count: 7, status: "synced", last_synced_at: "2026-09-05T00:00:00Z" })', context);
  assert.equal(nodes.get(".recruitment-sync-footer b").textContent, "7 / 7 已同步");
  assert.equal(panel.dataset.state, "synced");
  assert.equal(orbit.children.length, 7);
  vm.runInContext('renderRecruitmentSyncStatus({ expected_source_count: 7, connected_source_count: 7, transport_state: "synced", verification_state: "source_screened", last_synced_at: "2026-09-05T00:00:00Z", inventory_source_screened: 493, inventory_accepted: 10, inventory_pending: 3, inventory_rejected: 35 })', context);
  assert.equal(nodes.get('[data-sync-metric="source_screened"] strong').textContent, "493");
  assert.equal(nodes.get('[data-sync-metric="accepted"] strong').textContent, "10");
  assert.equal(nodes.get('[data-sync-metric="pending"] strong').textContent, "3");
  assert.equal(nodes.get('[data-sync-badge]').textContent, "同步完成 · 查看");
  assert.equal(dashboardRenders.length, 4);
  assert.ok(dashboardRenders.every((dashboard) => Object.keys(dashboard).length === 0));
});

test("the bridge labels inventory as source signals and never treats skipped or closed as rejected", () => {
  const start = appSource.indexOf("function ensureRecruitmentSyncPanel(");
  const end = appSource.indexOf("\nfunction radarCollection", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const renderer = appSource.slice(start, end);

  assert.match(renderer, /数字是各来源当前保留的信号记录，不是去重后的岗位数/);
  assert.match(renderer, /\["已核验信号", "accepted"/);
  assert.match(renderer, /\["尚未入池信号", "pending"/);
  assert.match(renderer, /\["未通过核验信号", "rejected"/);
  assert.match(renderer, /ChatGPT 已筛选的机会直接入池；官网核验是独立信息/);
  assert.match(renderer, /"transport_state", "status", "state", "bridge_status"/);
  assert.match(renderer, /visualState === "synced" && verificationState === "pending"/);
  assert.match(renderer, /回传完成 · 部分未入池/);
  assert.match(renderer, /同步完成 · 含未通过信号/);
  assert.doesNotMatch(renderer, /"rejected", "skipped"/);
  assert.doesNotMatch(renderer, /"rejected_count", "skipped_count"/);
  assert.doesNotMatch(renderer, /counts\.rejected", "counts\.skipped/);
});

test("all bridge counts, including zero counts, and the sync status have native drilldown actions", () => {
  const start = appSource.indexOf("function ensureRecruitmentSyncPanel(");
  const end = appSource.indexOf("\nfunction renderRecruitmentSyncStatus", start);
  class Node {
    constructor(tag, className = "", text = "") { this.tag = tag; this.className = className; this.textContent = text; this.children = []; this.dataset = {}; this.listeners = {}; this.style = { setProperty() {} }; }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.children.push(child); }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const opened = [];
  const context = { $: () => null, CHATGPT_MONITOR_SOURCE_COUNT: 9, makeElement: (tag, cls, text) => new Node(tag, cls, text),
    document: {createElement: tag => new Node(tag), querySelector: () => null}, showFutureRadarBridgeDetails: filter => opened.push(filter) };
  vm.createContext(context); vm.runInContext(appSource.slice(start, end), context);
  const panel = vm.runInContext("ensureRecruitmentSyncPanel()", context);
  const walk = node => [node, ...node.children.flatMap(walk)];
  const controls = walk(panel).filter(node => node.dataset.syncMetric || node.dataset.syncBadge);
  assert.equal(controls.length, 6);
  for (const control of controls) { assert.equal(control.tag, "button"); assert.equal(control.type, "button"); assert.notEqual(control.disabled, true); control.listeners.click(); }
  assert.deepEqual(opened, ["overview", "overview", "source_screened", "accepted", "pending", "rejected"]);
});
