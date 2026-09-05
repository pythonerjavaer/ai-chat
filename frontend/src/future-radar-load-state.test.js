import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createRadarPollingGate } from "./radar-polling.js";
import {
  DEFAULT_FUTURE_RADAR_STATUS, FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS, TIER_CODES, buildFutureRadarJobsQuery,
  buildFutureRadarCompanyJobsQuery, starfieldLabel,
  filterJobsByStarfields, futureRadarOpportunityDateCopy,
  futureRadarOpportunityErrorCopy, futureRadarOpportunitySource, futureRadarOriginalRating,
  futureRadarPublicOpportunityUrl, jobTierBucket, partitionJobsByPriority,
  futureRadarTierQuery, futureRadarVisibleCategoryCount,
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
  const timers = [];
  const payload = { items: Array.from({ length: 50 }, (_, i) => pendingJob(`pending-${i}`)), total: 255,
    page: 1, page_size: 50, stats: { verification_status: { pending: 255, verified: 0, conflicted: 0 },
      job_status: { unknown: 255 }, tier_counts: { UNRANKED: 255 }, category_counts: { internet_tech: 255 },
      balanced_total: 255, priority_total: 255, matching_total: 255, secondary_total: 0 } };
  const oldJobs = Array.from({ length: 6 }, (_, i) => pendingJob(`legacy-${i}`));
  const state = { token: Symbol("pure-state-session"), music: { enabled: false }, recruitmentJobs: oldJobs, recruitmentWatches: [], recruitmentTierFilter: "BALANCED", futureRadar: {
    jobsLoaded: existing, jobsError: "", jobs: existing ? [pendingJob("saved-main")] : [],
    jobsLoading: false, loading: false, jobsRequestId: 0, page: 1, pageSize: 50,
    jobsRequestQuery: "", jobsRequestController: null, jobsRequestPromise: null,
    jobsAppliedQuery: "", jobsAppliedTier: "BALANCED", jobsAppliedPage: 1,
    pollOpportunityController: null, snapshotRequestId: 0,
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
    "futureRadarPagination", "futureRadarPagePrev", "futureRadarPageNext", "futureRadarPageStatus",
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
    AbortController, URLSearchParams,
    radarPollingGate: createRadarPollingGate({ read: () => null, write() {}, locks: () => null }),
    radarOpportunityPollingGate: createRadarPollingGate({ read: () => null, write() {}, locks: () => null }),
    resumeFutureRadarRunStatusPolling() {},
    state, elements, DEFAULT_FUTURE_RADAR_STATUS, FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS, TIER_CODES, buildFutureRadarJobsQuery,
    buildFutureRadarCompanyJobsQuery, starfieldLabel,
    futureRadarOpportunityDateCopy, futureRadarOpportunityErrorCopy, futureRadarOpportunitySource, futureRadarOriginalRating,
    futureRadarPublicOpportunityUrl, jobTierBucket, partitionJobsByPriority,
    futureRadarTierQuery, futureRadarVisibleCategoryCount,
    document: { hidden: false, querySelectorAll: () => [], createElement: (tag) => new Element(tag), createTextNode: (text) => text },
    makeElement: (tag, className = "", text = "") => new Element(tag, className, text),
    api: async (path, options) => {
      calls.push(path);
      requestOptions.push(options);
      const custom = controls.apiHandler?.(path, options);
      if (custom !== undefined) return custom;
      if (path.startsWith("/future-radar/opportunities?")) {
        if (controls.opportunityHandler) return controls.opportunityHandler(path, options);
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
    setTimeout: (callback, delay) => {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout: (timer) => { if (timer) timer.cleared = true; },
    window: { clearTimeout() {}, clearInterval() {}, setInterval() { throw new Error("Unexpected timer start in test"); } },
  };
  for (const name of ["renderFutureRadarPagination", "syncFutureRadarProgramFilter",
    "renderFutureRadarPrograms", "mergeFutureRadarEvents", "syncFutureRadarSourceFilter",
    "renderFutureRadarSources", "renderFutureRadarRuns", "renderRecruitmentDeadlineAlerts", "renderRecruitmentProfile",
    "renderRecruitmentWatches", "renderHomeRecruitmentAlerts", "renderRecruitmentMonitors", "renderRecruitmentSyncStatus",
    "renderFutureRadarRunAvailability", "applyIncrementalRadarMetrics", "addRecruitmentWatchFromJob", "showToast", "renderMusicUI"]) context[name] = noop;
  vm.createContext(context);
  context.readFutureRadarDashboard = () => context.api("/future-radar/dashboard");
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
    extract("function renderFutureRadarPagination(", "\nfunction syncFutureRadarSourceFilter"),
    extract("function resetFutureRadarFilters(", "\nfunction applyIncrementalRadarMetrics"),
    extract("async function loadFutureRadarSnapshot(", "\nfunction stopFutureRadarPolling"),
    extract("function recruitmentVerification(", "\nfunction recruitmentScoringFactors"),
    extract("function selectRecruitmentTier(", "\nasync function addRecruitmentWatchFromJob"),
    extract("async function refreshRecruitment(", "\nasync function refreshRecruitmentSource"),
    extract("async function saveRecruitment(", "\nfunction scheduleRecruitmentAutoFilter"),
  ].join("\n");
  vm.runInContext(functions, context);
  return { controls, calls, requestOptions, payload, state, elements, context, timers,
    run: (expression) => vm.runInContext(expression, context),
    flushSelection: async () => {
      await new Promise(setImmediate);
      timers.filter((timer) => !timer.cleared && timer.delay === 140).forEach((timer) => timer.callback());
      await new Promise(setImmediate);
    },
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

test("the visible empty-pool retry resumes a persisted suspension without resuming metadata or creating duplicate reads", async () => {
  const r = runtime();
  await r.run("loadFutureRadarJobPage(1, true)");
  let opportunityTransport = JSON.stringify({ suspended: true, failures: 5, retryAt: Date.now() + 240_000 });
  r.context.radarOpportunityPollingGate = createRadarPollingGate({
    read: () => opportunityTransport, write: (value) => { opportunityTransport = value; }, locks: () => null,
  });
  r.context.radarPollingGate.failure({ status: 503 });
  let finish;
  r.controls.fail = false;
  r.controls.opportunityHandler = () => {
    r.context.radarOpportunityPollingGate.assertAllowed();
    return new Promise((resolve) => { finish = resolve; });
  };
  const retry = descendants(r.elements.recruitmentJobs).find((el) => el.tag === "button");
  assert.equal(retry.className, "radar-pool-retry");
  assert.notEqual(retry.disabled, true);
  const before = r.calls.length;
  const first = retry.listeners.click();
  const second = retry.listeners.click();
  await new Promise(setImmediate);
  assert.equal(r.calls.length, before + 1);
  finish(r.payload);
  assert.equal(await first, true);
  assert.equal(await second, true);
  assert.equal(r.state.futureRadar.jobsError, "");
  assert.equal(r.cards().length, 50);
  assert.throws(() => r.context.radarPollingGate.assertAllowed(), { code: "RADAR_POLL_DEFERRED" });
  assert.equal(JSON.parse(opportunityTransport).suspended, false);
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
  assert.equal(r.requestOptions[queryIndex].timeoutMs, 120_000);
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

test("job cards identify ChatGPT original ratings independently of official verification and system scores", () => {
  const r = runtime();
  r.context.testJob = { ...pendingJob("rated"), verification_status: "verified", tier_code: "T0.5", job_score: 91,
    system_tier_code: "T2", system_job_score: 73, raw_job_score: 73, rating_source: "chatgpt", rating_status: "applied",
    calibration_adjustment: null, calibration_reason: "岗位职责符合研究方向",
    source_rating: { scope: "job", tier_code: "T0.5", score: 91, source_id: "chatgpt-radar-09", reason: "岗位职责符合研究方向" } };
  const card = r.run("createRecruitmentJobCard(testJob)");
  const rank = descendants(card).find((element) => element.className === "job-rank");
  assert.equal(rank.textContent, "91 分 T0.5");
  assert.match(card.textContent, /ChatGPT 岗位原评：T0.5 · 91\/100/);
  assert.match(card.textContent, /原评依据：岗位职责符合研究方向/);
  assert.match(card.textContent, /系统 T 级参考 · T2/);
  assert.match(card.textContent, /系统分数参考 · 73/);
  assert.match(card.textContent, /原评与系统参考对照/);
  assert.doesNotMatch(card.textContent, /校准说明：|CALIBRATION · 0/);
  assert.match(card.textContent, /官网已确认/);
  assert.match(card.textContent, /不代表官网核验/);
});

test("partial original ratings do not invent a missing source tier or score", () => {
  const r = runtime();
  r.context.testJob = { ...pendingJob("partial-rating"), tier_code: null, job_score: 0,
    rating_status: "applied", rating_source: "chatgpt", source_rating: { scope: "job", score: 0, source_id: "chatgpt-radar-07" } };
  let card = r.run("createRecruitmentJobCard(testJob)");
  let rank = descendants(card).find((element) => element.className === "job-rank");
  assert.equal(rank.textContent, "0 分 原评未标 T 级");
  assert.match(card.textContent, /ChatGPT 岗位原评：0\/100/);
  r.context.testJob = { ...r.context.testJob, tier_code: "T1", job_score: null,
    source_rating: { scope: "job", tier_code: "T1", source_id: "chatgpt-radar-07" } };
  card = r.run("createRecruitmentJobCard(testJob)");
  rank = descendants(card).find((element) => element.className === "job-rank");
  assert.equal(rank.textContent, "已评分 T1");
  assert.doesNotMatch(card.textContent, /ChatGPT 岗位原评：T1 · \d/);
});

test("company references and differing original ratings stay separate from the displayed job rating", () => {
  const r = runtime();
  r.context.testJob = { ...pendingJob("company-reference"), listing_kind: "recruitment_program",
    rating_status: "company_reference", rating_source: "chatgpt",
    source_rating: { scope: "company", tier_code: "T0", score: 95, source_id: "chatgpt-radar-01" } };
  let card = r.run("createRecruitmentJobCard(testJob)");
  let rank = descendants(card).find((element) => element.className === "job-rank");
  assert.equal(rank.textContent, "未评分");
  assert.match(card.textContent, /ChatGPT 公司原评：T0 · 95\/100（仅公司参考，岗位单独评分/);
  r.context.testJob = { ...r.context.testJob, rating_status: "program_reference",
    source_rating: { scope: "job", tier_code: "T0", score: 95, source_id: "chatgpt-radar-01" } };
  card = r.run("createRecruitmentJobCard(testJob)");
  rank = descendants(card).find((element) => element.className === "job-rank");
  assert.equal(rank.textContent, "未评分");
  assert.match(card.textContent, /ChatGPT 招聘项目原评：T0 · 95\/100（项目参考，不作岗位分档/);
  assert.doesNotMatch(card.textContent, /已用于岗位排序/);
  r.context.testJob = { ...pendingJob("conflicting-rating"), tier_code: "T2", job_score: 73, rating_status: "conflicted",
    source_ratings: [
      { scope: "job", tier_code: "T0", source_id: "chatgpt-radar-01", reason: "研究岗位" },
      { scope: "job", tier_code: "T2", source_id: "chatgpt-radar-06", reason: "岗位职责需核对" },
    ] };
  card = r.run("createRecruitmentJobCard(testJob)");
  rank = descendants(card).find((element) => element.className === "job-rank");
  assert.equal(rank.textContent, "73 分 T2");
  assert.match(card.textContent, /原评有差异，当前显示系统评分/);
  assert.match(card.textContent, /ChatGPT 岗位原评：T0；研究岗位/);
  assert.match(card.textContent, /ChatGPT 岗位原评：T2；岗位职责需核对/);
  assert.match(card.textContent, /聊天线索/);
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
  assert.equal(r.requestOptions[index].timeoutMs, 120_000);
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
  r.state.recruitmentTierFilter = "BELOW_PRIORITY";
  const requested = [];
  r.context.loadFutureRadarJobPage = (page) => requested.push({ page, status: r.state.futureRadar.filters.status });
  r.run("resetFutureRadarFilters()");
  assert.deepEqual(requested, [{ page: 1, status: "active" }]);
  assert.equal(r.state.recruitmentTierFilter, "BALANCED");
  const resetQuery = new URLSearchParams(r.run("futureRadarJobsQuery()"));
  assert.equal(resetQuery.get("balanced_only"), "true");
  assert.equal(resetQuery.get("priority_only"), "false");
  assert.match(html, /id="future-radar-filter-status"><option value="active">全部有效机会（含待核验）/);
  assert.match(html, /value="all">全部（含已关闭）/);
  assert.equal(DEFAULT_FUTURE_RADAR_STATUS, "active");
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function tierPayload(tier, { id = `row-${tier}`, total = 1, page = 1, size = 1 } = {}) {
  const unranked = tier === "UNRANKED";
  return {
    items: Array.from({ length: size }, (_, index) => ({
      ...pendingJob(`${id}-${index}`), tier_code: unranked ? null : tier, tier_bucket: tier,
    })),
    page, page_size: 50, total,
    stats: { tier_counts: { T0: 0, "T0.5": 3, T1: 80, "T1.5": 4, T2: 120, "T2.5": 6, T3: 7, UNRANKED: 9, BELOW_PRIORITY: 10 },
      verification_status: { pending: total, verified: 0 }, category_counts: { internet_tech: 249 } },
  };
}

function installTierSnapshot(r, tier, payload = tierPayload(tier)) {
  r.state.recruitmentTierFilter = tier;
  r.context.testPayload = payload;
  r.run("applyFutureRadarJobsPayload(testPayload, futureRadarJobsQuery()); renderRecruitmentJobs(state.futureRadar.jobs);");
}

function tierButton(r, tier) {
  return descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === tier);
}

test("T selection is immediate, and its result/count comes from the complete compact API pool", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  const request = deferred();
  r.controls.opportunityHandler = () => request.promise;
  const selecting = r.run("selectRecruitmentTier('T2')");
  assert.equal(tierButton(r, "T2")["aria-pressed"], "true", "highlight changes before a network response");
  assert.equal(tierButton(r, "T1")["aria-pressed"], "false");
  assert.equal(r.cards().length, 0, "a T1 card must not appear under the pending T2 selection");
  assert.match(r.elements.recruitmentJobs.textContent, /正在筛选T2/);
  assert.match(r.elements.recruitmentStatus.textContent, /正在读取T2.*全池/);
  assert.match(r.elements.recruitmentJobs.textContent, /最多等待 120 秒.*继续切换.*旧筛选结果不会作为新结果显示/);
  assert.match(r.elements.recruitmentStatus.textContent, /最多等待 120 秒/);
  assert.equal(r.elements.futureRadarOpportunityCount.textContent, "—");
  assert.equal(r.calls.length, 0);
  await r.flushSelection();
  assert.equal(r.calls.length, 1);
  assert.equal(r.requestOptions[0].timeoutMs, 120_000);
  const query = new URLSearchParams(r.calls[0].split("?")[1]);
  assert.equal(query.get("tier_code"), "T2");
  assert.equal(query.get("compact"), "true");
  assert.equal(query.get("page"), "1");
  request.resolve(tierPayload("T2", { total: 120, size: 50 }));
  assert.equal(await selecting, true);
  assert.equal(r.cards().length, 50);
  assert.equal(r.state.futureRadar.totalJobs, 120, "the 50-row page must not become the global count");
  assert.match(tierButton(r, "T1").textContent, /80/);
  assert.match(tierButton(r, "T2").textContent, /120/);
  assert.equal(r.elements.recruitmentJobs["aria-busy"], "false");
  assert.match(r.elements.recruitmentJobs.textContent, /当前筛选共 120 个机会 · T2/);
});

test("initial and detail cold reads show a 120-second wait without presenting old or partial results", async () => {
  const r = runtime({ fail: false });
  const initial = deferred();
  r.controls.opportunityHandler = () => initial.promise;
  const loading = r.run("loadFutureRadarJobPage(1, true)");
  await new Promise(setImmediate);
  assert.equal(r.cards().length, 0);
  assert.match(r.elements.recruitmentJobs.textContent, /首次读取或扫描更新后可能较慢.*最多等待 120 秒/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /legacy-/);
  assert.equal(r.state.futureRadar.jobsLoading, true);
  assert.equal(r.requestOptions.at(-1).timeoutMs, 120_000);
  initial.resolve(r.payload); await loading;

  const request = deferred();
  r.controls.apiHandler = (path) => path === "/future-radar/opportunities/pending-0" ? request.promise : undefined;
  const details = descendants(r.cards()[0]).find((node) => node.dataset.opportunityDetail === "pending-0");
  details.open = true;
  const detailLoading = details.listeners.toggle();
  assert.match(details.textContent, /正在读取完整机会信息.*最多等待 120 秒/);
  assert.equal(r.requestOptions.at(-1).timeoutMs, 120_000);
  request.resolve(pendingJob("pending-0")); await detailLoading;
  assert.doesNotMatch(details.textContent, /正在读取完整机会信息/);
});

test("same-tier clicks are no-ops after success and share an in-flight selection", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  assert.equal(await r.run("selectRecruitmentTier('T1')"), false);
  assert.equal(r.calls.length, 0);
  const request = deferred();
  r.controls.opportunityHandler = () => request.promise;
  const reads = [r.run("selectRecruitmentTier('T2')"), r.run("selectRecruitmentTier('T2')"), r.run("loadFutureRadarJobPage(1, true)")];
  await r.flushSelection();
  assert.equal(r.calls.length, 1);
  request.resolve(tierPayload("T2"));
  assert.deepEqual(await Promise.all(reads), [true, true, true]);
});

test("rapid cross-tier clicks are coalesced into only the latest server query", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  r.controls.opportunityHandler = () => tierPayload("T1.5");
  const first = r.run("selectRecruitmentTier('T2')");
  const firstController = r.state.futureRadar.jobsRequestController;
  const second = r.run("selectRecruitmentTier('T0.5')");
  const secondController = r.state.futureRadar.jobsRequestController;
  const latest = r.run("selectRecruitmentTier('T1.5')");
  assert.equal(firstController.signal.aborted, true);
  assert.equal(secondController.signal.aborted, true);
  assert.equal(tierButton(r, "T1.5")["aria-pressed"], "true");
  await r.flushSelection();
  assert.deepEqual(await Promise.all([first, second, latest]), [false, false, true]);
  assert.equal(r.calls.length, 1);
  assert.equal(new URLSearchParams(r.calls[0].split("?")[1]).get("tier_code"), "T1.5");
});

test("in-flight A→B→A responses and errors cannot overwrite the latest A result", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  const requests = [];
  r.controls.opportunityHandler = (_path, options) => {
    const request = { ...deferred(), signal: options.signal };
    requests.push(request);
    return request.promise; // Deliberately emulate a transport that ignores abort.
  };
  const first = r.run("selectRecruitmentTier('UNRANKED')");
  await r.flushSelection();
  const second = r.run("selectRecruitmentTier('T0.5')");
  await r.flushSelection();
  const latest = r.run("selectRecruitmentTier('UNRANKED')");
  await r.flushSelection();
  assert.equal(requests.length, 3);
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(requests[1].signal.aborted, true);
  requests[2].resolve(tierPayload("UNRANKED", { id: "latest", total: 19 }));
  assert.equal(await latest, true);
  const latestCard = r.cards()[0];
  requests[1].reject(new Error("superseded upstream failure"));
  requests[0].resolve(tierPayload("UNRANKED", { id: "old", total: 2 }));
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.equal(r.cards()[0], latestCard);
  assert.equal(r.state.futureRadar.totalJobs, 19);
  assert.equal(r.state.futureRadar.jobs[0].id, "latest-0");
  assert.equal(r.state.futureRadar.jobsError, "");
});

test("failed T0 selection restores the T3 snapshot label, not a T0-highlighted T3 page, and permits retry", async () => {
  const r = runtime();
  installTierSnapshot(r, "T3");
  const selecting = r.run("selectRecruitmentTier('T0')");
  await r.flushSelection();
  assert.equal(await selecting, false);
  assert.equal(tierButton(r, "T3")["aria-pressed"], "true");
  assert.equal(tierButton(r, "T0")["aria-pressed"], "false");
  assert.match(r.elements.recruitmentJobs.textContent, /上次成功快照（T3 机会 · 原筛选条件）/);
  assert.equal(r.cards().length, 1);
  assert.equal(r.state.futureRadar.jobs[0].tier_code, "T3");
  r.controls.fail = false;
  r.controls.opportunityHandler = () => tierPayload("T0", { total: 0, size: 0 });
  const retry = r.run("selectRecruitmentTier('T0')");
  assert.equal(tierButton(r, "T0")["aria-pressed"], "true");
  await r.flushSelection();
  assert.equal(await retry, true);
  assert.equal(r.calls.length, 2);
  assert.equal(r.cards().length, 0);
  assert.equal(r.state.futureRadar.totalJobs, 0);
  assert.match(r.elements.recruitmentJobs.textContent, /当前没有 T0 机会/);
  assert.equal(r.state.futureRadar.jobsError, "");
});

test("a cancelled background poll cannot repaint a new T selection or its completed page", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  const poll = deferred();
  const current = deferred();
  r.controls.opportunityHandler = (_path, options) => {
    if (!r.state.futureRadar.jobsLoading) {
      poll.signal = options.signal;
      return poll.promise;
    }
    return current.promise;
  };
  const polling = r.run("pollFutureRadarEvents()");
  const selecting = r.run("selectRecruitmentTier('T2')");
  assert.equal(poll.signal.aborted, true);
  await r.flushSelection();
  current.resolve(tierPayload("T2"));
  await selecting;
  const newCard = r.cards()[0];
  poll.resolve(tierPayload("T1", { id: "stale-poll" }));
  await polling;
  assert.equal(r.cards()[0], newCard);
  assert.equal(tierButton(r, "T2")["aria-pressed"], "true");
  assert.equal(r.state.futureRadar.jobsError, "");
});

test("initial opportunities render before slow metadata, whose completion cannot repaint a later selection", async () => {
  const r = runtime({ fail: false });
  const metadata = deferred();
  r.controls.apiHandler = (path) => path === "/future-radar/programs" ? metadata.promise : undefined;
  const snapshot = r.run("loadFutureRadarSnapshot()");
  await new Promise(setImmediate);
  assert.equal(r.cards().length, 50, "do not wait for all six requests to render opportunities");
  const selectingResponse = deferred();
  r.controls.opportunityHandler = () => selectingResponse.promise;
  const selecting = r.run("selectRecruitmentTier('T1')");
  assert.equal(r.cards().length, 0);
  metadata.resolve({ items: [] });
  await snapshot;
  assert.equal(r.state.futureRadar.jobsLoading, true);
  assert.equal(tierButton(r, "T1")["aria-pressed"], "true");
  assert.match(r.elements.recruitmentJobs.textContent, /正在筛选T1/);
  await r.flushSelection();
  selectingResponse.resolve(tierPayload("T1"));
  assert.equal(await selecting, true);
  assert.equal(r.cards().length, 1);
});

test("an initialization refresh joins an in-flight page selection instead of switching back to page one", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1", tierPayload("T1", { total: 120 }));
  const response = deferred();
  r.controls.opportunityHandler = () => response.promise;
  const page = r.run("loadFutureRadarJobPage(2)");
  const controller = r.state.futureRadar.jobsRequestController;
  const snapshot = r.run("loadFutureRadarSnapshot()");
  await new Promise(setImmediate);
  assert.equal(r.calls.filter((path) => path.startsWith("/future-radar/opportunities?")).length, 1);
  assert.equal(controller.signal.aborted, false);
  assert.equal(r.state.futureRadar.page, 2);
  response.resolve(tierPayload("T1", { page: 2, total: 120, id: "page-two" }));
  assert.deepEqual(await Promise.all([page, snapshot]), [true, true]);
  assert.equal(r.state.futureRadar.jobsAppliedPage, 2);
  assert.equal(r.state.futureRadar.jobs[0].id, "page-two-0");
});

test("revisiting a tier reads a new version instead of trusting a stale client page cache", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  r.controls.opportunityHandler = (path) => {
    const tier = new URLSearchParams(path.split("?")[1]).get("tier_code");
    return tierPayload(tier, { id: tier === "T1" ? "new-version" : "other", total: tier === "T1" ? 23 : 7 });
  };
  const other = r.run("selectRecruitmentTier('T2')");
  await r.flushSelection();
  await other;
  const revisit = r.run("selectRecruitmentTier('T1')");
  await r.flushSelection();
  await revisit;
  assert.equal(r.calls.length, 2);
  assert.equal(r.state.futureRadar.totalJobs, 23);
  assert.equal(r.state.futureRadar.jobs[0].id, "new-version-0");
});

test("logout cancels an unsent tier debounce and removes all saved request state", async () => {
  const r = runtime({ fail: false });
  installTierSnapshot(r, "T1");
  const selecting = r.run("selectRecruitmentTier('T2')");
  await new Promise(setImmediate);
  r.state.token = null;
  r.run("endFutureRadarSession()");
  await r.flushSelection();
  assert.equal(await selecting, false);
  assert.equal(r.calls.length, 0);
  assert.equal(r.state.futureRadar.jobsLoaded, false);
  assert.equal(r.state.futureRadar.jobsAppliedQuery, "");
  assert.equal(r.state.futureRadar.jobsRequestController, null);
  assert.equal(r.state.futureRadar.jobsRequestPromise, null);
});

function companyGroup(key, name, total = 1, tier = "T2") {
  return { company_key: key, company_name: name, grouping: key.startsWith("telecom:") ? "telecom_group" : "company",
    opportunity_count: total, specific_job_count: total, program_count: 0,
    tier_counts: { [tier]: total }, category_counts: { state_tech_telecom: total },
    cities: ["上海", "北京"], city_count: 2, verified_count: 0, discovered_count: total };
}

function companyPayload({ page = 1, items, totalCompanies = 26, total = 145 } = {}) {
  return { view: "companies", items: items || (page === 1
    ? [companyGroup("telecom:china_unicom", "中国联通", 120), ...Array.from({ length: 19 }, (_, i) => companyGroup(`company:${i}`, `其他企业 ${i}`))]
    : Array.from({ length: 6 }, (_, i) => companyGroup(`company:page-two-${i}`, `第二页企业 ${i}`))),
  page, page_size: 20, total: totalCompanies, total_companies: totalCompanies, total_opportunities: total,
  stats: { total_opportunities: total, total_companies: totalCompanies,
    verification_status: { pending: total, verified: 0, conflicted: 0 },
    tier_counts: { T2: total }, category_counts: { state_tech_telecom: total } } };
}

function companyRuntime() {
  const r = runtime({ fail: false });
  Object.assign(r.state.futureRadar, { view: "companies", jobsAppliedView: "companies", pageSize: 20,
    totalCompanies: 0, companies: [], companyExpansions: new Map() });
  r.controls.opportunityHandler = (path) => {
    const params = new URLSearchParams(path.split("?")[1]);
    const page = Number(params.get("page"));
    if (!params.has("company_key")) return companyPayload({ page });
    const payload = tierPayload("T2", { page, total: 120, size: page < 3 ? 50 : 20, id: `expanded-page-${page}` });
    payload.view = "jobs";
    payload.total_opportunities = 120;
    payload.total_companies = 1;
    payload.items.forEach((item) => { item.company = "中国联合网络通信有限公司安徽省分公司"; });
    return payload;
  };
  r.companyCards = () => descendants(r.elements.recruitmentJobs).filter((node) => node.className === "radar-company-card");
  return r;
}

test("company expansion clones every parent filter and only replaces projection pagination", () => {
  const parentQuery = "view=companies&page=3&page_size=20&status=unknown&tier_code=T1&q=数据&source_id=public&category=internet_tech&category=state_tech_telecom";
  const params = new URLSearchParams(buildFutureRadarCompanyJobsQuery({ parentQuery, companyKey: "company:stable", page: 2 }));
  assert.equal(params.get("view"), "jobs");
  assert.equal(params.get("company_key"), "company:stable");
  assert.equal(params.get("page"), "2");
  assert.equal(params.get("page_size"), "50");
  assert.equal(params.get("status"), "unknown");
  assert.equal(params.get("tier_code"), "T1");
  assert.equal(params.get("q"), "数据");
  assert.equal(params.get("source_id"), "public");
  assert.deepEqual(params.getAll("category"), ["internet_tech", "state_tech_telecom"]);
  assert.throws(() => buildFutureRadarCompanyJobsQuery(), /companyKey/);
});

test("frontend defaults to companies and labels full company and opportunity counts separately", async () => {
  assert.match(source, /futureRadar:\s*\{\s*dashboard: null,\s*view: "companies"/);
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  assert.equal(new URLSearchParams(r.calls[0].split("?")[1]).get("view"), "companies");
  assert.equal(r.state.futureRadar.totalJobs, 145);
  assert.equal(r.state.futureRadar.totalCompanies, 26);
  assert.equal(r.companyCards().length, 20);
  assert.equal(r.cards().length, 0, "groups are not falsely rendered as jobs");
  assert.match(r.elements.futureRadarPageStatus.textContent, /第 1 \/ 2 页.*26 个企业分组.*145 个机会/);
  assert.match(r.elements.recruitmentStatus.textContent, /145 个机会.*26 个企业分组/);
  assert.match(r.elements.recruitmentJobs.textContent, /企业分组不合并岗位/);
  assert.equal(r.elements.futureRadarOpportunityCount.textContent, "145");
  const switcher = descendants(r.elements.recruitmentJobs).find((node) => node.dataset.opportunityView === "companies");
  assert.equal(switcher["aria-pressed"], "true");
  await r.run("loadFutureRadarJobPage(2)");
  assert.equal(r.companyCards().length, 6);
  assert.match(r.elements.recruitmentJobs.textContent, /第二页企业/);
  assert.match(r.elements.futureRadarPageStatus.textContent, /第 2 \/ 2 页/);
});

test("company expansion pages actual jobs with original entities, details and official links", async () => {
  const r = companyRuntime();
  r.state.futureRadar.filters = { status: "unknown", city: "上海", q: "数据", source_id: "public" };
  r.context.selectedRecruitmentStarfields = () => ["internet_tech", "state_tech_telecom"];
  r.state.recruitmentTierFilter = "T2";
  await r.run("loadFutureRadarJobPage(1, true)");
  const card = r.companyCards()[0];
  card.open = true;
  assert.equal(await card.listeners.toggle(), true);
  const query = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(query.get("company_key"), "telecom:china_unicom");
  assert.equal(query.get("view"), "jobs");
  assert.equal(query.get("tier_code"), "T2");
  assert.equal(query.get("status"), "unknown");
  assert.equal(query.get("source_id"), "public");
  assert.equal(query.get("city"), "上海");
  assert.deepEqual(query.getAll("category"), ["internet_tech", "state_tech_telecom"]);
  assert.equal(r.cards().length, 50);
  assert.match(r.cards()[0].textContent, /中国联合网络通信有限公司安徽省分公司/);
  assert.ok(descendants(r.cards()[0]).some((node) => node.dataset.opportunityDetail));
  assert.ok(descendants(r.cards()[0]).some((node) => node.tag === "a" && node.href.startsWith("https://careers.example.com/")));
  const next = descendants(card).find((node) => node.tag === "button" && node.textContent === "下一页岗位 →");
  await next.listeners.click();
  assert.equal(new URLSearchParams(r.calls.at(-1).split("?")[1]).get("page"), "2");
  assert.match(r.cards()[0].textContent, /expanded-page-2/);
  assert.equal(r.state.futureRadar.page, 1, "nested pagination cannot move the company page");
  assert.equal(r.state.futureRadar.totalJobs, 145, "nested counts cannot overwrite the main pool");
  assert.equal(r.state.futureRadar.totalCompanies, 26);
});

test("view switches retain filters and a late company response cannot overwrite job mode", async () => {
  const r = companyRuntime();
  const companies = deferred();
  const jobs = deferred();
  r.state.recruitmentTierFilter = "T1";
  r.state.futureRadar.filters = { q: "data", status: "active", city: "上海" };
  r.controls.opportunityHandler = (path) => new URLSearchParams(path.split("?")[1]).get("view") === "companies" ? companies.promise : jobs.promise;
  const first = r.run("loadFutureRadarJobPage(1, true)");
  await new Promise(setImmediate);
  const controller = r.state.futureRadar.jobsRequestController;
  const switchToJobs = r.run("selectFutureRadarView('jobs')");
  await new Promise(setImmediate);
  assert.equal(controller.signal.aborted, true);
  const query = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(query.get("view"), "jobs");
  assert.equal(query.get("page_size"), "50");
  assert.equal(query.get("tier_code"), "T1");
  assert.equal(query.get("q"), "data");
  jobs.resolve({ ...tierPayload("T1", { id: "job-mode", total: 8 }), view: "jobs", total_companies: 3, total_opportunities: 8 });
  assert.equal(await switchToJobs, true);
  companies.resolve(companyPayload());
  assert.equal(await first, false);
  assert.equal(r.state.futureRadar.jobsAppliedView, "jobs");
  assert.equal(r.state.futureRadar.companies.length, 0);
  assert.equal(r.state.futureRadar.totalJobs, 8);
  assert.match(r.cards()[0].textContent, /job-mode/);
});

test("rapid T changes in company mode use the latest full-pool query and clear stale expansions", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  const pending = new Map();
  r.controls.opportunityHandler = (path, options) => {
    const params = new URLSearchParams(path.split("?")[1]);
    const request = { ...deferred(), signal: options.signal };
    pending.set(params.get("company_key") || params.get("tier_code"), request);
    return request.promise;
  };
  const card = r.companyCards()[0];
  card.open = true;
  const expanding = card.listeners.toggle();
  await new Promise(setImmediate);
  const first = r.run("selectRecruitmentTier('T1')");
  await r.flushSelection();
  const latest = r.run("selectRecruitmentTier('T0.5')");
  await r.flushSelection();
  assert.equal(pending.get("telecom:china_unicom").signal.aborted, true);
  assert.equal(pending.get("T1").signal.aborted, true);
  pending.get("T0.5").resolve(companyPayload({ items: [companyGroup("company:latest", "最新匹配企业", 1, "T0.5")], totalCompanies: 1, total: 1 }));
  assert.equal(await latest, true);
  pending.get("T1").resolve(companyPayload());
  pending.get("telecom:china_unicom").resolve(tierPayload("T2", { id: "stale-expanded" }));
  assert.equal(await first, false);
  assert.equal(await expanding, false);
  assert.equal(r.state.futureRadar.jobsAppliedTier, "T0.5");
  assert.equal(r.state.futureRadar.jobsAppliedView, "companies");
  assert.equal(r.state.futureRadar.totalJobs, 1);
  assert.match(r.elements.recruitmentJobs.textContent, /最新匹配企业/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /stale-expanded/);
  assert.equal(new URLSearchParams(r.calls.at(-1).split("?")[1]).get("view"), "companies");
});

test("changing company page cancels an expansion and late rows stay out of the new page", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  const response = deferred();
  const initialHandler = r.controls.opportunityHandler;
  r.controls.opportunityHandler = (path) => path.includes("company_key=") ? response.promise : initialHandler(path);
  const card = r.companyCards()[0];
  card.open = true;
  const expansion = card.listeners.toggle();
  await new Promise(setImmediate);
  const entry = r.state.futureRadar.companyExpansions.get("telecom:china_unicom");
  const signal = entry.controller.signal;
  await r.run("loadFutureRadarJobPage(2)");
  assert.equal(signal.aborted, true);
  response.resolve(tierPayload("T2", { id: "late-other-page" }));
  assert.equal(await expansion, false);
  assert.equal(r.state.futureRadar.page, 2);
  assert.equal(entry.jobs.length, 0);
  assert.equal(r.cards().length, 0);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /late-other-page/);
});

test("nested page races and collapse keep only the latest expanded page", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  const first = deferred(), latest = deferred();
  r.controls.opportunityHandler = (path) => new URLSearchParams(path.split("?")[1]).get("page") === "1" ? first.promise : latest.promise;
  const card = r.companyCards()[0];
  card.open = true;
  const expanding = card.listeners.toggle();
  await new Promise(setImmediate);
  const entry = r.state.futureRadar.companyExpansions.get("telecom:china_unicom");
  const firstSignal = entry.controller.signal;
  const secondPage = r.run("loadFutureRadarCompanyJobs('telecom:china_unicom', 2)");
  await new Promise(setImmediate);
  assert.equal(firstSignal.aborted, true);
  latest.resolve(tierPayload("T2", { page: 2, total: 120, id: "latest-expanded" }));
  assert.equal(await secondPage, true);
  first.resolve(tierPayload("T2", { page: 1, total: 120, id: "outdated-expanded" }));
  assert.equal(await expanding, false);
  assert.equal(entry.page, 2);
  assert.match(card.textContent, /latest-expanded/);
  assert.doesNotMatch(card.textContent, /outdated-expanded/);
  card.open = false;
  card.listeners.toggle();
  assert.equal(entry.open, false);
  card.open = true;
  card.listeners.toggle();
  assert.match(card.textContent, /latest-expanded/);
});

test("unchanged company polling preserves expanded jobs and avoids duplicate reads", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  const card = r.companyCards()[0];
  card.open = true;
  await card.listeners.toggle();
  const previousCards = r.cards();
  await r.run("pollFutureRadarEvents()");
  assert.equal(r.companyCards()[0], card);
  assert.equal(r.cards()[0], previousCards[0]);
  assert.equal(card.open, true);
  assert.equal(r.calls.filter((path) => path.includes("company_key=")).length, 1);
});

test("failed view switch identifies the old snapshot and does not treat companies as job cards", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  r.controls.opportunityHandler = () => Promise.reject(Object.assign(new Error("not public"), { status: 500 }));
  assert.equal(await r.run("selectFutureRadarView('jobs')"), false);
  assert.equal(r.state.futureRadar.view, "jobs");
  assert.equal(r.state.futureRadar.jobsAppliedView, "companies");
  assert.match(r.elements.recruitmentJobs.textContent, /上次成功快照（原筛选条件）/);
  assert.equal(r.companyCards().length, 20);
  assert.equal(r.cards().length, 0);
  const switchers = descendants(r.elements.recruitmentJobs).filter((node) => node.dataset.opportunityView);
  assert.equal(switchers.find((node) => node.dataset.opportunityView === "companies")["aria-pressed"], "true");
  assert.equal(switchers.find((node) => node.dataset.opportunityView === "jobs")["aria-pressed"], "false");
});

test("balanced is the initial query and all three pool chips use exact server totals", async () => {
  assert.match(source, /recruitmentTierFilter:\s*"BALANCED"/);
  assert.match(extract('document.querySelectorAll(".recruitment-checks input").forEach', '\n[elements.recruitmentRoles'), /state\.recruitmentTierFilter = "BALANCED"/);
  const r = runtime({ fail: false });
  r.payload.stats.tier_counts = { T2: 20, UNRANKED: 255, BELOW_PRIORITY: 2956 };
  r.payload.stats.balanced_total = 120;
  r.payload.stats.priority_total = 275;
  r.payload.stats.matching_total = 3231;
  r.payload.stats.secondary_total = 2956;
  await r.run("loadFutureRadarJobPage(1, true)");
  const params = new URLSearchParams(r.calls[0].split("?")[1]);
  assert.equal(params.get("balanced_only"), "true");
  assert.equal(params.get("priority_only"), "false");
  assert.equal(params.has("tier_code"), false);
  assert.equal(r.state.futureRadar.jobsAppliedTier, "BALANCED");
  const balanced = descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === "BALANCED");
  assert.equal(balanced["aria-pressed"], "true");
  assert.match(balanced.textContent, /均衡精选.*120 个/);
  const focus = descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === "FOCUS");
  assert.equal(focus["aria-pressed"], "false");
  assert.match(focus.textContent, /全部重点.*275 个/);
  const all = descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === "ALL");
  assert.match(all.textContent, /全部记录.*3,231 个/);
  assert.match(r.elements.futureRadarOpportunitySummary.textContent, /均衡精选 120.*全部重点 275.*全部记录 3,231/);
  assert.match(r.elements.recruitmentJobs.textContent, /默认均衡精选 120 条.*全部重点 275 条.*全部记录 3,231 条/);
  delete r.payload.stats.balanced_total;
  r.run("renderRecruitmentJobs()");
  assert.match(descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === "BALANCED").textContent, /— 个/);
  assert.doesNotMatch(descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === "BALANCED").textContent, /50 个/);
  const selectingFocus = r.run("selectRecruitmentTier('FOCUS')");
  await r.flushSelection();
  await selectingFocus;
  const focusParams = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(focusParams.get("balanced_only"), "false");
  assert.equal(focusParams.get("priority_only"), "true");
  const selectingAll = r.run("selectRecruitmentTier('ALL')");
  await r.flushSelection();
  await selectingAll;
  const allParams = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(allParams.get("balanced_only"), "false");
  assert.equal(allParams.get("priority_only"), "false");
  assert.equal(r.state.futureRadar.jobsAppliedTier, "ALL");
  const selectingSecondary = r.run("selectRecruitmentTier('BELOW_PRIORITY')");
  await r.flushSelection();
  await selectingSecondary;
  const secondaryParams = new URLSearchParams(r.calls.at(-1).split("?")[1]);
  assert.equal(secondaryParams.get("priority_only"), "false");
  assert.equal(secondaryParams.get("tier_code"), "BELOW_PRIORITY");
});

test("balanced preserves returned unranked records and defensively excludes secondary rows without deleting them", async () => {
  const r = runtime({ fail: false });
  const secondary = { ...pendingJob("below-priority"), tier_code: "不建议投", job_score: 40 };
  r.payload.items = [pendingJob("unranked-kept"), { ...pendingJob("t3-kept"), tier_code: "T3", job_score: 61 }, secondary];
  await r.run("loadFutureRadarJobPage(1, true)");
  assert.equal(r.cards().length, 2);
  assert.match(r.elements.recruitmentJobs.textContent, /unranked-kept.*t3-kept|t3-kept.*unranked-kept/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /below-priority/);
  assert.equal(r.state.futureRadar.jobs.length, 3, "a display safeguard must not delete the source data");
  const selection = r.run("selectRecruitmentTier('ALL')");
  await r.flushSelection();
  await selection;
  assert.equal(r.cards().length, 3);
});

test("company expansion and background polling inherit the balanced projection without introducing a T restriction", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(1, true)");
  const card = r.companyCards()[0];
  card.open = true;
  await card.listeners.toggle();
  await r.run("pollFutureRadarEvents()");
  const queries = r.calls.filter((path) => path.startsWith("/future-radar/opportunities?")).map((path) => new URLSearchParams(path.split("?")[1]));
  assert.ok(queries.some((params) => params.has("company_key")));
  assert.ok(queries.length >= 3);
  queries.forEach((params) => {
    assert.equal(params.get("balanced_only"), "true");
    assert.equal(params.get("priority_only"), "false");
    assert.equal(params.has("tier_code"), false);
  });
});

function attachCategoryBadge(r, category) {
  const label = new Element("label");
  label.querySelector = () => descendants(label).find((node) => node.className === "radar-category-count");
  const input = { value: category, closest: () => label };
  r.context.document.querySelectorAll = (selector) => selector === ".recruitment-checks input" ? [input] : [];
  return () => label.querySelector();
}

test("sidebar counts reflect final company/tier scope and hide stale numbers while pending or failed", async () => {
  const r = companyRuntime();
  const badge = attachCategoryBadge(r, "state_tech_telecom");
  const payload = companyPayload({ items: [companyGroup("telecom:china_unicom", "中国联通", 12)], totalCompanies: 1, total: 12 });
  Object.assign(payload.stats, { category_counts: { state_tech_telecom: 2956 },
    visible_category_counts: { state_tech_telecom: 12 }, visible_category_company_counts: { state_tech_telecom: 1 } });
  r.controls.opportunityHandler = () => payload;
  await r.run("loadFutureRadarJobPage(1, true)");
  assert.equal(badge().textContent, "1组 · 12条");
  assert.equal(badge().dataset.status, "ready");
  const response = deferred();
  r.controls.opportunityHandler = () => response.promise;
  const selection = r.run("selectRecruitmentTier('T1')");
  assert.equal(badge().textContent, "—");
  assert.equal(badge().dataset.status, "loading");
  await r.flushSelection();
  response.reject(Object.assign(new Error("safe test failure"), { status: 503 }));
  assert.equal(await selection, false);
  assert.equal(badge().textContent, "—");
  assert.equal(badge().dataset.status, "error");
  assert.equal(r.elements.futureRadarOpportunityCount.textContent, "—");
  assert.match(r.elements.futureRadarOpportunitySummary.textContent, /读取失败.*均衡精选快照/);
  assert.doesNotMatch(r.elements.futureRadarOpportunitySummary.textContent, /2956|12|官网已确认/);
  const balanced = descendants(r.elements.recruitmentJobs).find((node) => node.dataset.tier === "BALANCED");
  assert.equal(balanced["aria-pressed"], "true");
  r.controls.opportunityHandler = () => ({ ...payload, items: [], total: 0, total_companies: 0, total_opportunities: 0,
    stats: { ...payload.stats, total_companies: 0, total_opportunities: 0, visible_category_counts: {}, visible_category_company_counts: {} } });
  const retry = r.run("selectRecruitmentTier('T1')");
  await r.flushSelection();
  await retry;
  assert.equal(badge().textContent, "0组 · 0条");
  assert.equal(badge().dataset.status, "ready");
});

test("switching focus back to balanced rejects projection-mismatched sidebar statistics", async () => {
  const r = companyRuntime();
  const badge = attachCategoryBadge(r, "state_tech_telecom");
  const focus = companyPayload({
    items: [companyGroup("telecom:china_unicom", "中国联通", 917)],
    totalCompanies: 21,
    total: 917,
  });
  Object.assign(focus.stats, {
    selection_mode: "priority",
    balanced_total: 304,
    priority_total: 1338,
    matching_total: 3511,
    visible_category_counts: { state_tech_telecom: 917 },
    visible_category_company_counts: { state_tech_telecom: 21 },
  });
  const balanced = companyPayload({
    items: [companyGroup("telecom:china_unicom", "中国联通", 6)],
    totalCompanies: 210,
    total: 304,
  });
  Object.assign(balanced.stats, {
    selection_mode: "balanced",
    balanced_total: 304,
    priority_total: 1338,
    matching_total: 3511,
    visible_category_counts: { state_tech_telecom: 56 },
    visible_category_company_counts: { state_tech_telecom: 21 },
  });
  let returnWrongBalancedProjection = true;
  r.controls.opportunityHandler = (path) => {
    const params = new URLSearchParams(path.split("?")[1]);
    if (params.get("priority_only") === "true") return focus;
    if (returnWrongBalancedProjection) {
      returnWrongBalancedProjection = false;
      return { ...balanced, stats: { ...focus.stats } };
    }
    return balanced;
  };

  const selectingFocus = r.run("selectRecruitmentTier('FOCUS')");
  await r.flushSelection();
  assert.equal(await selectingFocus, true);
  assert.equal(badge().textContent, "21组 · 917条");

  const selectingBalanced = r.run("selectRecruitmentTier('BALANCED')");
  await r.flushSelection();
  assert.equal(await selectingBalanced, false);
  assert.equal(r.state.futureRadar.jobsAppliedTier, "FOCUS");
  assert.equal(r.state.futureRadar.totalJobs, 917, "cards, totals and stats stay on one owned projection");
  assert.equal(badge().textContent, "—", "FOCUS counts never appear under a selected BALANCED chip");

  const retry = r.run("selectRecruitmentTier('BALANCED')");
  await r.flushSelection();
  assert.equal(await retry, true);
  assert.equal(r.state.futureRadar.jobsAppliedTier, "BALANCED");
  assert.equal(r.state.futureRadar.totalJobs, 304);
  assert.equal(badge().textContent, "21组 · 56条");
});

test("starfield changes hide the previous category count before the debounced profile-save read", async () => {
  const r = companyRuntime();
  const badge = attachCategoryBadge(r, "state_tech_telecom");
  const payload = companyPayload();
  payload.stats.visible_category_counts = { state_tech_telecom: 145 };
  payload.stats.visible_category_company_counts = { state_tech_telecom: 26 };
  r.controls.opportunityHandler = () => payload;
  await r.run("loadFutureRadarJobPage(1, true)");
  assert.equal(badge().textContent, "26组 · 145条");
  r.context.selectedRecruitmentStarfields = () => ["big_four_professional_services"];
  r.run("renderFutureRadarOpportunityOverview()");
  assert.equal(r.state.futureRadar.jobsLoading, false);
  assert.equal(badge().textContent, "—");
  assert.equal(badge().dataset.status, "loading");
  assert.equal(r.elements.futureRadarOpportunityCount.textContent, "—");
});

test("expanding an old company after a failed request gives a visible main-pool retry instead of an empty panel", async () => {
  const r = companyRuntime();
  const successfulHandler = r.controls.opportunityHandler;
  await r.run("loadFutureRadarJobPage(1, true)");
  r.controls.opportunityHandler = () => Promise.reject(Object.assign(new Error("test failure"), { status: 503 }));
  await r.run("selectFutureRadarView('jobs')");
  const card = r.companyCards()[0];
  const callsBefore = r.calls.length;
  card.open = true;
  await card.listeners.toggle();
  assert.equal(r.calls.length, callsBefore, "do not fetch a company using a failed new projection");
  assert.match(card.textContent, /主机会池本次读取失败.*上次成功的企业快照/);
  const retry = descendants(card).find((node) => node.tag === "button" && node.textContent === "重试主机会池");
  assert.ok(retry);
  r.controls.opportunityHandler = (path) => new URLSearchParams(path.split("?")[1]).get("view") === "jobs"
    ? { ...tierPayload("T2", { id: "recovered-projection", total: 1 }), view: "jobs" }
    : successfulHandler(path);
  await retry.listeners.click();
  assert.equal(r.state.futureRadar.jobsError, "");
  assert.equal(r.state.futureRadar.jobsAppliedView, "jobs");
  assert.equal(new URLSearchParams(r.calls.at(-1).split("?")[1]).has("company_key"), false);
  assert.match(r.cards()[0].textContent, /recovered-projection/);
});

test("failed switch from company page two retains the old page size and disables mismatched navigation", async () => {
  const r = companyRuntime();
  await r.run("loadFutureRadarJobPage(2, true)");
  r.controls.opportunityHandler = () => Promise.reject(Object.assign(new Error("not public"), { status: 500 }));
  await r.run("selectFutureRadarView('jobs')");
  assert.equal(r.state.futureRadar.pageSize, 50);
  assert.equal(r.state.futureRadar.jobsAppliedPageSize, 20);
  assert.match(r.elements.futureRadarPageStatus.textContent, /第 2 \/ 2 页.*上次成功快照.*26 个企业分组/);
  assert.equal(r.elements.futureRadarPagePrev.disabled, true);
  assert.equal(r.elements.futureRadarPageNext.disabled, true);
});
