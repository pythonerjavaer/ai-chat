import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const start = source.indexOf("async function loadFutureRadarGraph()");
const end = source.indexOf("async function loadFutureRadarTimeseries()", start);
assert.ok(start >= 0 && end > start);

function runtime() {
  function element(tag) {
    return { tag, children: [], attributes: {}, style: {}, listeners: {}, namespaceURI: "http://www.w3.org/2000/svg",
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, listener) { this.listeners[name] = listener; },
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren() { this.children = []; },
    };
  }
  const elements = {
    "future-radar-graph-view": element("div"),
    "future-radar-graph-status": { textContent: "" },
    "future-radar-graph-refresh": { disabled: false },
  };
  const calls = [];
  let resolve, reject;
  const response = new Promise((yes, no) => { resolve = yes; reject = no; });
  const context = vm.createContext({
    $: (id) => elements[id],
    api: (...args) => { calls.push(args); return response; },
    document: { createElementNS: (_namespace, tag) => element(tag) },
  });
  vm.runInContext(source.slice(start, end), context);
  return { elements, calls, resolve, reject, load: context.loadFutureRadarGraph };
}

test("graph gives a cold connection 60 seconds and allows only one pending sync", async () => {
  const r = runtime();
  const pending = r.load();
  await r.load();
  assert.equal(r.calls.length, 1);
  assert.equal(r.calls[0][0], "/future-radar/graph");
  assert.equal(r.calls[0][1].timeoutMs, 60000);
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, true);
  assert.match(r.elements["future-radar-graph-status"].textContent, /60 秒/);
  r.resolve({ status: "synced", nodes: [], postgres_opportunities_considered: 0 });
  await pending;
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, false);
  assert.equal(r.load.loading, false);
});

test("graph restores refresh after a failure or an unconfigured response", async () => {
  for (const failed of [true, false]) {
    const r = runtime();
    const pending = r.load();
    if (failed) r.reject(new Error("connection failed"));
    else r.resolve({ status: "not_configured", message: "请配置 Neo4j" });
    await pending;
    assert.equal(r.elements["future-radar-graph-refresh"].disabled, false);
    assert.equal(r.load.loading, false);
    assert.match(r.elements["future-radar-graph-status"].textContent,
      failed ? /图谱同步失败/ : /请配置 Neo4j/);
  }
});

test("dense graphs keep 44px rows, scroll vertically, and report actual drawn edges", async () => {
  const r = runtime();
  const nodes = [
    ...Array.from({ length: 60 }, (_, i) => ({ id: `employer:${i}`, kind: "employer", label: `企业 ${i}` })),
    ...Array.from({ length: 20 }, (_, i) => ({ id: `opportunity:${i}`, kind: "opportunity", label: `校园招聘数据分析及技术研究岗位 ${i}` })),
    ...Array.from({ length: 10 }, (_, i) => ({ id: `skill:${i}`, kind: "skill", label: `技能 ${i}` })),
  ];
  const pending = r.load();
  r.resolve({ status: "synced", opportunities: 20, relationships_stored: 28,
    privacy: "Only public attributes; personal state is excluded.", nodes,
    relationships: [
      { source: "employer:0", target: "opportunity:0", kind: "POSTS" },
      { source: "opportunity:0", target: "skill:0", kind: "REQUIRES" },
      { source: "employer:outside-preview", target: "opportunity:0", kind: "POSTS" },
    ],
  });
  await pending;
  const svg = r.elements["future-radar-graph-view"].children[0];
  assert.equal(svg.attributes.viewBox, "0 0 1000 2680");
  assert.equal(svg.attributes.height, "2680");
  assert.equal(svg.style.minWidth, "1000px");
  assert.equal(svg.style.minHeight, "2680px");
  const employers = svg.children.filter((child) => child.attributes.class === "radar-graph-node employer");
  const rows = employers.map((group) => Number(group.children.find((child) => child.tag === "circle").attributes.cy));
  assert.equal(rows.length, 60);
  assert.ok(rows.slice(1).every((value, index) => value - rows[index] === 44));
  assert.equal(svg.children.filter((child) => child.tag === "line").length, 2);
  const status = r.elements["future-radar-graph-status"].textContent;
  assert.match(status, /90 个实体和 2 条关系/);
  assert.match(status, /当前岗位范围内共 28 条关系/);
  assert.doesNotMatch(status, /Only public|personal state/);
  assert.deepEqual(svg.children.filter((child) => child.tag === "text").map((child) => child.textContent), ["企业", "岗位", "技能"]);
  const job = svg.children.find((child) => child.attributes.class === "radar-graph-node opportunity");
  const label = job.children.find((child) => child.tag === "text");
  assert.equal(label.style.fontSize, "14px");
  assert.equal(label.textContent.length, 16);
  assert.ok(label.textContent.endsWith("…"));
  assert.equal(job.children.find((child) => child.tag === "title").textContent, nodes[60].label);
  assert.equal(job.attributes.role, "button");
  assert.equal(job.attributes.tabindex, "0");
  job.listeners.click();
  assert.equal(job.attributes["aria-pressed"], "true");
  assert.equal(employers[1].style.opacity, "0.25");
  assert.match(r.elements["future-radar-graph-status"].textContent, /90 个实体和 2 条关系/);
  assert.match(r.elements["future-radar-graph-status"].textContent, /已选择.*图中关联 2 条关系/);
  let prevented = false;
  job.listeners.keydown({ key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(job.attributes["aria-pressed"], "false");
  assert.equal(employers[1].style.opacity, "1");
  assert.equal(r.elements["future-radar-graph-status"].textContent, status);
  job.listeners.keydown({ key: " ", preventDefault() {} });
  assert.equal(job.attributes["aria-pressed"], "true");
});
