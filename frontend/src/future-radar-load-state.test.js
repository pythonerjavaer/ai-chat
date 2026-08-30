import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {
  DEFAULT_FUTURE_RADAR_STATUS, TIER_CODES, buildFutureRadarJobsQuery,
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
  const payload = { items: Array.from({ length: 50 }, (_, i) => pendingJob(`pending-${i}`)), total: 255,
    page: 1, page_size: 50, stats: { verification_status: { pending: 255, verified: 0, conflicted: 0 },
      job_status: { unknown: 255 }, tier_counts: { UNRANKED: 255 }, category_counts: { internet_tech: 255 } } };
  const oldJobs = Array.from({ length: 6 }, (_, i) => pendingJob(`legacy-${i}`));
  const state = { recruitmentJobs: oldJobs, recruitmentWatches: [], recruitmentTierFilter: "ALL", futureRadar: {
    jobsLoaded: existing, jobsError: "", jobs: existing ? [pendingJob("saved-main")] : [],
    jobsLoading: false, loading: false, jobsRequestId: 0, page: 1, pageSize: 50,
    totalJobs: existing ? 1 : 0,
    opportunityStats: existing ? { tier_counts: { UNRANKED: 1 }, verification_status: { pending: 1 } } : {},
    filters: { status: DEFAULT_FUTURE_RADAR_STATUS },
    sources: [], runs: [], events: [], activeRunTypes: new Set(), polling: false,
    searchScope: {}, searchCoverage: null,
  } };
  const elements = Object.fromEntries([
    "recruitmentJobs", "recruitmentError", "futureRadarLoading", "futureRadarError",
    "futureRadarLiveState", "futureRadarOpportunitySummary", "futureRadarOpportunityCount",
  ].map((name) => [name, new Element()]));
  elements.recruitmentDialog = { open: true };
  elements.futureRadarFilterForm = { reset() {} };
  const noop = () => {};
  const context = {
    state, elements, DEFAULT_FUTURE_RADAR_STATUS, TIER_CODES, buildFutureRadarJobsQuery,
    futureRadarOpportunityDateCopy, futureRadarOpportunityErrorCopy, futureRadarOpportunitySource,
    futureRadarPublicOpportunityUrl, jobTierBucket, partitionJobsByPriority,
    document: { hidden: false, querySelectorAll: () => [], createElement: (tag) => new Element(tag), createTextNode: (text) => text },
    makeElement: (tag, className = "", text = "") => new Element(tag, className, text),
    api: async (path) => {
      calls.push(path);
      if (path.startsWith("/future-radar/opportunities?")) {
        if (controls.fail) throw Object.assign(new Error("private provider diagnostics must not render"), { status: 500 });
        return payload;
      }
      if (path === "/recruitment/jobs") {
        if (controls.legacyFail) throw new Error("legacy unavailable");
        return { jobs: oldJobs, monitor_pools: [] };
      }
      if (path === "/recruitment/watches") return { watches: [] };
      if (path === "/recruitment/profile") return {};
      if (path === "/future-radar/dashboard") return { total_jobs: 6 };
      return { items: [] };
    },
    radarCollection: (value) => value?.items || [],
    radarNumber: (value, keys, fallback) => keys.map((key) => value?.[key]).find((v) => v != null) ?? fallback,
    selectedRecruitmentStarfields: () => [],
    filterRecruitmentByStarfield: (jobs) => filterJobsByStarfields(jobs, []),
    recruitmentJobUrl: futureRadarPublicOpportunityUrl,
    recruitmentDaysLeft: () => null,
    recruitmentScoringFactors: () => [],
    radarStatusCopy: (status) => status,
    formatRadarTime: () => "待记录",
    createFutureRadarOpportunityDetail: () => new Element("details"),
    eventIdentity: (event) => event.id,
    chatgptSyncFromJobs: () => null,
    translateError: (message) => message,
  };
  for (const name of ["renderFutureRadarPagination", "renderFutureRadarDashboard", "syncFutureRadarProgramFilter",
    "renderFutureRadarPrograms", "mergeFutureRadarEvents", "renderFutureRadarEvents", "syncFutureRadarSourceFilter",
    "renderFutureRadarSources", "renderFutureRadarRuns", "renderRecruitmentDeadlineAlerts", "renderRecruitmentProfile",
    "renderRecruitmentWatches", "renderHomeRecruitmentAlerts", "renderRecruitmentMonitors", "renderRecruitmentSyncStatus",
    "setRecruitmentStatus", "applyIncrementalRadarMetrics", "addRecruitmentWatchFromJob"]) context[name] = noop;
  vm.createContext(context);
  const functions = [
    extract("function setFutureRadarLoading(", "\nfunction mergeFutureRadarEvents"),
    extract("function renderFutureRadarOpportunityOverview(", "\nfunction createFutureRadarOpportunityDetail"),
    extract("function recordFutureRadarOpportunityFailure(", "\nfunction syncFutureRadarSourceFilter"),
    extract("function resetFutureRadarFilters(", "\nfunction applyIncrementalRadarMetrics"),
    extract("async function loadFutureRadarSnapshot(", "\nfunction stopFutureRadarPolling"),
    extract("function recruitmentVerification(", "\nfunction recruitmentScoringFactors"),
    extract("function selectRecruitmentTier(", "\nasync function addRecruitmentWatchFromJob"),
    extract("async function refreshRecruitment(", "\nasync function refreshRecruitmentSource"),
  ].join("\n");
  vm.runInContext(functions, context);
  return { controls, calls, payload, state, elements, context,
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
  const retry = descendants(r.elements.recruitmentJobs).find((el) => el.tag === "button");
  assert.equal(retry.textContent, "重试加载主机会池");
  r.controls.fail = false;
  await retry.listeners.click();
  assert.equal(r.state.futureRadar.jobsLoaded, true);
  assert.equal(r.state.futureRadar.jobsError, "");
  assert.equal(r.cards().length, 50);
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
  assert.match(r.elements.recruitmentJobs.textContent, /saved-main/);
  assert.doesNotMatch(r.elements.recruitmentJobs.textContent, /legacy-/);
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
