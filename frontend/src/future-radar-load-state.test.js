import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {
  DEFAULT_FUTURE_RADAR_STATUS, FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS, TIER_CODES, buildFutureRadarJobsQuery,
  filterJobsByStarfields, futureRadarOpportunityDateCopy,
  futureRadarOpportunityErrorCopy, futureRadarOpportunitySource,
  futureRadarPublicOpportunityUrl, jobTierBucket, partitionJobsByPriority,
} from "./recruitment-radar.js";

const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
function extract(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start);
  return source.slice(start, end);
}

class Element {
  constructor(tag = "div", className = "", text = "") {
    this.tag = tag;
    this.className = className;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this._text = text;
    const update = (name, enabled) => {
      const values = new Set(this.className.split(/\s+/).filter(Boolean));
      if (enabled) values.add(name); else values.delete(name);
      this.className = [...values].join(" ");
    };
    this.classList = { add: (name) => update(name, true), remove: (name) => update(name, false), toggle: update };
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setAttribute(name, value) { this[name] = value; }
  get textContent() { return this._text + this.children.map((node) => typeof node === "string" ? node : node.textContent).join(" "); }
  set textContent(value) { this._text = String(value); this.children = []; }
  scrollIntoView() {}
  close() { this.open = false; }
}
function descendants(node) {
  return [node, ...node.children.flatMap((child) => child instanceof Element ? descendants(child) : [])];
}
function pendingJob(id) {
  return { id, company: "示例企业", title: `2027校园招聘数据分析师 ${id}`, city: "上海",
    status: "unknown", verification_status: "pending", tier_code: null,
    primary_category: "internet_tech", source_id: "chatgpt-radar-01",
    official_url: `https://careers.example.com/campus/${id}`, tags: ["校园招聘"] };
}
function runtime({ existing = false, fail = true, legacyFail = false } = {}) {
  const controls = { fail, legacyFail };
  const calls = [];
  const requestOptions = [];
  const payload = { items: Array.from({ length: 50 }, (_, i) => pendingJob(`pending-${i}`)), total: 255,
    page: 1, page_size: 50, stats: { verification_status: { pending: 255, verified: 0, conflicted: 0 },
      job_status: { unknown: 255 }, tier_counts: { UNRANKED: 255 }, category_counts: { internet_tech: 255 } } };
  const oldJobs = Array.from({ length: 6 }, (_, i) => pendingJob(`legacy-${i}`));
  const state = { token: Symbol("pure-state-session"), music: { enabled: false }, recruitmentJobs: oldJobs, recruitmentWatches: [], recruitmentTierFilter: "ALL", futureRadar: {
    jobsLoaded: existing, jobsError: "", jobs: existing ? [pendingJob("saved-main")] : [],
    jobsLoading: false, loading: false, jobsRequestId: 0, page: 1, pageSize: 50,
    totalJobs: existing ? 1 : 0,
    opportunityStats: existing ? { tier_counts: { UNRANKED: 1 }, verification_status: { pending: 1 } } : {},
    filters: { status: DEFAULT_FUTURE_RADAR_STATUS },
    sources: [], runs: [], events: [], activeRunTypes: new Set(), polling: false,
    searchScope: {}, searchCoverage: null,
    runStatusPollTimer: { quick: 1, deep: 2 }, runDelayTimer: { quick: 3, deep: 4 },
    runDelayUntil: { quick: 0, deep: 0 }, runStarting: { quick: false, deep: false },
  } };
  const elements = Object.fromEntries([
    "recruitmentJobs", "recruitmentError", "recruitmentStatus", "futureRadarLoading", "futureRadarError",
    "futureRadarLiveState", "futureRadarOpportunitySummary", "futureRadarOpportunityCount",
    "futureRadarDashboard", "futureRadarLastScan", "futureRadarLastSuccess", "futureRadarSourceHealth",
    "futureRadarFilterStatus", "futureRadarFilterEvent", "futureRadarFilterVerification", "futureRadarEvents",
    "recruitmentRoles", "recruitmentIndustries", "recruitmentLocations",
    "settingsDialog", "consentDialog", "worldMapDialog", "appView", "authView", "password",
  ].map((name) => [name, new Element()]));
  elements.recruitmentDialog = new Element("dialog");
  elements.recruitmentDialog.open = true;
  elements.futureRadarFilterForm = { reset() {} };
  const noop = () => {};
  const context = {
    state, elements, DEFAULT_FUTURE_RADAR_STATUS, FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS, TIER_CODES, buildFutureRadarJobsQuery,
    futureRadarOpportunityDateCopy, futureRadarOpportunityErrorCopy, futureRadarOpportunitySource,
    futureRadarPublicOpportunityUrl, jobTierBucket, partitionJobsByPriority,
    document: { hidden: false, querySelectorAll: () => [], createElement: (tag) => new Element(tag), createTextNode: (text) => text },
    makeElement: (tag, className = "", text = "") => new Element(tag, className, text),
    api: async (path, options) => {
      calls.push(path);
      requestOptions.push(options);
      if (path.startsWith("/future-radar/opportunities?")) {
        if (controls.fail) throw Object.assign(new Error("private provider diagnostics must not render"), { status: 500 });
        return payload;
      }
      if (path === "/recruitment/jobs") {
        if (controls.legacyFail) throw new Error("legacy unavailable");
        return { jobs: oldJobs, monitor_pools: [], data_status: { message: "当前筛选 6 个岗位；待核验不进入主池。" } };
      }
      if (path.startsWith("/future-radar/opportunities/")) {
        if (controls.detailFail) throw new Error("detail unavailable");
        return { ...pendingJob(decodeURIComponent(path.split("/").at(-1))),
          sources: [{ name: "公开招聘页", source_url: "https://careers.example.com/campus/source" }],
          discovered_by: [{ source_id: "chatgpt-radar-01" }] };
      }
      if (path === "/recruitment/watches") return { watches: [] };
      if (path === "/recruitment/profile") return {};
      if (path === "/future-radar/dashboard") return { total_jobs: 6, new: 265, pending: 253, verified: 11 };
      return { items: [] };
    },
    radarCollection: (value) => value?.items || [],
    radarNumber: (value, keys, fallback = 0) => keys.map((key) => value?.[key]).find((v) => v != null) ?? fallback,
    selectedRecruitmentStarfields: () => [],
    filterRecruitmentByStarfield: (jobs) => filterJobsByStarfields(jobs, []),
    recruitmentJobUrl: futureRadarPublicOpportunityUrl,
    recruitmentDaysLeft: () => null,
    recruitmentScoringFactors: () => [],
    radarStatusCopy: (status) => status,
    formatRadarTime: () => "待记录",
    eventIdentity: (event) => event.id,
    chatgptSyncFromJobs: () => null,
    translateError: (message) => message,
    valueAtPaths: () => null,
    radarStatusClass: () => "pending",
    futureRadarActiveRunTypes: () => [],
    futureRadarProfileReload: { request() {} },
    FUTURE_RADAR_SCAN_TYPES: ["quick", "deep"],
    FUTURE_RADAR_REQUEST_CONTROLLERS: new Set(),
    FUTURE_RADAR_POLL_INTERVAL_MS: 30_000,
    soundscapeEngine: { async destroy() {} },
    storage: { async remove() {} },
    STORAGE_KEYS: { token: "local-test-only" },
    splitRecruitmentValues: () => [],
    setTimeout: () => null,
    window: { clearTimeout() {}, clearInterval() {}, setInterval() { throw new Error("Unexpected timer start in test"); } },
  };
  for (const name of ["renderFutureRadarPagination", "syncFutureRadarProgramFilter",
    "renderFutureRadarPrograms", "mergeFutureRadarEvents", "syncFutureRadarSourceFilter",
    "renderFutureRadarSources", "renderFutureRadarRuns", "renderRecruitmentDeadlineAlerts", "renderRecruitmentProfile",
    "renderRecruitmentWatches", "renderHomeRecruitmentAlerts", "renderRecruitmentMonitors", "renderRecruitmentSyncStatus",
    "renderFutureRadarRunAvailability", "applyIncrementalRadarMetrics", "addRecruitmentWatchFromJob", "showToast", "renderMusicUI"]) context[name] = noop;
  vm.createContext(context);
  const functions = [
    "let recruitmentAutoFilterTimer = null;",
    extract("function endFutureRadarSession(", "\nasync function loadWorkspaces"),
    extract("function stopFutureRadarRunStatusPolling(", "\nfunction scheduleFutureRadarRunStatusPoll"),
    extract("function stopFutureRadarPolling(", "\nasync function runFutureRadarNow"),
    extract("function setRecruitmentStatus(", "\nfunction valueAtPaths"),
    extract("function activateFutureRadarTab(", "\nfunction setFutureRadarLoading"),
    extract("function setFutureRadarLoading(", "\nfunction mergeFutureRadarEvents"),
    extract("function renderFutureRadarDashboard(", "\nfunction renderFutureRadarPrograms"),
    extract("function eventTimestamp(", "\nfunction renderFutureRadarSources"),
    extract("function recordFutureRadarOpportunityFailure(", "\nfunction syncFutureRadarSourceFilter"),
    extract("function resetFutureRadarFilters(", "\nfunction applyIncrementalRadarMetrics"),
    extract("async function loadFutureRadarSnapshot(", "\nfunction stopFutureRadarPolling"),
    extract("function recruitmentVerification(", "\nfunction recruitmentScoringFactors"),
    extract("function selectRecruitmentTier(", "\nasync function addRecruitmentWatchFromJob"),
    extract("async function refreshRecruitment(", "\nasync function refreshRecruitmentSource"),
    extract("async function saveRecruitment(", "\nfunction scheduleRecruitmentAutoFilter"),
  ].join("\n");
  vm.runInContext(functions, context);
  return { controls, calls, requestOptions, payload, state, elements, context,
    run: (expression) => vm.runInContext(expression, context),
    cards: () => descendants(elements.recruitmentJobs).filter((el) => el.className === "recruitment-job-card") };
}

test("first main-pool failure never renders six legacy rows and explicitly offers retry", async () => {
  const r = runtime();
  assert.equal(await r.run("refreshRecruitment()"), null);
  assert.equal(r.state.futureRadar.jobsLoaded, false);
  assert.equal(r.cards().length, 0);
  assert.match(r.elements.recruitmentJobs.textContent, /主机会池加载失败/);
  assert.match(r.elements.futureRadarError.textContent, /HTTP 500/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /legacy-|private provider/);
  assert.equal(r.elements.futureRadarOpportunityCount.textContent, "—");
  assert.match(r.elements.recruitmentStatus.textContent, /主机会池加载失败（HTTP 500）/);
  assert.doesNotMatch(r.elements.recruitmentStatus.textContent, /当前筛选 6|待核验不进入/);
  const retry = descendants(r.elements.recruitmentJobs).find((el) => el.tag === "button");
  assert.equal(retry.textContent, "重试加载主机会池");
  r.controls.fail = false;
  await retry.listeners.click();
  assert.equal(r.state.futureRadar.jobsLoaded, true);
  assert.equal(r.state.futureRadar.jobsError, "");
  assert.equal(r.cards().length, 50);
  assert.match(r.elements.recruitmentStatus.textContent, /当前筛选 255 个机会/);
});

test("a partial dashboard snapshot is not successful when opportunities failed", async () => {
  const r = runtime();
  assert.equal(await r.run("loadFutureRadarSnapshot()"), false);
  assert.equal(r.cards().length, 0);
  assert.equal(r.state.futureRadar.jobsLoading, false);
});

test("refresh failure preserves the last successful unified snapshot even after filter changes", async () => {
  const r = runtime({ existing: true });
  const saved = r.state.futureRadar.jobs;
  r.state.recruitmentTierFilter = "T0";
  assert.equal(await r.run("loadFutureRadarSnapshot()"), false);
  assert.equal(r.state.futureRadar.jobs, saved);
  assert.equal(r.cards().length, 1);
  assert.match(r.elements.recruitmentJobs.textContent, /上次成功/);
  assert.match(r.elements.recruitmentStatus.textContent, /主机会池刷新失败.*上次成功/);
  assert.match(r.elements.recruitmentJobs.textContent, /saved-main/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /legacy-/);
  const queryIndex = r.calls.findIndex((path) => path.startsWith("/future-radar/opportunities?"));
  assert.equal(r.requestOptions[queryIndex].timeoutMs, 45_000);
});

test("profile-save legacy response cannot overwrite unified-pool totals or failure state", async () => {
  const r = runtime({ fail: false });
  await r.run("loadFutureRadarSnapshot()");
  await r.run("saveRecruitment(null, {silent: true})");
  assert.match(r.elements.recruitmentStatus.textContent, /当前筛选 255 个机会.*待核验 255/);
  assert.doesNotMatch(r.elements.recruitmentStatus.textContent, /当前筛选 6|待核验不进入/);
  r.controls.fail = true;
  await r.run("loadFutureRadarSnapshot()");
  await r.run("saveRecruitment(null, {silent: true})");
  assert.match(r.elements.recruitmentStatus.textContent, /主机会池刷新失败/);
});

test("NEW and DISCOVERED metrics are keyboard-native buttons filtering the unified pool", async () => {
  const r = runtime({ fail: false });
  await r.run("loadFutureRadarSnapshot()");
  const buttons = descendants(r.elements.futureRadarDashboard).filter((element) => element.tag === "button");
  assert.equal(buttons.length, 3);
  const discovered = buttons.find((button) => button.className.includes("metric-discovered"));
  assert.equal(discovered.type, "button");
  await discovered.listeners.click();
  let query = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(query.get("verification_status"), "pending");
  assert.equal(query.get("status"), "active");
  assert.equal(query.has("event_type"), false);
  assert.equal(r.elements.futureRadarFilterVerification.value, "pending");
  assert.equal(r.state.futureRadar.activeTab, "jobs");
  const fresh = buttons.find((button) => button.className.includes("metric-new"));
  assert.match(fresh.textContent, /近7天新增记录/);
  assert.match(fresh.title, /来源记录.*去重/);
  assert.match(html, /机会池会合并重复并按条件筛选，与指标数量可能不同/);
  await fresh.listeners.click();
  query = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(query.get("event_type"), "NEW");
  assert.equal(query.has("verification_status"), false);
  assert.equal(query.get("status"), "active");
});

test("pending cards open the unified detail API and expose safe ordinary source anchors", async () => {
  const r = runtime({ fail: false });
  await r.run("loadFutureRadarSnapshot()");
  const card = r.cards()[0];
  const cardLink = descendants(card).find((node) => node.tag === "a");
  assert.equal(cardLink.href, "https://careers.example.com/campus/pending-0");
  assert.match(cardLink.textContent, /招聘线索/);
  const detail = descendants(card).find((node) => node.dataset.opportunityDetail === "pending-0");
  detail.open = true;
  await detail.listeners.toggle();
  const index = r.calls.findIndex((path) => path === "/future-radar/opportunities/pending-0");
  assert.ok(index >= 0);
  assert.equal(r.requestOptions[index].timeoutMs, 45_000);
  const links = descendants(detail).filter((node) => node.tag === "a");
  assert.ok(links.some((link) => link.href === cardLink.href));
  assert.ok(links.some((link) => link.href === "https://careers.example.com/campus/source"));
  assert.ok(links.every((link) => link.target === "_blank" && link.rel === "noreferrer"));
  assert.doesNotMatch(detail.textContent, /官网已确认/);
});

test("NEW event source links are clickable only when an actual safe public URL exists", () => {
  const r = runtime({ fail: false });
  r.state.futureRadar.events = [
    { id: 1, event_type: "NEW", after_data: { ...pendingJob("new"), official_url: "https://careers.example.com/new" } },
    { id: 2, event_type: "NEW", after_data: { ...pendingJob("private"), official_url: "https://chatgpt.com/c/not-public" } },
    { id: 3, event_type: "NEW", after_data: { ...pendingJob("unsafe"), official_url: "javascript:alert(1)" } },
  ];
  r.run("renderFutureRadarEvents()");
  const links = descendants(r.elements.futureRadarEvents).filter((node) => node.tag === "a");
  assert.equal(links.length, 1);
  assert.equal(links[0].href, "https://careers.example.com/new");
});

test("expired logout closes radar and cancels in-flight reads before waiting for audio cleanup", async () => {
  const r = runtime({ existing: true, fail: false });
  let finishAudioCleanup;
  r.context.soundscapeEngine.destroy = () => new Promise((resolve) => { finishAudioCleanup = resolve; });
  const inFlight = new AbortController();
  r.context.FUTURE_RADAR_REQUEST_CONTROLLERS.add(inFlight);
  r.state.futureRadar.pollingTimer = 42;
  const stopped = [];
  r.context.window.clearInterval = (id) => stopped.push(id);
  const logout = r.run("logout(false)");
  assert.equal(r.state.token, null);
  assert.equal(r.elements.recruitmentDialog.open, false);
  assert.ok(stopped.includes(42));
  assert.equal(r.state.futureRadar.pollingTimer, null);
  assert.equal(r.state.futureRadar.runStatusPollTimer.quick, null);
  assert.equal(r.state.futureRadar.runStatusPollTimer.deep, null);
  assert.equal(r.state.futureRadar.jobsRequestId, 1);
  assert.equal(r.state.futureRadar.jobsLoaded, false);
  assert.equal(r.state.futureRadar.jobs.length, 0);
  assert.equal(inFlight.signal.aborted, true);
  assert.equal(inFlight.signal.reason.status, 401);
  assert.equal(inFlight.signal.reason.code, "AUTH_REQUIRED");
  assert.match(r.state.futureRadar.jobsError, /HTTP 401.*请重新登录/);
  assert.doesNotMatch(r.state.futureRadar.jobsError, /刷新机会/);
  finishAudioCleanup();
  await logout;
  assert.match(r.elements.appView.className, /hidden/);
  assert.doesNotMatch(r.elements.authView.className, /hidden/);
});

test("a missing session prevents radar snapshot, page read, compatibility reads and polling", async () => {
  const r = runtime({ fail: false });
  r.state.token = null;
  // Even a stale open overlay may not trigger another request after expiry.
  r.elements.recruitmentDialog.open = true;
  await r.run("loadFutureRadarSnapshot()");
  await r.run("loadFutureRadarJobPage(1, true)");
  await r.run("refreshRecruitment()");
  await r.run("pollFutureRadarEvents()");
  r.run("startFutureRadarPolling()");
  assert.equal(r.calls.length, 0);
});

test("active main pool renders pending unknown rows with the complete backend count", async () => {
  const r = runtime({ fail: false });
  assert.equal(await r.run("loadFutureRadarSnapshot()"), true);
  const request = r.calls.find((path) => path.startsWith("/future-radar/opportunities?"));
  assert.equal(new URLSearchParams(request.split("?")[1]).get("status"), "active");
  assert.equal(r.state.futureRadar.totalJobs, 255);
  assert.equal(r.elements.futureRadarOpportunityCount.textContent, "255");
  assert.match(r.elements.futureRadarOpportunitySummary.textContent, /聊天 \/ 搜索发现 255/);
  assert.equal(r.state.futureRadar.opportunityStats.category_counts.internet_tech, 255);
  assert.equal(r.cards().length, 50);
  assert.ok(r.state.futureRadar.jobs.every((job) => job.status === "unknown" && job.tier_code == null));
  assert.match(r.elements.recruitmentJobs.textContent, /当前筛选共 255 个机会/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /legacy-/);
});

test("opportunity polling failure remains visible even while event polling succeeds", async () => {
  const r = runtime({ existing: true });
  await r.run("pollFutureRadarEvents()");
  assert.match(r.elements.futureRadarLiveState.className, /warning/);
  assert.match(r.elements.futureRadarError.textContent, /主机会池刷新失败/);
  assert.equal(r.cards().length, 1);
  const card = r.cards()[0];
  await r.run("pollFutureRadarEvents()");
  assert.equal(r.cards()[0], card);
  r.controls.fail = false;
  await r.run("pollFutureRadarEvents()");
  assert.equal(r.state.futureRadar.jobsError, "");
  assert.equal(r.elements.futureRadarError.textContent, "");
  assert.match(r.elements.futureRadarLiveState.className, /healthy/);
  assert.match(r.elements.futureRadarLiveState.textContent, /主机会池已恢复/);
  assert.equal(r.cards().length, 50);
});

test("legacy profile compatibility failure cannot block a successful main pool read", async () => {
  const r = runtime({ fail: false, legacyFail: true });
  await r.run("refreshRecruitment()");
  assert.equal(r.state.futureRadar.jobsLoaded, true);
  assert.equal(r.cards().length, 50);
  assert.equal(r.state.futureRadar.totalJobs, 255);
});

test("reset and the HTML default use active without widening to closed opportunities", async () => {
  const r = runtime({ fail: false });
  r.state.futureRadar.filters.status = "closed";
  const requested = [];
  r.context.loadFutureRadarJobPage = (page) => requested.push({ page, status: r.state.futureRadar.filters.status });
  r.run("resetFutureRadarFilters()");
  assert.deepEqual(requested, [{ page: 1, status: "active" }]);
  assert.match(html, /id="future-radar-filter-status"><option value="active">全部有效机会（含待核验）/);
  assert.match(html, /value="all">全部（含已关闭）/);
  assert.equal(DEFAULT_FUTURE_RADAR_STATUS, "active");
});
