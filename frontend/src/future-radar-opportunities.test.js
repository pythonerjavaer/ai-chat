import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {
  FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
  buildFutureRadarJobsQuery,
  futureRadarOpportunityDateCopy,
  futureRadarOpportunitySource,
  futureRadarPublicOpportunityUrl,
  jobTierBucket,
} from "./recruitment-radar.js";

const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
function functionSource(startMarker, endMarker) {
  const start = appSource.indexOf(startMarker);
  const end = appSource.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start);
  return appSource.slice(start, end);
}

test("chat leads, search discoveries and official confirmation keep distinct source labels", () => {
  assert.equal(futureRadarOpportunitySource({ verification_status: "pending", sources: [{ source_id: "chatgpt-radar-06" }] }).label, "聊天线索");
  assert.equal(futureRadarOpportunitySource({ verification_status: "pending", discovered_by: [{ name: "OpenAI 公网搜索" }] }).label, "搜索发现");
  assert.equal(futureRadarOpportunitySource({ verification_status: "verified", sources: [{ source_id: "chatgpt-radar-06" }] }).label, "官网已确认");
  const conflicted = futureRadarOpportunitySource({ verification_status: "conflicted", source: "ChatGPT 受控同步" });
  assert.equal(conflicted.label, "聊天线索");
  assert.match(conflicted.description, /有差异/);
  assert.equal(futureRadarOpportunitySource({}).label, "公开线索");
});

test("unconfirmed source dates are not presented as verified deadlines", () => {
  const lead = { verification_status: "pending", closing_date: "2027-09-08", opening_date: "2027-08-20" };
  const dates = futureRadarOpportunityDateCopy(lead);
  assert.equal(dates.verified, false);
  assert.equal(dates.closing, "来源标注截止 2027-09-08");
  assert.equal(dates.opening, "来源标注开放 2027-08-20");
  assert.equal(futureRadarOpportunityDateCopy({ ...lead, verification_status: "verified" }).closing, "截止 2027-09-08");
  assert.equal(futureRadarOpportunityDateCopy({}).closing, "截止日期未标注");
});

test("recruitment programs stay visible without pretending to be scored individual jobs", () => {
  assert.equal(jobTierBucket({ listing_kind: "recruitment_program", tier_code: "T0", job_score: 95 }), "UNRANKED");
  assert.equal(jobTierBucket({ scoring_status: "unscored_program_listing", tier_code: null }), "UNRANKED");
  assert.match(appSource, /isRecruitmentProgram\) labels\.appendChild\(makeElement\("span", "job-type", "招聘项目"\)\)/);
});

test("opportunity links use supplied public HTTPS URLs and reject private or unsafe values", () => {
  assert.equal(futureRadarPublicOpportunityUrl({ application_url: "https://careers.example.com/job/42", official_url: "https://careers.example.com/" }), "https://careers.example.com/job/42");
  assert.equal(futureRadarPublicOpportunityUrl({ discovered_by: [{ source_url: "https://careers.example.com/job/43" }] }), "https://careers.example.com/job/43");
  ["javascript:alert(1)", "http://careers.example.com/", "https://user:secret@careers.example.com/", "https://127.0.0.1/", "https://10.0.0.1/", "https://chatgpt.com/c/private-placeholder", "https://careers.example.com/?token=secret"].forEach((url) => {
    assert.equal(futureRadarPublicOpportunityUrl({ url }), "");
  });
});

test("one unified API page is not mixed with old pools or assigned a default T3", () => {
  const chat = { id: "chat", verification_status: "pending", tier_code: "T1.5" };
  const unranked = { id: "unranked", verification_status: "pending", tier_code: null };
  const state = { futureRadar: { jobsLoaded: true, jobs: [chat, unranked] }, recruitmentJobs: [{ id: "old-page" }] };
  const result = vm.runInNewContext(`${functionSource("function futureRadarDisplayJobs(", "\nfunction finiteRadarScore")}\nfutureRadarDisplayJobs();`, { state });
  assert.equal(result, state.futureRadar.jobs);
  assert.equal(result.length, 2);
  assert.equal(jobTierBucket(result[0]), "T1.5");
  assert.equal(jobTierBucket(result[1]), "UNRANKED");
});

test("the query applies T filters across the complete API pool", () => {
  const state = { recruitmentTierFilter: "T0.5", futureRadar: { page: 3, pageSize: 50, filters: { status: "open", verification_status: "" } } };
  const query = vm.runInNewContext(`${functionSource("function futureRadarJobsQuery(", "\nfunction syncFutureRadarSourceFilter")}\nfutureRadarJobsQuery(1);`, {
    state, buildFutureRadarJobsQuery, selectedRecruitmentStarfields: () => ["policy_state_banks"],
  });
  const params = new URLSearchParams(query);
  assert.equal(params.get("tier_code"), "T0.5");
  assert.equal(params.get("page"), "1");
  assert.equal(params.get("category"), "policy_state_banks");
  assert.equal(params.has("verification_status"), false);
});

function pollingContext({ race = false, unchanged = false } = {}) {
  const calls = [];
  const lead = { id: "new-chat-lead", verification_status: "pending", tier_code: "T1" };
  const payload = { items: [lead], total: 1, stats: { total_opportunities: 1, tier_counts: { T1: 1 } } };
  const state = { token: Symbol("pure-state-session"), futureRadar: {
    polling: false, jobsLoading: false, jobsRequestId: 1,
    jobsLoaded: unchanged, jobsError: "",
    lastEventId: null, activeRunTypes: new Set(), events: [],
    jobs: unchanged ? payload.items : [], opportunityStats: unchanged ? payload.stats : {},
  } };
  const result = { applied: 0, rendered: 0 };
  const context = {
    AbortController,
    FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
    state, document: { hidden: false },
    elements: { recruitmentDialog: { open: true }, futureRadarLiveState: { replaceChildren() {} } },
    futureRadarJobsQuery: () => "page=1&status=open",
    api: async (path) => {
      calls.push(path);
      if (path.startsWith("/future-radar/events")) return { items: [] };
      if (race) state.futureRadar.jobsRequestId += 1;
      return payload;
    },
    radarCollection: (value) => value?.items || [],
    applyFutureRadarJobsPayload: () => { result.applied += 1; },
    renderRecruitmentJobs: () => { result.rendered += 1; },
    renderRecruitmentDeadlineAlerts() {},
    eventIdentity: (event) => event.id,
    makeElement() { return {}; },
  };
  return { context, calls, result, state };
}

test("a newly imported chat lead appears in the main list without a verified event", async () => {
  const fixture = pollingContext();
  await vm.runInNewContext(`${functionSource("async function pollFutureRadarEvents(", "\nfunction stopFutureRadarPolling")}\npollFutureRadarEvents();`, fixture.context);
  assert.ok(fixture.calls.some((path) => path.startsWith("/future-radar/opportunities?")));
  assert.equal(fixture.result.applied, 1);
  assert.equal(fixture.result.rendered, 1);
  assert.equal(fixture.state.futureRadar.polling, false);
});

test("a newer filter or navigation request wins over an older background poll", async () => {
  const fixture = pollingContext({ race: true });
  await vm.runInNewContext(`${functionSource("async function pollFutureRadarEvents(", "\nfunction stopFutureRadarPolling")}\npollFutureRadarEvents();`, fixture.context);
  assert.equal(fixture.result.applied, 0);
  assert.equal(fixture.result.rendered, 0);
});

test("an unchanged poll keeps the current cards and expanded details stable", async () => {
  const fixture = pollingContext({ unchanged: true });
  await vm.runInNewContext(`${functionSource("async function pollFutureRadarEvents(", "\nfunction stopFutureRadarPolling")}\npollFutureRadarEvents();`, fixture.context);
  assert.equal(fixture.result.applied, 1);
  assert.equal(fixture.result.rendered, 0);
});
