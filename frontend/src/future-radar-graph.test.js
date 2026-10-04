import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createRadarPollingGate } from "./radar-polling.js";

const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const start = source.indexOf("async function loadFutureRadarGraph()");
const end = source.indexOf("async function loadFutureRadarTimeseries()", start);
assert.ok(start >= 0 && end > start);

function runtime() {
  function element(tag) {
    return { tag, value: "", children: [], attributes: {}, style: {}, dataset: {}, listeners: {}, namespaceURI: "http://www.w3.org/2000/svg",
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, listener) { this.listeners[name] = listener; },
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = nodes; },
      querySelector(selector) { return this.children.find((child) => child.tag === selector) || null; },
    };
  }
  const elements = {
    "future-radar-graph-view": element("div"),
    "future-radar-graph-status": { textContent: "" },
    "future-radar-graph-refresh": { disabled: false },
  };
  const calls = [];
  const mapUpdates = [];
  let stored = null, active;
  const gate = createRadarPollingGate({ now: () => 1000, read: () => stored,
    write: (value) => { stored = value; }, locks: () => null });
  const context = vm.createContext({
    state: { token: "test-session" },
    futureRadarMapController: null,
    updateFutureRadarMap: async (graph) => { mapUpdates.push(graph); },
    $: (id) => elements[id],
    radarPollingGate: gate,
    api: (...args) => {
      gate.assertAllowed();
      calls.push(args);
      return new Promise((resolve, reject) => { active = { resolve, reject }; });
    },
    document: { createElement: tag => element(tag), createElementNS: (_namespace, tag) => element(tag) },
  });
  vm.runInContext(source.slice(start, end), context);
  const resetStart = source.indexOf("function resetFutureRadarGraph()");
  const resetEnd = source.indexOf("async function updateFutureRadarMap(", resetStart);
  vm.runInContext(source.slice(resetStart, resetEnd), context);
  return { elements, calls, gate, mapUpdates, context, resolve: (value) => active.resolve(value),
    reject: (error) => active.reject(error), load: context.loadFutureRadarGraph, reset: context.resetFutureRadarGraph };
}

test("graph keyboard focus restores contrast only while focused without changing selection or layout", () => {
  const rule = styles.match(/^\.radar-graph-node:focus-visible\s*\{([^}]+)\}/m);
  assert.ok(rule, "SVG button groups need an explicit focus-visible style");
  const declarations = Object.fromEntries(rule[1].split(";").filter((value) => value.trim())
    .map((value) => value.split(":").map((part) => part.trim())));
  assert.equal(declarations.outline, "3px solid #ffffff");
  assert.equal(declarations["outline-offset"], "4px");
  assert.equal(declarations.opacity, "1 !important", "Focused nodes must remain clear even when their inline opacity is dimmed");
  assert.deepEqual(Object.keys(declarations).sort(), ["opacity", "outline", "outline-offset"],
    "Focus may restore contrast only; do not override selection strokes or graph geometry");
  for (const [, selector, body] of styles.matchAll(/(\.radar-graph-node[^{}]*)\{([^{}]*)\}/g)) {
    if (/\bopacity\s*:/.test(body)) assert.equal(selector.trim(), ".radar-graph-node:focus-visible",
      "Graph dimming may be overridden only for keyboard-visible focus");
  }
});

test("expanded graphs have a bounded two-axis scroller while the recruitment panel remains scrollable", () => {
  const viewport = styles.match(/^\.future-radar-graph-view\s*\{([^}]+)\}/m)?.[1];
  assert.ok(viewport);
  assert.match(viewport, /min-width:\s*0\s*;/);
  assert.match(viewport, /max-width:\s*100%\s*;/);
  assert.match(viewport, /max-height:\s*70vh\s*;/);
  assert.match(viewport, /overflow:\s*auto\s*;/, "All graph rows and columns must be reachable");
  assert.match(viewport, /overscroll-behavior:\s*auto\s*;/);
  assert.match(viewport, /touch-action:\s*pan-x pan-y\s*;/);
  assert.doesNotMatch(viewport, /(?:^|;)\s*height\s*:/, "Empty graphs should not reserve a fixed height");
  assert.match(styles, /^\.recruitment-body\s*\{[^}]*min-width:\s*0[^}]*min-height:\s*0/m);
  assert.match(styles, /^\.recruitment-results\s*\{[^}]*max-width:\s*100%[^}]*overflow-x:\s*hidden[^}]*overflow-y:\s*auto[^}]*overscroll-behavior-y:\s*auto/m);
  assert.match(styles, /\.recruitment-body\s*\{\s*display:\s*block;\s*overflow-x:\s*hidden;\s*overflow-y:\s*auto;\s*overscroll-behavior-y:\s*auto;/);
  assert.match(styles, /\.recruitment-results\s*\{\s*min-height:\s*520px;\s*overflow:\s*visible;/,
    "On narrow screens the body, not another nested results panel, owns vertical scrolling");
});

test("graph scrolling is keyboard reachable and does not intercept wheel or touch navigation", async () => {
  const r = runtime();
  const pending = r.load();
  r.resolve({ status: "synced", opportunities: 1, nodes: [
    { id: "opportunity:1", kind: "opportunity", label: "真实岗位" },
  ], relationships: [] });
  await pending;
  const host = r.elements["future-radar-graph-view"];
  assert.equal(host.attributes.role, "region");
  assert.equal(host.attributes.tabindex, "0");
  assert.match(host.attributes["aria-label"], /左右滚动.*上下滚动浏览面板/);
  assert.equal(host.listeners.wheel, undefined);
  assert.equal(host.listeners.touchmove, undefined);
  assert.equal(host.querySelector("svg").listeners.wheel, undefined);
  assert.equal(host.querySelector("svg").listeners.touchmove, undefined);
  assert.match(styles, /^\.future-radar-graph-view:focus-visible\s*\{[^}]*outline:\s*2px solid #b5f1ff/m);
});

test("all evidenced skills survive the old preview limit and unconnected skills stay excluded", async () => {
  const r = runtime(); const pending = r.load();
  const base = [{ id: "opportunity:1", kind: "opportunity", label: "公开岗位" },
    ...Array.from({ length: 89 }, (_, i) => ({ id: `employer:${i}`, kind: "employer", label: `企业${i}` }))];
  const skills = Array.from({ length: 60 }, (_, i) => ({ id: `skill:${i}`, kind: "skill", label: `有证据技能${i}` }));
  r.resolve({ status: "synced", opportunities: 1, nodes: [...base, ...skills,
    { id: "skill:unconnected", kind: "skill", label: "不相关技能" }],
    relationships: skills.map((skill) => ({ source: "opportunity:1", target: skill.id, kind: "REQUIRES" })) });
  await pending;
  const svg = r.elements["future-radar-graph-view"].querySelector("svg");
  assert.equal(svg.children.filter((node) => node.attributes.class === "radar-graph-node skill").length, 60);
  assert.equal(svg.children.filter((node) => node.attributes.class === "radar-graph-edge requires").length, 60);
  assert.equal(svg.children.some((node) => node.attributes["aria-label"]?.includes("不相关技能")), false);
});

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

test("a fast graph failure waits for lazy map preparation before painting unavailable", async () => {
  const r = runtime();
  let completePreparation;
  const updates = [];
  r.context.updateFutureRadarMap = () => new Promise((resolve) => {
    completePreparation = () => {
      r.context.futureRadarMapController = { update(value) { updates.push(value.status); } };
      resolve();
    };
  });
  const pending = r.load();
  r.reject(new Error("fast failure"));
  await Promise.resolve();
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, true);
  completePreparation();
  await pending;
  assert.deepEqual(updates, ["unavailable"]);
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, false);
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
  const svg = r.elements["future-radar-graph-view"].querySelector("svg");
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
  assert.match(status, /81 个实体和 2 条关系/);
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
  assert.match(r.elements["future-radar-graph-status"].textContent, /81 个实体和 2 条关系/);
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

test("manual graph refresh clears automatic backoff from a failed sibling read", async () => {
  const r = runtime();
  r.gate.failure({ code: "REQUEST_TIMEOUT" });
  assert.ok(r.gate.delay() > 0);
  const pending = r.load();
  assert.equal(r.calls.length, 1);
  assert.equal(r.gate.delay(), 0);
  r.resolve({ status: "synced", nodes: [], postgres_opportunities_considered: 0 });
  await pending;
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, false);
});

test("manual graph refresh still respects a server Retry-After", async () => {
  const r = runtime();
  r.gate.failure({ status: 429, retryAfter: "120" });
  await r.load();
  assert.equal(r.calls.length, 0);
  assert.equal(r.gate.delay(), 120000);
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, false);
  assert.match(r.elements["future-radar-graph-status"].textContent, /等待后重试/);
});

test("failed refresh retains the previous graph and marks its interactive snapshot as stale", async () => {
  const r = runtime();
  const graph = { status: "synced", opportunities: 1, relationships_stored: 2,
    nodes: [
      { id: "employer:1", kind: "employer", label: "企业" },
      { id: "opportunity:1", kind: "opportunity", label: "岗位" },
      { id: "skill:Python", kind: "skill", label: "Python" },
    ], relationships: [
      { source: "employer:1", target: "opportunity:1", kind: "POSTS" },
      { source: "opportunity:1", target: "skill:Python", kind: "REQUIRES" },
    ],
  };
  const first = r.load();
  r.resolve(graph);
  await first;
  const host = r.elements["future-radar-graph-view"], svg = host.querySelector("svg");
  const summary = r.elements["future-radar-graph-status"].textContent;
  const pending = r.load();
  assert.equal(host.querySelector("svg"), svg);
  assert.match(r.elements["future-radar-graph-status"].textContent, /等待期间展示上次成功快照/);
  r.reject(new Error("unavailable"));
  await pending;
  assert.equal(host.querySelector("svg"), svg);
  assert.equal(host.dataset.graphSnapshotStale, "true");
  assert.ok(r.elements["future-radar-graph-status"].textContent.startsWith(summary));
  assert.match(r.elements["future-radar-graph-status"].textContent, /上次成功快照/);
  svg.children.find((child) => child.attributes.class === "radar-graph-node skill").listeners.click();
  assert.match(r.elements["future-radar-graph-status"].textContent, /上次成功快照.*已选择“Python”/);
  const unsuccessful = r.load();
  r.resolve({ status: "not_configured", message: "连接不可用" });
  await unsuccessful;
  assert.equal(host.querySelector("svg"), svg);
  assert.match(r.elements["future-radar-graph-status"].textContent, /连接不可用.*上次成功快照/);
  const recovered = r.load();
  r.resolve(graph);
  await recovered;
  assert.equal(host.children.length, 2);
  assert.notEqual(host.querySelector("svg"), svg);
  assert.equal(host.dataset.graphSnapshotStale, "false");
  assert.equal(r.elements["future-radar-graph-status"].textContent, summary);
});

test("locations form a fourth graph column and graph/map selection shares one snapshot", async () => {
  const r = runtime(), selections = [];
  r.context.futureRadarMapController = { selectNode: (node) => selections.push(node), update() {} };
  const graph = { status: "synced", opportunities: 1, items: [{id:"one", employer:"示例企业", places:[]}],
    nodes: [
      { id: "employer:示例企业", kind: "employer", label: "示例企业" },
      { id: "opportunity:one", kind: "opportunity", label: "分析师" },
      { id: "skill:Python", kind: "skill", label: "Python" },
      { id: "location:810000", kind: "location", label: "香港特别行政区" },
    ], relationships: [
      {source:"employer:示例企业",target:"opportunity:one",kind:"POSTS"},
      {source:"opportunity:one",target:"location:810000",kind:"LOCATED_IN"},
    ] };
  const pending = r.load(); r.resolve(graph); await pending;
  assert.equal(r.mapUpdates[0].status, "loading");
  assert.equal(r.mapUpdates[1], graph, "Map must get the full response, not the 90-node preview");
  const host = r.elements["future-radar-graph-view"], svg = host.querySelector("svg");
  assert.equal(svg.attributes.viewBox, "0 0 1320 610");
  assert.deepEqual(svg.children.filter((node) => node.tag === "text").map((node) => node.textContent), ["企业","岗位","技能","地区"]);
  svg.children.find((node) => node.attributes.class === "radar-graph-node opportunity").listeners.click();
  assert.equal(selections.at(-1).id, "opportunity:one");
  host.selectGraphNode(graph.nodes[0]);
  host.selectGraphNode(graph.nodes[0]);
  assert.equal(selections.length, 1, "Map choices must not feed back and erase map filters");
  const employer = svg.children.find((node) => node.attributes.class === "radar-graph-node employer");
  assert.equal(employer.attributes["aria-pressed"], "true", "Dropdown selection must not toggle itself off");
  host.selectGraphNode(null);
  assert.equal(employer.attributes["aria-pressed"], "false");
  assert.equal(selections.length, 1);
});

test("session reset clears map and graph and refuses a late previous-session response", async () => {
  const r = runtime(); let destroyed = false;
  r.context.futureRadarMapController = { destroy() { destroyed = true; } };
  const pending = r.load(); r.reset(); r.context.state.token = "new-session";
  r.resolve({status:"synced",opportunities:1,nodes:[{id:"old",kind:"employer",label:"OLD_SESSION"}]});
  await pending;
  assert.equal(destroyed, true);
  assert.equal(r.elements["future-radar-graph-view"].children.length, 0);
  assert.equal(r.elements["future-radar-graph-refresh"].disabled, false);
  assert.match(r.elements["future-radar-graph-status"].textContent, /请登录/);
  assert.equal(r.mapUpdates.filter((update) => update.status === "synced").length, 0);
});

test("map entities outside the bounded preview keep the graph clear and report full-input adjacency honestly", async () => {
  const r = runtime(), selections = [];
  r.context.futureRadarMapController = { selectNode: (node) => selections.push(node), update() {} };
  const outside = { id: "employer:qrt", kind: "employer", label: "Qube Research & Technologies (QRT)" };
  const graph = { status: "synced", opportunities: 3, relationships_stored: 492,
    nodes: [
      { id: "employer:visible", kind: "employer", label: "预览企业" },
      { id: "opportunity:visible", kind: "opportunity", label: "预览岗位" },
      ...Array.from({ length: 88 }, (_, i) => ({ id: `skill:${i}`, kind: "skill", label: `技能 ${i}` })),
      outside,
      { id: "opportunity:qrt-one", kind: "opportunity", label: "QRT 岗位一" },
      { id: "opportunity:qrt-two", kind: "opportunity", label: "QRT 岗位二" },
    ], relationships: [
      { source: "employer:visible", target: "opportunity:visible", kind: "POSTS" },
      { source: outside.id, target: "opportunity:qrt-one", kind: "POSTS" },
      { source: outside.id, target: "opportunity:qrt-two", kind: "POSTS" },
    ],
  };
  const pending = r.load(); r.resolve(graph); await pending;
  const host = r.elements["future-radar-graph-view"], svg = host.querySelector("svg");
  const nodes = svg.children.filter((node) => node.attributes.class?.startsWith("radar-graph-node"));
  const edges = svg.children.filter((node) => node.tag === "line");
  host.selectGraphNode(graph.nodes[0]);
  assert.ok(nodes.some((node) => node.style.opacity === "0.25"));
  host.selectGraphNode(outside);
  assert.ok(nodes.some((node) => node.attributes["aria-label"]?.startsWith(outside.label) && node.attributes["aria-pressed"] === "true"));
  assert.equal(edges.filter(edge => edge.style.strokeWidth === "3px").length, 2);
  assert.match(r.elements["future-radar-graph-status"].textContent, /图中关联 2 条关系/);
  assert.match(r.elements["future-radar-graph-status"].textContent, /当前岗位范围内共 492 条关系/);
  assert.doesNotMatch(r.elements["future-radar-graph-status"].textContent, /图中关联 0|无关系/);
  host.selectGraphNode({ id: "location:outside", kind: "location", label: "未进入输入图的地区" });
  assert.match(r.elements["future-radar-graph-status"].textContent, /当前缩略预览未包含该实体/);
  assert.doesNotMatch(r.elements["future-radar-graph-status"].textContent, /关联 0|无关系/);
  assert.equal(selections.length, 0, "Map-originated selection must not feed back into map filters");
  host.selectGraphNode(null);
  assert.equal(r.elements["future-radar-graph-status"].textContent, host.dataset.graphSnapshotStatus);
});

test("complete graph retains all business and geography nodes beyond old preview limits", async () => {
  const r = runtime();
  const nodes = [...Array.from({length:100},(_,index)=>({id:`opportunity:${index}`,kind:"opportunity",label:`岗位 ${index}`})),
    ...Array.from({length:20},(_,index)=>({id:`location:${index}`,kind:"location",label:`地区 ${index}`}))];
  const pending = r.load(); r.resolve({status:"synced",opportunities:100,nodes,items:[],relationships:[]}); await pending;
  const svg = r.elements["future-radar-graph-view"].querySelector("svg");
  assert.equal(svg.children.filter((node)=>node.attributes.class === "radar-graph-node opportunity").length,100);
  assert.equal(svg.children.filter((node)=>node.attributes.class === "radar-graph-node location").length,20);
  assert.match(r.elements["future-radar-graph-status"].textContent,/120 个实体/);
});

test("complete graph retains edges beyond 220 and searches the last entity", async () => {
  const r = runtime();
  const employer = {id:"employer:all",kind:"employer",label:"全部企业"};
  const jobs = Array.from({length:230},(_,i)=>({id:`opportunity:${i}`,kind:"opportunity",label:`岗位 ${i}`}));
  const pending = r.load(); r.resolve({status:"synced",opportunities:230,nodes:[employer,...jobs],
    relationships:jobs.map(job=>({source:employer.id,target:job.id,kind:"POSTS"}))}); await pending;
  const host = r.elements["future-radar-graph-view"];
  assert.equal(host.querySelector("svg").children.filter(node=>node.tag === "line").length,230);
  const navigation = host.children.find(node=>node.className === "radar-graph-navigation");
  const [search,picker] = navigation.children;
  search.value = "岗位 229"; search.listeners.input();
  assert.equal(picker.children.length,2);
  assert.equal(picker.children[1].value,"opportunity:229");
  picker.value = "opportunity:229"; picker.listeners.change();
  assert.match(r.elements["future-radar-graph-status"].textContent,/已选择“岗位 229”.*关联 1 条关系/);
});

test("a successful empty graph cannot revive the old graph selection and counts", async () => {
  const r = runtime();
  let pending = r.load();
  r.resolve({status:"synced",opportunities:1,nodes:[{id:"opportunity:one",kind:"opportunity",label:"旧岗位"}],items:[],relationships:[]});
  await pending;
  assert.equal(typeof r.elements["future-radar-graph-view"].selectGraphNode,"function");
  pending = r.load(); r.resolve({status:"synced",opportunities:0,postgres_opportunities_considered:0,nodes:[],items:[],relationships:[]}); await pending;
  assert.equal(r.elements["future-radar-graph-view"].selectGraphNode,undefined);
  assert.match(r.elements["future-radar-graph-status"].textContent,/机会池为 0 条/);
});

test("geography code is lazy and the map is cleared at session end", () => {
  assert.match(source, /futureRadarMapLoading \|\|= import\("\.\/future-radar-map\.js"\)/);
  assert.doesNotMatch(source, /^import .+ from ["']\.\/future-radar-map/m);
  assert.match(source, /function endFutureRadarSession\([^)]*\) \{\s*resetFutureRadarGraph\(\)/);
});
