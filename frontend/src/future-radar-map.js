/** Offline administrative schematic. Never infer offices, device location or distances. */
const SVG_NS = "http://www.w3.org/2000/svg";
const text = (value) => String(value ?? "").trim();
const fold = (value) => text(value).toLowerCase().replace(/ß/g, "ss").replace(/ς/g, "σ");
let instanceNumber = 0;
const CREDENTIAL_QUERY_KEYS = new Set(["apikey", "apitoken", "token", "accesstoken", "refreshtoken", "idtoken",
  "authtoken", "sessiontoken", "password", "passwd", "pwd", "secret", "clientsecret", "authorization", "auth",
  "bearer", "privatekey", "signingkey", "credential", "credentials"]);

function coordinate(value) {
  if (typeof value !== "number" && (typeof value !== "string" || !value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function validPoint(point) {
  if (!Array.isArray(point) || point.length < 2) return null;
  const longitude = coordinate(point[0]), latitude = coordinate(point[1]);
  return longitude !== null && latitude !== null && Math.abs(longitude) <= 180 && Math.abs(latitude) < 85
    ? [longitude, latitude] : null;
}

export function safeRadarMapUrl(job) {
  for (const value of [job?.application_url, job?.official_url, job?.url]) {
    if (!text(value)) continue;
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password
          || [...url.searchParams.keys()].some((key) => CREDENTIAL_QUERY_KEYS.has(key.toLowerCase().replace(/[-_]/g, "")))
          || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")
          || host.includes(":")
          || /^(?:0|10|127)\./.test(host) || /^169\.254\./.test(host) || /^192\.168\./.test(host)
          || /^172\.(?:1[6-9]|2\d|3[01])\./.test(host)) continue;
      return url.href;
    } catch { /* A malformed public link is not a navigable link. */ }
  }
  return null;
}

function publicPlace(value) {
  const point = validPoint([value?.longitude, value?.latitude]);
  if (!text(value?.id) || !point) return null;
  return { id: text(value.id), name: text(value.name) || text(value.id), level: text(value.level),
    province_id: text(value.province_id), province_name: text(value.province_name),
    city_id: text(value.city_id), city_name: text(value.city_name), longitude: point[0], latitude: point[1],
    accuracy: text(value.accuracy) || "administrative_center", source: text(value.source) };
}

export function normalizeRadarMapGraph(graph = {}) {
  const nodes = new Map((Array.isArray(graph.nodes) ? graph.nodes : []).filter((node) => text(node?.id))
    .map((node) => [text(node.id), { id: text(node.id), kind: text(node.kind), label: text(node.label) }]));
  const employers = new Map((Array.isArray(graph.relationships) ? graph.relationships : [])
    .filter((edge) => edge?.kind === "POSTS")
    .map((edge) => [text(edge.target).replace(/^opportunity:/, ""), text(edge.source)]));
  const geography = new Map();
  const geographicValue = graph.geography?.features;
  const geographicFeatures = Array.isArray(geographicValue) ? geographicValue
    : Array.isArray(geographicValue?.features) ? geographicValue.features : [];
  for (const feature of geographicFeatures) {
    if (feature?.geometry?.type !== "Point") continue;
    const properties = feature.properties || {};
    const place = publicPlace({ ...properties, longitude: feature.geometry.coordinates?.[0],
      latitude: feature.geometry.coordinates?.[1] });
    if (!place) continue;
    for (const id of Array.isArray(properties.opportunity_ids) ? properties.opportunity_ids : []) {
      if (!geography.has(text(id))) geography.set(text(id), []);
      geography.get(text(id)).push(place);
    }
  }
  const jobs = [], seen = new Set(), companies = new Map(), places = new Map();
  for (const item of Array.isArray(graph.items) ? graph.items : []) {
    const id = text(item?.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const employer = text(item.employer || item.company) || "未标注企业";
    const employerId = employers.get(id) || `employer:${fold(employer)}`;
    const located = new Map();
    // Explicit empty places means unresolved. Do not revive a stale geography point.
    for (const value of Array.isArray(item.places) ? item.places : geography.get(id) || []) {
      const place = publicPlace(value);
      if (place) { located.set(place.id, place); places.set(place.id, place); }
    }
    const job = { id, employer, employerId, title: text(item.title) || "未标注公开岗位",
      location: text(item.location), skills: (Array.isArray(item.skills) ? item.skills : []).map(text).filter(Boolean),
      places: [...located.values()], location_status: text(item.location_status), url: safeRadarMapUrl(item) };
    jobs.push(job);
    if (!companies.has(employerId)) companies.set(employerId, { id: employerId, name: employer });
  }
  return { jobs, companies, places, nodes };
}

export function createBoundaryIndex(collection) {
  return new Map((Array.isArray(collection?.features) ? collection.features : [])
    .filter((feature) => ["Polygon", "MultiPolygon"].includes(feature?.geometry?.type) && text(feature.properties?.id))
    .map((feature) => [text(feature.properties.id), feature]));
}

export function createAdministrativeIndex(collection, catalog) {
  const index = createBoundaryIndex(collection);
  for (const place of Array.isArray(catalog?.places) ? catalog.places : []) {
    // Do not turn a province-only, unverified county hierarchy into a city or
    // district drill target. Legacy records without this explicit flag remain.
    if (place.navigation_visible === false) continue;
    const id = text(place.id), center = validPoint(place.center);
    if (id && center && !index.has(id) && ["province", "city", "district"].includes(place.level)) {
      index.set(id, { type: "Feature", properties: { id, name: text(place.name), level: place.level,
        parent_id: text(place.parent_id), province_id: text(place.province_id), city_id: text(place.city_id),
        center, crs: text(place.crs), source: text(place.source) }, geometry: { type: "Point", coordinates: center } });
    }
  }
  return index;
}

function within(place, id, boundaries) {
  if (!id) return true;
  if ([place.id, place.province_id, place.city_id].includes(id)) return true;
  let current = place.id;
  const visited = new Set();
  while (current && !visited.has(current)) {
    if (current === id) return true;
    visited.add(current);
    current = text(boundaries.get(current)?.properties?.parent_id);
  }
  return false;
}

export function radarMapJobs(model, selection = {}, boundaries = new Map()) {
  return model.jobs.filter((job) => (!selection.employer || job.employerId === selection.employer)
    && (!selection.job || job.id === selection.job)
    && (!selection.skill || job.skills.some((skill) => fold(skill) === fold(selection.skill)))
    && (!selection.unlocated || !job.places.length)
    && (!selection.place || job.places.some((place) => place.id === selection.place))
    && (!selection.city || job.places.some((place) => within(place, selection.city, boundaries)))
    && (!selection.province || job.places.some((place) => within(place, selection.province, boundaries))));
}

export function radarMapPlaces(jobs) {
  const places = new Map();
  for (const job of jobs) for (const place of job.places) {
    if (!places.has(place.id)) places.set(place.id, { ...place, opportunity_ids: new Set() });
    places.get(place.id).opportunity_ids.add(job.id);
  }
  return [...places.values()].map((place) => ({ ...place, count: place.opportunity_ids.size,
    opportunity_ids: [...place.opportunity_ids] }));
}

function geometryPoints(feature) {
  if (feature?.geometry?.type === "Point") { const point = validPoint(feature.geometry.coordinates); return point ? [point] : []; }
  const polygons = feature?.geometry?.type === "Polygon" ? [feature.geometry.coordinates]
    : feature?.geometry?.type === "MultiPolygon" ? feature.geometry.coordinates : [];
  return polygons.flatMap((polygon) => polygon.flatMap((ring) => ring.map(validPoint).filter(Boolean)));
}

export function createMapProjection(features, width = 1000, height = 660, padding = 40) {
  const mercator = ([longitude, latitude]) => [longitude * Math.PI / 180,
    -Math.log(Math.tan(Math.PI / 4 + latitude * Math.PI / 360))];
  const points = features.flatMap(geometryPoints).map(mercator);
  if (!points.length) return () => [width / 2, height / 2];
  let left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
  for (const [x, y] of points) { left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y); }
  const scale = Math.min((width - 2 * padding) / Math.max(right - left, 0.000001),
    (height - 2 * padding) / Math.max(bottom - top, 0.000001));
  return (point) => {
    const [x, y] = mercator(point);
    return [width / 2 + (x - (left + right) / 2) * scale, height / 2 + (y - (top + bottom) / 2) * scale];
  };
}

export function boundaryPath(feature, project) {
  const polygons = feature?.geometry?.type === "Polygon" ? [feature.geometry.coordinates]
    : feature?.geometry?.type === "MultiPolygon" ? feature.geometry.coordinates : [];
  return polygons.flatMap((polygon) => polygon.map((ring) => {
    const points = ring.map(validPoint).filter(Boolean);
    if (points.length < 3) return "";
    return points.map((point, index) => `${index ? "L" : "M"}${project(point).map((value) => value.toFixed(2)).join(",")}`).join(" ") + " Z";
  })).join(" ");
}

export function radarMapDisplayPoint(place, boundaries) {
  // The offline polygon source does not declare its CRS. Join by administrative
  // id and use its own display center, not a false precise WGS84/GCJ02 overlay.
  return validPoint(boundaries.get(place.id)?.properties?.center) || validPoint([place.longitude, place.latitude]);
}

export function selectMapLabels(labels, screenWidth = 1000) {
  const scale = Math.max(1, screenWidth) / 1000, boxes = [], accepted = [];
  for (const label of [...labels].sort((a, b) => (b.priority || 0) - (a.priority || 0))) {
    const glyphs = [...label.text].reduce((sum, char) => sum + (/[^\x00-\x7F]/.test(char) ? 1 : .58), 0);
    // Reserve at least a readable screen-size footprint on small displays,
    // rather than letting microscopic national labels pile up on each other.
    const font = Math.max(10, (label.fontSize || 14) * scale), width = glyphs * font + 8, height = font + 6;
    let x = label.x, anchor = label.anchor || "start";
    if (anchor === "start" && x * scale + width > screenWidth - 4) { anchor = "end"; x -= 24; }
    const left = x * scale - (anchor === "middle" ? width / 2 : anchor === "end" ? width : 0);
    const box = { left, right: left + width, top: label.y * scale - height, bottom: label.y * scale + 3 };
    if (box.left < 0 || box.right > screenWidth || box.top < 0
        || boxes.some((other) => box.left < other.right + 4 && box.right > other.left - 4
          && box.top < other.bottom + 3 && box.bottom > other.top - 3)) continue;
    boxes.push(box); accepted.push({ ...label, x, anchor });
  }
  return accepted;
}

// Vite turns local JSON into a JavaScript module; do not assert a JSON MIME in
// the browser. Node controller tests inject their offline fixtures instead.
const defaultBoundaryLoader = () => import("./data/china-boundaries.json")
  .then((module) => module.default);
const defaultCatalogLoader = () => import("./data/china-admin-centers.json").then((module) => module.default);

export function createFutureRadarMap({ host, onSelect = () => {}, boundaries, catalog,
  boundaryLoader = defaultBoundaryLoader, catalogLoader = defaultCatalogLoader } = {}) {
  if (!host?.ownerDocument) throw new TypeError("Future Radar map requires a DOM host");
  const document = host.ownerDocument;
  const prefix = `radar-map-${++instanceNumber}`;
  const media = document.defaultView?.matchMedia?.("(prefers-reduced-motion: reduce)");
  const state = { employer: "", job: "", place: "", skill: "", province: "", city: "", unlocated: false,
    mode: "2d", yaw: -18, pitch: 56, motionOverride: null, feedback: "", stale: false, loading: false, snapshot: false };
  let model = normalizeRadarMapGraph(), collection = null, adminCatalog = null, index = new Map(), destroyed = false, boundaryError = false;
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const svgElement = (tag, attributes = {}, content) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const root = element("section", "radar-map"), heading = element("div", "radar-map-heading");
  const title = element("h3", "", "中国招聘地图"), subtitle = element("p", "", "企业招聘分布（非总部地址）");
  heading.append(title, subtitle);
  const controls = element("div", "radar-map-controls");
  const employerLabel = element("label", "radar-map-control", "企业"), employerSelect = element("select");
  const jobLabel = element("label", "radar-map-control", "岗位"), jobSelect = element("select");
  const skillLabel = element("label", "radar-map-control", "技能"), skillSelect = element("select");
  employerSelect.setAttribute("aria-label", "选择企业招聘分布");
  jobSelect.setAttribute("aria-label", "单独选择公开岗位");
  skillSelect.setAttribute("aria-label", "按公开岗位技能筛选");
  employerLabel.append(employerSelect); jobLabel.append(jobSelect);
  skillLabel.append(skillSelect);
  const mode = element("button", "radar-map-button", "3D 地图"), resetView = element("button", "radar-map-button", "重置视角");
  const motion = element("button", "radar-map-button", "关闭动效");
  const clear = element("button", "radar-map-button", "清除选择");
  for (const button of [mode, resetView, motion, clear]) button.type = "button";
  controls.append(employerLabel, jobLabel, skillLabel, mode, resetView, motion, clear);
  const viewStatus = element("p", "radar-map-view-status");
  viewStatus.setAttribute("role", "status"); viewStatus.setAttribute("aria-live", "polite");
  const breadcrumb = element("nav", "radar-map-breadcrumb"); breadcrumb.setAttribute("aria-label", "行政区地图层级");
  const summary = element("p", "radar-map-summary"); summary.setAttribute("role", "status"); summary.setAttribute("aria-live", "polite");
  const viewport = element("div", "radar-map-viewport"), stage = element("div", "radar-map-stage");
  viewport.setAttribute("tabindex", "0");
  viewport.setAttribute("aria-label", "中国公开招聘 3D 地图视图，可拖拽旋转");
  const map = svgElement("svg", { viewBox: "0 0 1000 660", role: "group", tabindex: "-1", "aria-label": "中国公开招聘行政区示意地图" });
  const mapMessage = element("p", "radar-map-message");
  stage.append(map); viewport.append(stage, mapMessage);
  const locations = element("div", "radar-map-location-list"); locations.setAttribute("aria-label", "本范围已定位地区与实际公开岗位数量");
  const precision = element("p", "radar-map-precision", "行政中心示意，非办公地址或导航坐标；城市与区县仅按公开招聘地点关联，不推断企业总部。");
  const attribution = element("p", "radar-map-attribution");
  const details = element("div", "radar-map-details"), detailsTitle = element("h4"), list = element("div", "radar-map-job-list");
  details.append(detailsTitle, list);
  root.append(heading, controls, viewStatus, breadcrumb, summary, viewport, locations, precision, attribution, details);
  host.replaceChildren(root);

  const emit = (id, kind, label) => onSelect(id ? model.nodes.get(id) || { id, kind, label } : null);
  const emitLocation = (id, label) => {
    if (id && (model.nodes.has(`location:${id}`) || model.places.has(id))) emit(`location:${id}`, "location", label);
    else emit(null);
  };
  const resetGeography = () => { state.province = ""; state.city = ""; state.place = ""; state.unlocated = false; };
  const activate = (node, callback) => {
    node.addEventListener("click", callback);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); callback(); map.focus?.(); }
    });
  };
  function selectJob(id, notify = true) {
    const job = model.jobs.find((candidate) => candidate.id === id);
    state.job = job?.id || "";
    if (job) state.employer = job.employerId;
    state.skill = ""; state.unlocated = false;
    render();
    if (notify) emit(job ? `opportunity:${job.id}` : state.employer, job ? "opportunity" : "employer", job?.title || model.companies.get(state.employer)?.name);
  }
  function drill(feature) {
    const properties = feature.properties;
    const id = text(properties.id);
    state.place = ""; state.unlocated = false;
    if (properties.level === "province") { state.province = id; state.city = ""; }
    else if (properties.level === "city") { state.province = text(properties.province_id) || state.province; state.city = id; }
    else { state.place = id; }
    render();
    emitLocation(id, properties.name);
  }
  function choosePlace(place, notify = true) {
    state.place = place.id; state.unlocated = false;
    render();
    if (notify) emit(`location:${place.id}`, "location", place.name);
  }
  function option(select, value, label) {
    const node = element("option", "", label); node.value = value; select.append(node);
  }
  function visibleFeatures() {
    if (!collection) return [];
    const scope = state.city || state.province;
    const children = [...index.values()].filter((feature) => text(feature.properties.parent_id) === scope);
    if (scope) {
      const parent = index.get(scope);
      if (children.length) return parent && children.every((feature) => feature.geometry.type === "Point") && parent.geometry.type !== "Point"
        ? [{ ...parent, mapContext: true }, ...children] : children;
      return parent ? [parent] : [];
    }
    return [...index.values()].filter((feature) => ["province", "auxiliary"].includes(feature.properties.level));
  }
  function renderBreadcrumb() {
    breadcrumb.replaceChildren();
    const crumbs = [{ id: "", label: "中国 · 含香港", level: "national" },
      ...[state.province, state.city].filter(Boolean).map((id) => ({ id, label: index.get(id)?.properties.name || id,
        level: index.get(id)?.properties.level }))];
    for (const crumb of crumbs) {
      const button = element("button", "radar-map-button", crumb.label); button.type = "button";
      button.addEventListener("click", () => {
        state.place = ""; state.unlocated = false;
        if (!crumb.id) { state.province = ""; state.city = ""; }
        else if (crumb.level === "province") { state.province = crumb.id; state.city = ""; }
        render();
        emitLocation(crumb.id, crumb.label);
      });
      breadcrumb.append(button);
    }
    if (state.place) breadcrumb.append(element("span", "radar-map-current-place", index.get(state.place)?.properties.name || model.places.get(state.place)?.name || state.place));
    const hongKong = [...index.values()].find((feature) => feature.properties.level === "province" && /香港/.test(feature.properties.name));
    if (hongKong && state.province !== text(hongKong.properties.id)) {
      const button = element("button", "radar-map-button radar-map-hong-kong", "香港放大"); button.type = "button";
      button.addEventListener("click", () => drill(hongKong)); breadcrumb.append(button);
    }
  }
  function renderMap(jobs) {
    map.replaceChildren();
    const features = visibleFeatures();
    mapMessage.hidden = features.length > 0 && !boundaryError;
    mapMessage.textContent = boundaryError ? "离线行政边界暂不可用；岗位列表仍可查看。" : "正在载入离线行政边界…";
    if (!features.length) return;
    const defs = svgElement("defs"), grid = svgElement("pattern", { id: `${prefix}-grid`, width: 44, height: 44, patternUnits: "userSpaceOnUse" });
    grid.append(svgElement("path", { d: "M 44 0 L 0 0 0 44", fill: "none", stroke: "currentColor", "stroke-width": 0.4 }));
    defs.append(grid); map.append(defs, svgElement("rect", { width: 1000, height: 660, fill: `url(#${prefix}-grid)`, class: "radar-map-grid" }));
    const labels = [], screenWidth = viewport.clientWidth || host.clientWidth || 1000;
    const project = createMapProjection(features), locations = radarMapPlaces(jobs).filter((place) =>
      within(place, state.city || state.province, index) && (!state.place || place.id === state.place));
    for (const feature of features) {
      const properties = feature.properties, id = text(properties.id);
      const count = jobs.filter((job) => job.places.some((place) => within(place, id, index))).length;
      if (feature.geometry.type === "Point") {
        const [x, y] = project(feature.geometry.coordinates);
        const point = svgElement("g", { class: `radar-map-admin-point${state.place === id ? " is-selected" : ""}`,
          role: "button", tabindex: "0", "data-region-id": id,
          "aria-label": `${properties.name}，行政区参考点，${count} 个公开岗位，无区县边界，查看下级或筛选` });
        point.append(svgElement("title", {}, `${properties.name} · 行政参考点（无区县边界） · ${count} 个公开岗位`),
          svgElement("circle", { cx: x, cy: y, r: 4, class: "radar-map-admin-dot" }));
        labels.push({ id: `admin:${id}`, text: properties.name, x: x + 9, y: y - 5, className: "radar-map-admin-label",
          parent: point, priority: state.place === id ? 100 : 1, fontSize: 13 });
        activate(point, () => drill(feature)); map.append(point); continue;
      }
      const auxiliary = properties.level === "auxiliary" || feature.mapContext;
      const path = svgElement("path", { d: boundaryPath(feature, project), "fill-rule": "evenodd", class: `radar-map-region${state.place === id ? " is-selected" : ""}`,
        ...(auxiliary ? { "aria-hidden": "true" } : { role: "button", tabindex: "0", "aria-label": `${properties.name}，${count} 个公开岗位，查看下级行政区` }), "data-region-id": id });
      path.append(svgElement("title", {}, `${properties.name} · ${count} 个公开岗位`));
      if (!auxiliary) activate(path, () => drill(feature)); map.append(path);
      const center = validPoint(properties.center);
      if (center) {
        const [x, y] = project(center);
        labels.push({ id: `region:${id}`, text: properties.name, x, y, className: "radar-map-region-label", anchor: "middle",
          parent: map, priority: 0, fontSize: screenWidth <= 600 ? 15 : 13 });
      }
    }
    for (const place of locations) {
      const point = radarMapDisplayPoint(place, index);
      if (!point) continue;
      const [x, y] = project(point);
      const selected = state.place === place.id;
      const marker = svgElement("g", { class: `radar-map-marker${selected ? " is-selected" : ""}`, role: "button", tabindex: "0",
        "aria-label": `${place.name}，${place.count} 个公开岗位，筛选岗位列表`, "aria-pressed": selected ? "true" : "false", "data-place-id": place.id });
      marker.append(svgElement("title", {}, `${place.name} · ${place.count} 个岗位 · 行政中心示意`),
        svgElement("circle", { cx: x, cy: y, r: 15, class: "radar-map-pulse", "aria-hidden": "true" }),
        svgElement("circle", { cx: x, cy: y, r: 6, class: "radar-map-dot" }));
      labels.push({ id: `marker:${place.id}`, text: `${place.name} · ${place.count}`, x: x + 12, y: y - 10,
        className: "radar-map-marker-count", parent: marker, priority: selected ? 100 : 10 + Math.min(place.count, 100) / 1000,
        fontSize: screenWidth <= 600 ? 18 : 14 });
      activate(marker, () => choosePlace(place)); map.append(marker);
    }
    for (const label of selectMapLabels(labels, screenWidth)) label.parent.append(svgElement("text", {
      x: label.x, y: label.y, class: label.className, "text-anchor": label.anchor, "aria-hidden": "true",
    }, label.text));
  }
  function renderLocations(jobs) {
    locations.replaceChildren();
    for (const place of radarMapPlaces(jobs).filter((place) => within(place, state.city || state.province, index)
      && (!state.place || place.id === state.place))) {
      const button = element("button", "radar-map-button", `${place.name} · ${place.count}`); button.type = "button";
      button.setAttribute("aria-label", `${place.name}，${place.count} 个公开岗位，筛选岗位列表`);
      button.setAttribute("aria-pressed", String(state.place === place.id));
      button.addEventListener("click", () => choosePlace(place)); locations.append(button);
    }
  }
  function renderList(jobs) {
    detailsTitle.textContent = `当前范围公开岗位 · ${state.snapshot ? jobs.length : "待载入"}`;
    list.replaceChildren();
    if (!jobs.length) {
      list.append(element("p", "radar-map-empty", !state.snapshot ? "公开岗位数据尚未载入；行政区可先浏览。"
        : model.jobs.length ? "当前筛选或行政区暂无公开招聘岗位，可返回全国或清除选择。" : "暂无可展示的公开招聘岗位；地图行政区仍可浏览。"));
      return;
    }
    for (const job of jobs) {
      const card = element("article", `radar-map-job${state.job === job.id ? " is-selected" : ""}`);
      const button = element("button", "radar-map-job-title", job.title); button.type = "button";
      button.setAttribute("aria-pressed", state.job === job.id ? "true" : "false");
      button.addEventListener("click", () => selectJob(job.id));
      const placeText = job.places.length ? [...new Set(job.places.map((place) => place.name))].join(" / ") : "无法定位（公开地点不足或存在歧义）";
      card.append(button, element("p", "radar-map-job-meta", `${job.employer} · ${placeText}`));
      if (job.location_status === "ambiguous") card.append(element("p", "radar-map-location-warning", "仅标示可确认行政范围，更具体地点待确认"));
      if (job.url) {
        const link = element("a", "radar-map-job-link", "公开招聘页面 ↗"); link.href = job.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        card.append(link);
      } else card.append(element("span", "radar-map-job-no-link", "暂无可用公开链接"));
      list.append(card);
    }
  }
  function render() {
    if (destroyed) return;
    const jobs = radarMapJobs(model, state, index), missing = model.jobs.filter((job) => !job.places.length).length;
    const ambiguous = model.jobs.filter((job) => job.location_status === "ambiguous").length;
    const motionEnabled = state.motionOverride ?? !media?.matches;
    root.className = `radar-map${state.mode === "3d" ? " radar-map-3d" : ""}${motionEnabled ? " radar-map-motion" : ""}${state.motionOverride === true ? " radar-map-motion-manual" : ""}`;
    root.dataset.snapshotStale = String(state.stale);
    stage.style.transform = state.mode === "3d" ? `rotateX(${state.pitch}deg) rotateZ(${state.yaw}deg) translateZ(0)` : "";
    employerSelect.replaceChildren(); option(employerSelect, "", "全部企业招聘分布");
    for (const company of [...model.companies.values()].sort((a, b) => a.name.localeCompare(b.name, "zh-CN"))) option(employerSelect, company.id, company.name);
    employerSelect.value = state.employer;
    jobSelect.replaceChildren(); option(jobSelect, "", "全部公开岗位");
    for (const job of model.jobs.filter((job) => !state.employer || job.employerId === state.employer)) option(jobSelect, job.id, `${job.title} · ${job.employer}`);
    jobSelect.value = state.job;
    skillSelect.replaceChildren(); option(skillSelect, "", "全部公开技能");
    const skills = [...new Set(model.jobs.flatMap((job) => job.skills))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    for (const skill of skills) option(skillSelect, skill, skill);
    skillSelect.value = state.skill;
    mode.setAttribute("aria-pressed", String(state.mode === "3d"));
    mode.textContent = state.mode === "3d" ? "平面地图" : "3D 地图";
    resetView.disabled = state.mode !== "3d";
    resetView.setAttribute("aria-disabled", String(state.mode !== "3d"));
    motion.setAttribute("aria-pressed", String(motionEnabled));
    motion.textContent = motionEnabled ? "关闭动效" : "开启动效";
    motion.title = media?.matches && state.motionOverride === null ? "系统偏好减少动态效果，默认关闭；点击可仅为本地图手动开启" : "切换本地图的缓慢微光与招聘点脉冲，不改变系统设置";
    viewStatus.textContent = `${state.mode === "3d" ? `3D 视图（可拖拽旋转，俯仰 ${Math.round(state.pitch)}°，方位 ${Math.round(state.yaw)}°）` : "平面视图"} · 动效${motionEnabled ? "已开启" : "已关闭"}${media?.matches && state.motionOverride === null ? "（遵循系统偏好，可手动开启）" : ""}${state.feedback ? ` · ${state.feedback}` : ""}${!model.jobs.length ? state.snapshot ? " · 暂无公开岗位，企业与岗位筛选暂无选项；行政区仍可点击" : " · 企业与岗位选项等待图谱载入；视图与行政区可先操作" : ""}`;
    const phase = state.loading ? "正在读取公开招聘数据 · " : state.stale ? "读取未成功 · " : "";
    summary.textContent = state.snapshot
      ? `${phase}${state.stale || state.loading ? "上次成功快照 · " : ""}本次图谱公开岗位 ${model.jobs.length} · 当前筛选 ${jobs.length} · 未定位岗位（全部）${missing} · 地点待确认（全部）${ambiguous}${state.skill ? ` · 技能 ${state.skill}` : ""}`
      : `${phase}暂无成功快照，可先浏览行政区`;
    precision.textContent = "行政中心示意，非办公地址或导航坐标；按行政区 ID 关联，边界源坐标系未声明。区县边界覆盖有限，无边界地区用参考点下钻，不推断企业总部。";
    attribution.replaceChildren();
    const sources = [...(adminCatalog?.sources || [])];
    if (collection?.source && collection?.license_url) sources.unshift({ name: "离线行政边界", license: collection.license, url: collection.license_url });
    const uniqueSources = new Map(sources.map((source) => [source.url, source]));
    for (const source of uniqueSources.values()) {
      const url = safeRadarMapUrl({ url: source.url });
      if (url) { const link = element("a", "", `${source.name} · ${source.license || "数据来源"}`); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; attribution.append(link); }
    }
    renderBreadcrumb(); renderMap(jobs); renderLocations(jobs); renderList(jobs);
  }
  employerSelect.addEventListener("change", () => {
    state.employer = employerSelect.value; state.job = ""; state.skill = ""; resetGeography(); render();
    emit(state.employer, "employer", model.companies.get(state.employer)?.name);
  });
  jobSelect.addEventListener("change", () => { resetGeography(); selectJob(jobSelect.value); });
  skillSelect.addEventListener("change", () => {
    state.skill = skillSelect.value; state.employer = ""; state.job = ""; resetGeography();
    state.feedback = state.skill ? `已筛选技能：${state.skill}` : "已恢复全部公开技能"; render();
    const node = [...model.nodes.values()].find((value) => value.kind === "skill" && fold(value.label) === fold(state.skill));
    if (state.skill) emit(node?.id || `skill:${fold(state.skill)}`, "skill", state.skill); else emit(null);
  });
  mode.addEventListener("click", () => { state.mode = state.mode === "2d" ? "3d" : "2d"; state.feedback = state.mode === "3d" ? "已切换为可旋转 3D 地图" : "已返回平面地图"; render(); });
  resetView.addEventListener("click", () => { state.yaw = -18; state.pitch = 56; state.feedback = "3D 视角已重置"; render(); });
  motion.addEventListener("click", () => { state.motionOverride = !(state.motionOverride ?? !media?.matches); state.feedback = "动效设置已切换"; render(); });
  clear.addEventListener("click", () => { state.employer = ""; state.job = ""; state.skill = ""; resetGeography(); state.feedback = "已清除全部筛选并返回全国"; render(); emit(null); });
  let drag = null;
  const updateRotation = (deltaX, deltaY) => {
    state.yaw = Math.max(-70, Math.min(70, state.yaw + deltaX * 0.22));
    state.pitch = Math.max(34, Math.min(68, state.pitch - deltaY * 0.18));
    state.feedback = "3D 视角已旋转";
    render();
  };
  viewport.addEventListener("pointerdown", (event) => {
    if (state.mode !== "3d" || event.button && event.button !== 0) return;
    drag = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    viewport.setPointerCapture?.(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag || state.mode !== "3d") return;
    updateRotation(event.clientX - drag.x, event.clientY - drag.y);
    drag = { ...drag, x: event.clientX, y: event.clientY };
  });
  viewport.addEventListener("pointerup", (event) => {
    if (!drag) return;
    viewport.releasePointerCapture?.(event.pointerId);
    drag = null;
  });
  viewport.addEventListener("keydown", (event) => {
    if (state.mode !== "3d") return;
    const deltas = { ArrowLeft: [-18, 0], ArrowRight: [18, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] }[event.key];
    if (!deltas) return;
    event.preventDefault?.();
    updateRotation(deltas[0], deltas[1]);
  });
  const mediaChanged = () => render(); media?.addEventListener?.("change", mediaChanged);
  const ResizeObserverClass = document.defaultView?.ResizeObserver;
  let observedWidth = 0;
  const resizeObserver = ResizeObserverClass ? new ResizeObserverClass((entries) => {
    const width = entries[0]?.contentRect?.width;
    if (!destroyed && Number.isFinite(width) && Math.abs(width - observedWidth) > 1) {
      observedWidth = width; renderMap(radarMapJobs(model, state, index));
    }
  }) : null;
  resizeObserver?.observe(viewport);
  render();
  const boundaryReady = Promise.resolve().then(() => boundaries || boundaryLoader()).then((value) => {
    if (!destroyed) { collection = value; index = createAdministrativeIndex(value, adminCatalog); boundaryError = !createBoundaryIndex(value).size; render(); }
    return !destroyed;
  }).catch(() => { if (!destroyed) { boundaryError = true; render(); } return false; });
  const catalogReady = Promise.resolve().then(() => catalog || (boundaries ? { places: [] } : catalogLoader())).then((value) => {
    if (!destroyed) { adminCatalog = value; index = createAdministrativeIndex(collection, value); render(); }
  }).catch(() => { /* A missing optional point catalog must not erase valid polygons or public jobs. */ });
  const ready = Promise.all([boundaryReady, catalogReady]).then(([loaded]) => loaded && !destroyed);
  return {
    ready,
    update(graph) {
      if (destroyed) return;
      if (graph?.status === "loading") { state.loading = true; render(); return; }
      state.loading = false;
      if (!graph || (graph.status && graph.status !== "synced") || !Array.isArray(graph.items)) { state.stale = true; render(); return; }
      model = normalizeRadarMapGraph(graph); state.stale = false; state.snapshot = true;
      if (!model.companies.has(state.employer)) state.employer = "";
      if (!model.jobs.some((job) => job.id === state.job)) state.job = "";
      if (!model.jobs.some((job) => job.skills.some((skill) => fold(skill) === fold(state.skill)))) state.skill = "";
      if (state.place && !model.places.has(state.place) && !index.has(state.place)) state.place = "";
      render();
    },
    selectNode(node) {
      if (destroyed) return;
      if (!node) { state.employer = ""; state.job = ""; state.skill = ""; resetGeography(); render(); return; }
      const id = text(node.id), kind = node.kind || id.split(":")[0];
      if (kind === "employer") {
        const company = model.companies.get(id) || [...model.companies.values()].find((value) => fold(value.name) === fold(node.label));
        if (company) { state.employer = company.id; state.job = ""; state.skill = ""; resetGeography(); render(); }
      } else if (kind === "opportunity") { resetGeography(); selectJob(id.replace(/^opportunity:/, ""), false); }
      else if (kind === "location") {
        const placeId = id.replace(/^location:/, "");
        const place = model.places.get(placeId) || node.place || index.get(placeId)?.properties;
        if (place) {
          state.employer = ""; state.job = ""; state.skill = ""; resetGeography();
          if (place.level === "province") state.province = placeId;
          else if (place.level === "city") { state.province = text(place.province_id); state.city = placeId; }
          else { state.province = text(place.province_id); state.city = place.city_id !== place.province_id ? text(place.city_id) : ""; state.place = placeId; }
          render();
        }
      } else if (kind === "skill") {
        state.skill = text(node.label) || id.replace(/^skill:/, ""); state.employer = ""; state.job = ""; resetGeography(); render();
      }
    },
    destroy() { destroyed = true; media?.removeEventListener?.("change", mediaChanged); resizeObserver?.disconnect(); host.replaceChildren(); },
  };
}
