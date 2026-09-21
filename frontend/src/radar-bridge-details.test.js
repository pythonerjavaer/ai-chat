import test from "node:test";
import assert from "node:assert/strict";
import { bridgeDetailQuery, bridgeCandidateCanAdd, initBridgeDetails } from "./radar-bridge-details.js";

class Element {
  constructor(tag, className = "", value = "") { this.tag = tag; this.className = className; this._text = value; this.children = []; this.listeners = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(" "); }
  set textContent(value) { this._text = value; this.children = []; }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const make = (tag, cls, value) => new Element(tag, cls, value);
function fixture() {
  const root = make("div"); const requests = [];
  const controller = initBridgeDetails({root, make, formatTime: (time, fallback = "—") => time || fallback,
    getSyncStatus: () => ({last_synced_at: "2026-09-21", sources: [{ name: "招聘监控", last_seen_at: "2026-09-21", source_screened: 580 }]}),
    api: (url, options = {}) => new Promise((resolve, reject) => requests.push({url, ...options, resolve, reject})),
  });
  return { root, requests, controller, button: label => descendants(root).find(node => node.tag === "button" && node.textContent === label) };
}

test("bridge metric queries filter each inventory status and scope counts to ChatGPT", () => {
  for (const status of ["source_screened", "accepted", "pending", "rejected", "all"]) {
    const query = new URLSearchParams(bridgeDetailQuery(status, 2).split("?")[1]);
    assert.equal(query.get("review_status"), status); assert.equal(query.get("source_scope"), "chatgpt"); assert.equal(query.get("offset"), "50");
  }
  assert.equal(new URLSearchParams(bridgeDetailQuery("needs_review").split("?")[1]).has("source_scope"), false);
  assert.equal(bridgeCandidateCanAdd({ verification_status: "rejected" }), false);
  assert.equal(bridgeCandidateCanAdd({ can_add_to_pool: true }), true);
});

test("sync date and status overview opens source details without triggering external work", async () => {
  const f = fixture(); await f.controller.open("overview");
  assert.equal(f.requests.length, 0); assert.match(f.root.textContent, /招聘监控/); assert.match(f.root.textContent, /已筛选 580/);
});

test("rejected drilldown shows actual reason and zero accepted records stays actionable", async () => {
  const f = fixture(); const pending = f.controller.open("rejected");
  f.requests[0].resolve({items: [{id: "candidate-1", company: "测试公司", title: "数据岗", review_status_label: "未通过", review_reason: "官网未找到对应岗位", verification_status: "rejected", can_add_to_pool: false}], total: 1}); await pending;
  assert.match(f.root.textContent, /官网未找到对应岗位/); assert.equal(f.button("加入机会池"), undefined);
  const empty = f.button("已核验信号").listeners.click(); f.requests[1].resolve({items: [], total: 0}); await empty;
  assert.match(f.root.textContent, /当前没有“已核验信号”记录/); assert.ok(f.button("ChatGPT 已筛选"));
});

test("bridge pagination reaches records beyond the original 200 limit", async () => {
  const f = fixture(); const pending = f.controller.open("source_screened"); f.requests[0].resolve({items: [], total: 580}); await pending;
  for (let page = 2; page <= 6; page++) { const next = f.button("下一页").listeners.click(); const request = f.requests.at(-1); assert.equal(new URLSearchParams(request.url.split("?")[1]).get("offset"), String((page - 1) * 50)); request.resolve({items: [], total: 580}); await next; }
  assert.match(f.root.textContent, /第 6 \/ 12 页 · 580 条信号/);
});

test("a slow previous filter cannot replace the selected filter and failures never claim an empty list", async () => {
  const f = fixture(); const old = f.controller.open("rejected"); const selected = f.controller.open("accepted");
  f.requests[1].reject(new Error("read timeout")); await selected;
  f.requests[0].resolve({items: [{company: "stale-rejected"}], total: 1}); await old;
  assert.match(f.root.textContent, /信号明细读取失败/); assert.doesNotMatch(f.root.textContent, /stale-rejected|当前没有/);
});
