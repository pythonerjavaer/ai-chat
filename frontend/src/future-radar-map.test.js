import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createFutureRadarMap, normalizeRadarMapGraph, radarMapJobs, radarMapPlaces,
  createBoundaryIndex, createAdministrativeIndex, createMapProjection, boundaryPath, radarMapDisplayPoint, safeRadarMapUrl, selectMapLabels } from "./future-radar-map.js";

// Small artificial test rectangles are never exported as the country asset.
const feature = (id, name, level, parent, center, extra = {}) => ({ type: "Feature", properties: {
  id, name, level, parent_id: parent, province_id: level === "province" ? id : "440000",
  city_id: level === "city" ? id : "440300", center, ...extra,
}, geometry: { type: "Polygon", coordinates: [[[center[0] - .1, center[1] - .1], [center[0] + .1, center[1] - .1],
  [center[0] + .1, center[1] + .1], [center[0] - .1, center[1] + .1], [center[0] - .1, center[1] - .1]]] } });
const boundaries = { type: "FeatureCollection", crs: "unknown-display", features: [
  feature("440000", "广东省", "province", "100000", [113, 23]),
  feature("440300", "深圳市", "city", "440000", [114, 22.5]),
  feature("440305", "南山区", "district", "440300", [113.9, 22.5]),
  feature("810000", "香港特别行政区", "province", "100000", [114.2, 22.3], { city_id: "" }),
  feature("810001", "中西区", "district", "810000", [114.15, 22.28], { province_id: "810000", city_id: "" }),
  feature("100000_JD", "南海附图", "auxiliary", "100000", [112, 17]),
] };
const place = (id = "440300", extra = {}) => ({ id, name: id === "810001" ? "中西区" : "深圳市", level: id === "810001" ? "district" : "city",
  longitude: 114, latitude: 22.5, province_id: id === "810001" ? "810000" : "440000", province_name: "广东省",
  city_id: id === "810001" ? "" : "440300", city_name: "深圳市", accuracy: "administrative_center", source: "public-catalog", ...extra });
const graph = () => ({ status: "synced", items: [
  { id: "one", employer: "示例科技", title: "Python 技术岗", location: "深圳", skills: ["Python"], places: [place(), place()], url: "https://careers.example.test/one" },
  { id: "two", employer: "另一企业", title: "金融分析岗", location: "香港", skills: ["SQL"], places: [place("810001")], official_url: "https://careers.example.test/two" },
  { id: "unknown", employer: "示例科技", title: "全国招聘", location: "全国", skills: [], places: [], location_status: "unresolved", url: "javascript:alert(1)" },
], nodes: [{ id: "employer:示例科技", kind: "employer", label: "示例科技" }, { id: "opportunity:one", kind: "opportunity", label: "Python 技术岗" }] });

function runtime({ reduced = false } = {}) {
  const media = { matches: reduced, listeners: [], addEventListener(_name, callback) { this.listeners.push(callback); },
    removeEventListener(_name, callback) { this.listeners = this.listeners.filter((value) => value !== callback); } };
  const document = { defaultView: { matchMedia: () => media } };
  function node(tag, namespaceURI = "") {
    return { tag, namespaceURI, ownerDocument: document, children: [], attributes: {}, listeners: {}, dataset: {}, style: {},
      className: "", textContent: "", hidden: false, value: "",
      setAttribute(name, value) { this.attributes[name] = String(value); }, getAttribute(name) { return this.attributes[name]; },
      addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
      append(...values) { this.children.push(...values); }, replaceChildren(...values) { this.children = values; },
      focus() { document.activeElement = this; },
      fire(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); },
    };
  }
  document.createElement = (tag) => node(tag);
  document.createElementNS = (namespace, tag) => node(tag, namespace);
  const host = node("div");
  const all = () => { const output = []; const walk = (value) => { output.push(value); for (const child of value.children) walk(child); }; walk(host); return output; };
  const byClass = (className) => all().find((value) => (value.className || value.attributes.class || "").split(" ").includes(className));
  const withAttribute = (name, value) => all().find((node) => node.attributes[name] === value);
  const button = (label) => all().find((node) => node.tag === "button" && node.textContent === label);
  return { host, document, media, all, byClass, withAttribute, button };
}

test("only real public item ids and valid administrative points contribute to job counts", () => {
  const input = graph(); input.items.push({ ...input.items[0], private_profile: "secret" });
  input.items.push({ id: "bad", employer: "未知企业", title: "地点未知", places: [place("bad", { longitude: null })] });
  const model = normalizeRadarMapGraph(input);
  assert.equal(model.jobs.length, 4);
  assert.deepEqual(radarMapPlaces(model.jobs).map((value) => [value.id, value.count]), [["440300", 1], ["810001", 1]]);
  assert.equal(model.jobs.filter((job) => !job.places.length).length, 2);
  assert.equal(JSON.stringify(model.jobs).includes("secret"), false);
  assert.equal(model.jobs.find((job) => job.id === "unknown").url, null);
});

test("geography fallback joins only actual item ids and never overrides explicit unresolved places", () => {
  const input = { items: [{ id: "fallback", employer: "企业", title: "岗位" }, { id: "unresolved", places: [] }], geography: {
    features: [{ geometry: { type: "Point", coordinates: [114, 22.5] }, properties: { ...place(), opportunity_ids: ["fallback", "unresolved", "nonexistent"] } }],
  } };
  const model = normalizeRadarMapGraph(input);
  assert.equal(model.jobs[0].places.length, 1);
  assert.equal(model.jobs[1].places.length, 0);
  assert.deepEqual(radarMapPlaces(model.jobs)[0].opportunity_ids, ["fallback"]);
  input.geography.features = { type: "FeatureCollection", features: input.geography.features };
  assert.deepEqual(normalizeRadarMapGraph(input).jobs, model.jobs);
});

test("enterprise distributions are recruitment locations, never invented headquarters, and skills filter actual jobs", () => {
  const model = normalizeRadarMapGraph(graph()), index = createBoundaryIndex(boundaries);
  assert.deepEqual(radarMapJobs(model, { employer: "employer:示例科技" }, index).map((job) => job.id), ["one", "unknown"]);
  assert.deepEqual(radarMapJobs(model, { skill: "python" }, index).map((job) => job.id), ["one"]);
  assert.deepEqual(radarMapJobs(model, { province: "810000" }, index).map((job) => job.id), ["two"]);
  assert.deepEqual(radarMapJobs(model, { place: "440305" }, index), []);
  assert.deepEqual(radarMapJobs(model, { unlocated: true }, index).map((job) => job.id), ["unknown"]);
});

test("polygon and multipolygon holes render from source coordinates, and display points join administrative ids", () => {
  const polygon = boundaries.features[0], project = createMapProjection([polygon]);
  const multi = { geometry: { type: "MultiPolygon", coordinates: [polygon.geometry.coordinates, polygon.geometry.coordinates] } };
  assert.equal((boundaryPath(polygon, project).match(/M/g) || []).length, 1);
  assert.equal((boundaryPath(multi, project).match(/M/g) || []).length, 2);
  assert.ok(project(polygon.properties.center).every(Number.isFinite));
  assert.deepEqual(radarMapDisplayPoint(place(), createBoundaryIndex(boundaries)), [114, 22.5]);
  assert.deepEqual(radarMapDisplayPoint(place("810001"), createBoundaryIndex(boundaries)), [114.15, 22.28]);
  assert.equal(boundaryPath({ geometry: { type: "Point", coordinates: [0, 0] } }, project), "");
});

test("external public links reject credentials, script/data schemes and local/private hosts", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,secret", "https://user:pass@example.test", "http://localhost/",
    "http://127.0.0.1/", "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/", "http://[::1]/",
    "http://[::ffff:192.168.1.1]/", "http://[::ffff:7f00:1]/", "https://[2001:4860:4860::8888]/", "not a url"]) {
    assert.equal(safeRadarMapUrl({ url }), null, url);
  }
  assert.equal(safeRadarMapUrl({ application_url: "javascript:bad", official_url: "https://careers.example.test/apply" }), "https://careers.example.test/apply");
});

test("zero data is honest while province/city/district and Hong Kong remain keyboard browsable", async () => {
  const r = runtime(), emitted = [];
  const map = createFutureRadarMap({ host: r.host, boundaries, onSelect: (value) => emitted.push(value) }); await map.ready;
  map.update({ status: "synced", items: [] });
  assert.match(r.byClass("radar-map-summary").textContent, /公开岗位 0.*当前筛选 0.*未定位岗位（全部）0/);
  assert.match(r.byClass("radar-map-empty").textContent, /暂无可展示/);
  assert.equal(r.all().filter((node) => node.attributes["data-place-id"]).length, 0);
  const province = r.withAttribute("data-region-id", "440000");
  let prevented = false; province.fire("keydown", { key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.ok(r.withAttribute("data-region-id", "440300"));
  r.withAttribute("data-region-id", "440300").fire("click"); assert.ok(r.withAttribute("data-region-id", "440305"));
  r.button("中国 · 含香港").fire("click"); r.button("香港放大").fire("click");
  assert.ok(r.withAttribute("data-region-id", "810001"));
  assert.ok(emitted.length > 0 && emitted.every((value) => value === null), "Zero-data administrative exploration clears highlights without fabricating nodes");
  map.destroy(); assert.equal(r.host.children.length, 0); assert.equal(r.media.listeners.length, 0);
});

test("ordinary city drilldown offers zero-job district reference points without inventing polygons", async () => {
  const r = runtime(), catalog = { places: [{ id: "geonames:district", name: "测试区", level: "district", parent_id: "440300",
    province_id: "440000", city_id: "440300", center: [114.01, 22.55], crs: "WGS84", source: "test-only" }] };
  const onlyProvinceCity = { ...boundaries, features: boundaries.features.filter((value) => value.properties.level !== "district") };
  const index = createAdministrativeIndex(onlyProvinceCity, catalog);
  assert.equal(index.get("geonames:district").geometry.type, "Point");
  const map = createFutureRadarMap({ host: r.host, boundaries: onlyProvinceCity, catalog }); await map.ready;
  map.update({ status: "synced", items: [] });
  r.withAttribute("data-region-id", "440000").fire("click"); r.withAttribute("data-region-id", "440300").fire("click");
  const district = r.withAttribute("data-region-id", "geonames:district");
  assert.match(district.attributes["aria-label"], /行政区参考点.*0 个公开岗位.*无区县边界/);
  district.fire("click"); assert.equal(r.byClass("radar-map-current-place").textContent, "测试区");
  assert.equal(r.all().filter((value) => value.attributes["data-place-id"]).length, 0);
});

test("unverified orphan counties are not drill targets and unknown jobs do not acquire their reference points", async () => {
  const orphan = { id: "geonames:orphan", name: "层级待核县", level: "district", parent_id: "440000",
    province_id: "440000", city_id: "", center: [114.02, 22.56], navigation_visible: false,
    hierarchy_status: "province_only_unverified" };
  const legacy = { id: "geonames:legacy", name: "已关联测试区", level: "district", parent_id: "440300",
    province_id: "440000", city_id: "440300", center: [114.03, 22.57] };
  const catalog = { places: [orphan, legacy] }, index = createAdministrativeIndex(boundaries, catalog);
  assert.equal(index.has(orphan.id), false);
  assert.equal(index.has(legacy.id), true, "Missing navigation flags preserve existing behavior");
  assert.equal(index.has("810001"), true, "Verified direct Hong Kong district polygons are unaffected");
  const r = runtime(); const map = createFutureRadarMap({ host: r.host, boundaries, catalog }); await map.ready;
  map.update({ status: "synced", items: [{ id: "unknown", employer: "测试企业", title: "真实地点未知岗位",
    location: orphan.name, places: [], location_status: "unresolved" }], geography: { features: {
    type: "FeatureCollection", features: [{ type: "Feature", properties: { ...orphan, opportunity_ids: ["unknown"] },
      geometry: { type: "Point", coordinates: orphan.center } }],
  } } });
  r.withAttribute("data-region-id", "440000").fire("click");
  assert.equal(r.withAttribute("data-region-id", orphan.id), undefined);
  r.withAttribute("data-region-id", "440300").fire("click");
  assert.equal(r.withAttribute("data-region-id", orphan.id), undefined);
  assert.ok(r.withAttribute("data-region-id", legacy.id));
  assert.equal(r.all().filter((value) => value.attributes["data-place-id"]).length, 0);
  assert.match(r.byClass("radar-map-summary").textContent, /未定位岗位（全部）1/);
  r.button("香港放大").fire("click"); assert.ok(r.withAttribute("data-region-id", "810001"));
  map.destroy();
});

test("upstream province and city Location parents filter descendant jobs, not just exact leaf places", async () => {
  const r = runtime(), emitted = [], data = graph();
  data.items.push({ id: "district-job", employer: "第三企业", title: "区县公开岗位", places: [place("440305", { name: "南山区", level: "district" })] });
  data.nodes.push({ id: "location:440000", kind: "location", label: "广东省" });
  const map = createFutureRadarMap({ host: r.host, boundaries, onSelect: (node) => emitted.push(node) }); await map.ready; map.update(data);
  map.selectNode({ id: "location:440000", kind: "location", place: { id: "440000", name: "广东省", level: "province" } });
  assert.equal(r.byClass("radar-map-job-list").children.length, 2);
  assert.ok(r.withAttribute("data-region-id", "440300"));
  assert.equal(emitted.length, 0);
  map.selectNode({ id: "location:440300", kind: "location" }); assert.equal(r.byClass("radar-map-job-list").children.length, 2);
  map.selectNode(null); r.withAttribute("data-region-id", "440000").fire("click");
  assert.equal(emitted.at(-1).id, "location:440000", "Real parent graph nodes must participate in reverse selection");
});

test("native location count buttons offer the same real marker filtering on narrow touch displays", async () => {
  const r = runtime(), emitted = []; const map = createFutureRadarMap({ host: r.host, boundaries, onSelect: (node) => emitted.push(node) });
  await map.ready; map.update(graph());
  assert.deepEqual(r.byClass("radar-map-location-list").children.map((node) => node.textContent), ["深圳市 · 1", "中西区 · 1"]);
  r.button("深圳市 · 1").fire("click"); assert.equal(emitted.at(-1).id, "location:440300");
  assert.equal(r.byClass("radar-map-job-list").children.length, 1);
});

test("screen-coordinate label collision prefers selection and reveals more labels after drilldown", () => {
  const close = [
    { id: "region", text: "广东省", x: 500, y: 300, priority: 0, anchor: "middle" },
    { id: "city", text: "深圳市 · 3", x: 500, y: 300, priority: 10 },
    { id: "selected", text: "南山区 · 1", x: 502, y: 301, priority: 100 },
  ];
  assert.deepEqual(selectMapLabels(close, 360).map((value) => value.id), ["selected"]);
  const zoomed = close.map((value, index) => ({ ...value, x: 200 + index * 300 }));
  assert.equal(selectMapLabels(zoomed, 1000).length, 3);
});

test("breadcrumbs and zero-job administrative drill synchronize or clear actual graph highlights", async () => {
  const r = runtime(), emitted = [], data = graph(); data.nodes.push({ id: "location:440000", kind: "location", label: "广东省" });
  const map = createFutureRadarMap({ host: r.host, boundaries, onSelect: (node) => emitted.push(node) }); await map.ready; map.update(data);
  r.withAttribute("data-region-id", "440000").fire("click"); r.withAttribute("data-region-id", "440300").fire("click");
  assert.equal(emitted.at(-1).id, "location:440300");
  r.button("广东省").fire("click"); assert.equal(emitted.at(-1).id, "location:440000");
  r.button("中国 · 含香港").fire("click"); assert.equal(emitted.at(-1), null);
  map.update({ status: "synced", items: [data.items[0]], nodes: [] });
  r.button("香港放大").fire("click"); assert.equal(emitted.at(-1), null);
  r.withAttribute("data-region-id", "810001").fire("click"); assert.equal(emitted.at(-1), null);
  assert.equal(r.byClass("radar-map-job-list").children[0].className, "radar-map-empty");
});

test("ambiguity remains disclosed even when the parent province has a valid administrative point", async () => {
  const r = runtime(), data = graph();
  data.items.push({ id: "partial", employer: "待确认企业", title: "公开省内岗位", location_status: "ambiguous",
    places: [place("440000", { name: "广东省", level: "province", city_id: "" })] });
  data.items.push({ id: "confirmed-province", employer: "省域企业", title: "明确省域岗位", location_status: "resolved",
    places: [place("440000", { name: "广东省", level: "province", city_id: "" })] });
  const map = createFutureRadarMap({ host: r.host, boundaries }); await map.ready; map.update(data);
  assert.match(r.byClass("radar-map-summary").textContent, /未定位岗位（全部）1.*地点待确认（全部）1/);
  assert.equal(r.all().filter((node) => node.className === "radar-map-location-warning").length, 1);
  assert.equal(r.byClass("radar-map-location-warning").textContent, "仅标示可确认行政范围，更具体地点待确认");
  map.selectNode({ id: "opportunity:partial", kind: "opportunity" });
  assert.match(r.byClass("radar-map-summary").textContent, /当前筛选 1.*地点待确认（全部）1/);
  assert.ok(r.withAttribute("data-place-id", "440000"), "The confirmed parent point remains visible, without guessing a city");
});

test("credential-bearing query keys are rejected but public job and ordinary tracking ids remain usable", () => {
  for (const key of ["api_key", "ApiKey", "api-key", "token", "access_token", "password", "client_secret", "auth_token", "private_key"]) {
    assert.equal(safeRadarMapUrl({ application_url: `https://careers.example.test/apply?${key}=private-value` }), null, key);
  }
  assert.equal(safeRadarMapUrl({ url: "https://careers.example.test/apply?api%5Fkey=private" }), null);
  assert.equal(safeRadarMapUrl({ application_url: "https://careers.example.test/apply?token=private", official_url: "https://careers.example.test/jobs/1" }), "https://careers.example.test/jobs/1");
  assert.equal(safeRadarMapUrl({ url: "https://careers.example.test/apply?job_id=1&source=campus&utm_source=public" }), "https://careers.example.test/apply?job_id=1&source=campus&utm_source=public");
});

test("separate employer/job controls and markers link to graph selection without recursive callbacks", async () => {
  const r = runtime(), emitted = [];
  const map = createFutureRadarMap({ host: r.host, boundaries, onSelect: (node) => emitted.push(node) }); await map.ready; map.update(graph());
  const employer = r.withAttribute("aria-label", "选择企业招聘分布"); employer.value = "employer:示例科技"; employer.fire("change");
  assert.equal(emitted.at(-1).id, "employer:示例科技");
  const jobs = r.withAttribute("aria-label", "单独选择公开岗位");
  assert.deepEqual(jobs.children.map((node) => node.value), ["", "one", "unknown"]);
  jobs.value = "one"; jobs.fire("change"); assert.equal(emitted.at(-1).id, "opportunity:one");
  assert.match(r.byClass("radar-map-summary").textContent, /当前筛选 1/);
  r.withAttribute("data-place-id", "440300").fire("click"); assert.equal(emitted.at(-1).id, "location:440300");
  const count = emitted.length;
  map.selectNode({ id: "opportunity:two", kind: "opportunity" });
  assert.equal(emitted.length, count); assert.equal(jobs.value, "two");
  assert.ok(r.withAttribute("data-place-id", "810001"));
  map.selectNode(null); assert.match(r.byClass("radar-map-summary").textContent, /当前筛选 3/);
  assert.equal(emitted.length, count);
  const link = r.all().find((node) => node.tag === "a"); assert.equal(link.target, "_blank"); assert.equal(link.rel, "noopener noreferrer");
});

test("failed refresh retains the snapshot and map controls; successful zero clears real data", async () => {
  const r = runtime(); const map = createFutureRadarMap({ host: r.host, boundaries }); await map.ready; map.update(graph());
  map.selectNode({ id: "employer:示例科技", kind: "employer" }); r.button("2.5D 浮起").fire("click"); r.button("关闭动效").fire("click");
  map.update({ status: "unavailable" });
  assert.equal(r.byClass("radar-map").dataset.snapshotStale, "true");
  assert.match(r.byClass("radar-map-summary").textContent, /上次成功快照.*当前筛选 2/);
  assert.match(r.byClass("radar-map").className, /radar-map-raised/);
  assert.doesNotMatch(r.byClass("radar-map").className, /radar-map-motion/);
  map.update(graph()); assert.equal(r.byClass("radar-map").dataset.snapshotStale, "false");
  assert.equal(r.withAttribute("aria-label", "选择企业招聘分布").value, "employer:示例科技");
  map.update({ status: "synced", items: [] }); assert.match(r.byClass("radar-map-summary").textContent, /公开岗位 0/);
});

test("initial loading is not claimed as zero data, and refresh loading keeps the selected snapshot", async () => {
  const r = runtime(); const map = createFutureRadarMap({ host: r.host, boundaries }); await map.ready;
  map.update({ status: "loading" });
  assert.match(r.byClass("radar-map-summary").textContent, /正在读取.*暂无成功快照/);
  assert.doesNotMatch(r.byClass("radar-map-summary").textContent, /公开岗位 0/);
  assert.match(r.byClass("radar-map-empty").textContent, /尚未载入/);
  map.update(graph()); map.selectNode({ id: "opportunity:one", kind: "opportunity" }); map.update({ status: "loading" });
  assert.match(r.byClass("radar-map-summary").textContent, /正在读取.*上次成功快照.*当前筛选 1/);
  assert.equal(r.withAttribute("aria-label", "单独选择公开岗位").value, "one");
});

test("reduced motion blocks automatic animation and follows later system preference changes", async () => {
  const r = runtime({ reduced: true }); const map = createFutureRadarMap({ host: r.host, boundaries }); await map.ready; map.update(graph());
  assert.doesNotMatch(r.byClass("radar-map").className, /radar-map-motion/);
  assert.equal(r.button("已遵循减少动态效果").disabled, true);
  r.media.matches = false; r.media.listeners[0](); assert.match(r.byClass("radar-map").className, /radar-map-motion/);
  r.button("关闭动效").fire("click"); assert.doesNotMatch(r.byClass("radar-map").className, /radar-map-motion/);
});

test("offline asset failure leaves public details usable and late loading does not resurrect a destroyed map", async () => {
  const r = runtime(); const map = createFutureRadarMap({ host: r.host, boundaryLoader: () => { throw new Error("offline unavailable"); } });
  map.update(graph()); assert.equal(await map.ready, false);
  assert.match(r.byClass("radar-map-message").textContent, /离线行政边界暂不可用/);
  assert.equal(r.byClass("radar-map-job-list").children.length, 3);
  const second = runtime(); let resolve; const late = createFutureRadarMap({ host: second.host, boundaryLoader: () => new Promise((done) => { resolve = done; }) });
  await Promise.resolve(); late.destroy(); resolve(boundaries); assert.equal(await late.ready, false); assert.equal(second.host.children.length, 0);
});

test("map styles remain responsive, slow-pulsed, and include a reduced-motion hard stop", () => {
  const css = readFileSync(new URL("./future-radar-map.css", import.meta.url), "utf8");
  assert.doesNotMatch(css, /min-width:\s*[1-9]\d*px/);
  assert.match(css, /radar-map-slow-pulse 8s/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(css, /animation:\s*none !important/);
  assert.match(css, /focus-visible/);
});

test("real offline assets retain province/Hong Kong polygons and ordinary city point-only districts", async () => {
  const country = JSON.parse(readFileSync(new URL("./data/china-boundaries.json", import.meta.url), "utf8"));
  const centers = JSON.parse(readFileSync(new URL("./data/china-admin-centers.json", import.meta.url), "utf8"));
  const polygons = createBoundaryIndex(country), index = createAdministrativeIndex(country, centers);
  assert.equal([...polygons.values()].filter((value) => value.properties.level === "province").length, 34);
  assert.equal([...polygons.values()].filter((value) => value.properties.level === "district" && value.properties.province_id === "810000").length, 18);
  assert.ok(polygons.has("100000_JD"), "Keep the source South China Sea auxiliary geometry without creating a job location");
  assert.ok([...index.values()].some((value) => value.properties.city_id === "440300" && value.properties.level === "district" && value.geometry.type === "Point"));
  assert.equal(country.crs, "unknown-display");
  const r = runtime(); const map = createFutureRadarMap({ host: r.host, boundaries: country, catalog: centers }); await map.ready;
  map.update({ status: "synced", items: [] });
  assert.equal(r.all().filter((value) => value.attributes["data-region-id"]).length, 35);
  r.withAttribute("data-region-id", "440000").fire("click"); r.withAttribute("data-region-id", "440300").fire("click");
  assert.ok(r.all().some((value) => value.attributes["data-region-id"]?.startsWith("geonames:")));
  assert.match(r.byClass("radar-map-attribution").children.map((value) => value.textContent).join(" "), /GeoNames.*CC-BY-4.0/);
  map.destroy();
});
