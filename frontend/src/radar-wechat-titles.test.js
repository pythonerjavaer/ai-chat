import test from "node:test";
import assert from "node:assert/strict";
import { initWechatTitleRadar, parseWechatImportUrls, wechatTitleQuery, wechatImportSummary } from "./radar-wechat-titles.js";

class Element {
  constructor(tag, className = "", value = "") { this.tag = tag; this.className = className; this._text = value; this.children = []; this.listeners = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  dispatch(name) { return this.listeners[name]?.({ preventDefault() {} }); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(" "); }
  set textContent(value) { this._text = value; this.children = []; }
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const make = (tag, cls, value) => new Element(tag, cls, value);
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const root = make("div"); const requests = [];
  const controller = initWechatTitleRadar({ root, make, formatTime: (time, fallback = "—") => time || fallback,
    api: (url, options = {}) => new Promise((resolve, reject) => requests.push({url, ...options, resolve, reject})),
  });
  return { root, requests, controller, nodes: () => descendants(root), field: label => descendants(root).find(node => node["aria-label"] === label), button: label => descendants(root).find(node => node.tag === "button" && node.textContent === label) };
}
async function open(f, items = []) {
  const pending = f.controller.open();
  f.requests.at(-2).resolve({ items: [{ source_name: "国聘", seed_url: "https://mp.weixin.qq.com/s/seed", enabled: true, total_articles: 1, new_articles: 1 }], discovery_status: "unavailable", provider_state: { provider: "sogou_wechat", status: "unavailable", last_counts: {}, failure_reason: "尚未在当前部署环境探测。" } });
  f.requests.at(-1).resolve({items, total: items.length, page: 1, page_size: 30});
  await pending;
}

test("manual title imports reject unrelated and unsafe URLs before any network request", () => {
  const url = "https://mp.weixin.qq.com/s/public-example";
  assert.deepEqual(parseWechatImportUrls(`${url}\n${url}`), [url]);
  for (const invalid of ["", "http://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com.evil.test/s/a", "https://example.com", "https://name:secret@mp.weixin.qq.com/s/a", "file:///etc/passwd", "http://127.0.0.1"]) assert.throws(() => parseWechatImportUrls(invalid));
  assert.throws(() => parseWechatImportUrls(Array(51).fill(url).join("\n")), /50/);
});

test("title filters preserve exact source and date fields without mixing opportunity filters", () => {
  const query = new URLSearchParams(wechatTitleQuery({sourceName: "国资小新", relevance: "possible", page: 2, fromDate: "2026-09-01", toDate: "2026-09-21"}).split("?")[1]);
  assert.equal(query.get("source_name"), "国资小新"); assert.equal(query.get("relevance_status"), "possible"); assert.equal(query.get("page"), "2"); assert.equal(query.get("from_date"), "2026-09-01");
  assert.equal(query.has("verification_status"), false);
});

test("opening the title tab only reads saved metadata and cannot trigger crawling", async () => {
  const f = fixture(); assert.equal(f.requests.length, 0);
  await open(f, [{title: "2027校园招聘", source_name: null, source_name_detection: null, url: "https://mp.weixin.qq.com/s/a", published_at: null, discovered_at: "2026-09-21", relevance_status: "relevant", relevance_score: 95, fetch_status: "success", matched_keywords: ["校园招聘"]}]);
  assert.deepEqual(f.requests.map(r => r.url), ["/sources/wechat", "/sources/wechat/articles?page=1&page_size=30"]);
  assert.ok(f.requests.every(r => !r.method));
  assert.match(f.root.textContent, /公众号自动发现 · Beta/);
  assert.match(f.root.textContent, /公众号名称未知/);
  assert.match(f.root.textContent, /发布时间 未知/);
  assert.match(f.root.textContent, /待官网核验/);
  assert.ok(f.button("立即扫描"));
});

test("manual discovery trigger uses one explicit endpoint and renders provider outcome", async () => {
  const f = fixture(); await open(f);
  const pending = f.button("立即扫描").dispatch("click");
  const request = f.requests.at(-1);
  assert.equal(request.url, "/sources/wechat/discover");
  assert.equal(request.method, "POST");
  request.resolve({status: "unavailable", counts: {discovered: 0, new: 0, duplicate: 0, related: 0}});
  await tick();
  f.requests.at(-2).resolve({items: [], provider_state: {status: "unavailable", last_counts: {discovered: 0}, failure_reason: "公开搜索访问受限。"}});
  f.requests.at(-1).resolve({items: [], total: 0, page: 1, page_size: 30});
  await pending;
  assert.match(f.root.textContent, /公开搜索访问受限/);
  assert.match(f.root.textContent, /不使用 Cookie/);
});

test("batch import reports each failure, uses configured provenance only by choice, and never forces a refresh by default", async () => {
  const f = fixture(); await open(f);
  f.field("公开文章链接").value = "https://mp.weixin.qq.com/s/a\nhttps://mp.weixin.qq.com/s/b";
  f.field("公开文章链接").dispatch("input");
  const form = f.nodes().find(node => node.tag === "form"); const pending = form.dispatch("submit");
  const request = f.requests.at(-1);
  assert.equal(request.url, "/sources/wechat/articles/import"); assert.equal(request.method, "POST");
  assert.deepEqual(JSON.parse(request.body), {urls: ["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b"], force_refresh: false});
  request.resolve({total: 2, success: 1, new: 1, duplicate: 0, failed: 1, items: [{url: "https://mp.weixin.qq.com/s/a", title: "招聘", fetch_status: "success", is_new: true}, {url: "https://mp.weixin.qq.com/s/b", fetch_status: "http_403"}]});
  await tick(); f.requests.at(-2).resolve({items: []}); f.requests.at(-1).resolve({items: [], total: 0}); await pending;
  assert.match(f.root.textContent, /本批 2 条：成功 1，新增 1，重复 0，失败 1/);
  assert.match(f.root.textContent, /访问受限（403）/);
  assert.ok(f.nodes().some(n => n.tag === "details" && n.open === true));
});

test("read errors are not presented as zero article results and account reset discards late responses", async () => {
  const f = fixture(); const pending = f.controller.open(); f.requests[0].resolve({items: []}); f.requests[1].reject(new Error("server timeout")); await pending;
  assert.match(f.root.textContent, /标题记录读取失败/); assert.doesNotMatch(f.root.textContent, /当前筛选下尚无已保存标题/);
  const late = f.controller.open(); f.controller.reset(); f.requests.at(-2).resolve({items: []}); f.requests.at(-1).resolve({items: [{title: "private old result"}], total: 1}); await late;
  assert.equal(f.root.children.length, 0);
});

test("unknown import counters stay unknown rather than fabricated zeroes", () => {
  assert.match(wechatImportSummary({total: 2}), /成功 —，新增 —，重复 —，失败 —/);
});

test("one-click watchlist import uses configured seeds without pasting URLs", async () => {
  const f = fixture(); await open(f);
  const button = f.button("一键导入 1 个历史入口");
  assert.ok(button);
  const pending = button.dispatch("click");
  const request = f.requests.at(-1);
  assert.equal(request.url, "/sources/wechat/articles/import-watchlist");
  assert.equal(request.method, "POST");
  assert.equal(request.body, undefined);
  request.resolve({
    total: 1, success: 1, new: 1, duplicate: 0, failed: 0,
    items: [{url: "https://mp.weixin.qq.com/s/seed", title: "招聘", fetch_status: "success", is_new: true}],
  });
  await tick();
  f.requests.at(-2).resolve({items: [{source_name: "国聘", seed_url: "https://mp.weixin.qq.com/s/seed", enabled: true}]});
  f.requests.at(-1).resolve({items: [], total: 0, page: 1, page_size: 30});
  await pending;
  assert.match(f.root.textContent, /本批 1 条：成功 1，新增 1，重复 0，失败 0/);
  assert.match(f.root.textContent, /不会自动发现该公众号之后发布的新文章/);
});
