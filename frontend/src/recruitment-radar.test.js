import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import {
  STARFIELD_DEFINITIONS,
  TIER_CODES,
  buildFutureRadarCandidatesQuery,
  buildFutureRadarJobsQuery,
  createCoalescedRadarReload,
  filterJobsByStarfields,
  formatOrganizationAssessment,
  formatRadarCooldown,
  formatScoringFactors,
  futureRadarActiveRunTypes,
  futureRadarAiSearchNotice,
  futureRadarCandidateVerification,
  futureRadarCoverageCopy,
  futureRadarRunErrorCopy,
  futureRadarRunSuccessCopy,
  futureRadarSourceErrorCopy,
  futureRadarTierQuery,
  futureRadarVisibleCategoryCount,
  isDefaultFutureRadarJobsView,
  jobTierBucket,
  mergeFutureRadarJobs,
  parseRadarRetryAfter,
  partitionJobsByPriority,
} from "./recruitment-radar.js";

test("balanced, focus and tier modes are explicit server projections, not page-only filters", () => {
  for (const tier of ["BALANCED", "FOCUS", "ALL", ...TIER_CODES, "UNRANKED", "BELOW_PRIORITY"]) {
    const params = new URLSearchParams(buildFutureRadarJobsQuery({ filters: futureRadarTierQuery(tier) }));
    assert.equal(params.get("balanced_only"), String(tier === "BALANCED"));
    assert.equal(params.get("priority_only"), String(tier === "FOCUS"));
    assert.equal(params.get("tier_code"), ["BALANCED", "FOCUS", "ALL"].includes(tier) ? null : tier);
  }
  assert.deepEqual(futureRadarTierQuery(), { balanced_only: true, priority_only: false, tier_code: "" });
});

test("category counts use post-filter opportunities and distinct display groups with explicit units", () => {
  const stats = { category_counts: { state_tech_telecom: 2956 },
    visible_category_counts: { state_tech_telecom: 12 },
    visible_category_company_counts: { state_tech_telecom: 1 } };
  assert.equal(futureRadarVisibleCategoryCount(stats, "state_tech_telecom", { view: "companies" }).text, "1组 · 12条");
  assert.equal(futureRadarVisibleCategoryCount(stats, "state_tech_telecom", { view: "jobs" }).text, "12条");
  assert.equal(futureRadarVisibleCategoryCount(stats, "policy_state_banks", { view: "companies" }).text, "0组 · 0条");
  assert.equal(futureRadarVisibleCategoryCount({ category_counts: { state_tech_telecom: 2956 } }, "state_tech_telecom").text, "—");
  assert.equal(futureRadarVisibleCategoryCount({ visible_category_counts: { state_tech_telecom: 12 } }, "state_tech_telecom", { view: "companies" }).text, "—组 · 12条");
  for (const status of ["loading", "error", "unavailable"]) {
    const result = futureRadarVisibleCategoryCount(stats, "state_tech_telecom", { view: "companies", status });
    assert.equal(result.text, "—");
    assert.equal(result.status, status);
    assert.doesNotMatch(result.title, /2956|1组|12条/);
  }
});

test("company coverage distinguishes configured scope from completed search", () => {
  const scope = { category_count: 10, list_entry_count: 218, target_count: 205 };
  const neverRun = futureRadarCoverageCopy(scope);
  assert.match(neverRun.scopeText, /全部 10 类.*218.*205/);
  assert.match(neverRun.resultText, /尚无/);
  assert.equal(neverRun.incomplete, true);
  const partial = futureRadarCoverageCopy(scope, {
    target_count: 205, searched_count: 200, failed_count: 5, employers_with_candidates_count: 60,
  });
  assert.match(partial.resultText, /200\/205.*5 家未完成.*60 家发现候选/);
  assert.equal(partial.incomplete, true);
  const complete = futureRadarCoverageCopy(scope, {
    target_count: 205, searched_count: 205, failed_count: 0, employers_with_candidates_count: 60,
  }, "healthy");
  assert.equal(complete.incomplete, false);
  assert.equal(futureRadarCoverageCopy(scope, { target_count: 205, searched_count: 205 }, "error").incomplete, true);
});

test("all ten machine-coded starfields filter structured API categories", () => {
  assert.equal(STARFIELD_DEFINITIONS.length, 10);
  const jobs = STARFIELD_DEFINITIONS.map(({ code }, index) => ({ id: `job-${index}`, primary_category: code }));

  STARFIELD_DEFINITIONS.forEach(({ code }, index) => {
    assert.deepEqual(filterJobsByStarfields(jobs, [code]), [jobs[index]]);
  });
  assert.deepEqual(filterJobsByStarfields(jobs, []), jobs);
});

test("legacy API employer categories map without inspecting company names", () => {
  const jobs = [
    { id: "public-fund", employer_categories: ["券商/基金"] },
    { id: "professional-services", employer_categories: ["四大/专业服务"] },
    { id: "unclassified", company: "Deloitte", employer_categories: [] },
  ];
  assert.deepEqual(
    filterJobsByStarfields(jobs, ["securities_public_funds_asset_management"]).map(({ id }) => id),
    ["public-fund"],
  );
  assert.deepEqual(
    filterJobsByStarfields(jobs, ["big_four_professional_services"]).map(({ id }) => id),
    ["professional-services"],
  );
});

test("a structured primary category prevents cross-tags from duplicating a job across starfields", () => {
  const privateFundJob = {
    id: "private-fund",
    primary_category: "quant_private_hedge",
    employer_categories: [
      "securities_public_funds_asset_management",
      "quant_private_hedge",
    ],
  };
  assert.deepEqual(
    filterJobsByStarfields([privateFundJob], ["quant_private_hedge"]),
    [privateFundJob],
  );
  assert.deepEqual(
    filterJobsByStarfields([privateFundJob], ["securities_public_funds_asset_management"]),
    [],
  );
});

test("only a nullish tier is unranked and below-priority scores stay separate", () => {
  assert.equal(jobTierBucket({ tier_code: null }), "UNRANKED");
  assert.equal(jobTierBucket({}), "UNRANKED");
  assert.equal(jobTierBucket({ tier_code: "T1" }), "T1");
  assert.equal(jobTierBucket({ tier_code: "不建议投", match_score: 58 }), "BELOW_PRIORITY");
  assert.equal(jobTierBucket({ tier_code: "", match_score: 58 }), "BELOW_PRIORITY");
  assert.equal(jobTierBucket({ tier_code: "unexpected", match_score: 72 }), "INVALID");
});

test("priority partition keeps unranked jobs visible and isolates below-60 jobs", () => {
  const jobs = [
    { id: "ranked", tier_code: "T1", match_score: 82 },
    { id: "unranked", tier_code: null, match_score: null },
    { id: "below", tier_code: "不建议投", match_score: 58 },
    { id: "invalid", tier_code: "unexpected", match_score: 72 },
  ];
  const partitioned = partitionJobsByPriority(jobs);
  assert.deepEqual(partitioned.priorityJobs.map(({ id }) => id), ["ranked", "unranked"]);
  assert.deepEqual(partitioned.belowPriorityJobs.map(({ id }) => id), ["below"]);
  assert.deepEqual(partitioned.invalidJobs.map(({ id }) => id), ["invalid"]);
});

test("profile-triggered Radar reloads coalesce and wait for an active request", async () => {
  const deferred = [];
  let busy = true;
  let reloads = 0;
  const coordinator = createCoalescedRadarReload({
    isBusy: () => busy,
    reload: async () => { reloads += 1; },
    defer: (task) => deferred.push(task),
  });

  coordinator.request();
  coordinator.request();
  assert.equal(deferred.length, 1);
  await deferred.shift()();
  assert.equal(reloads, 0);
  assert.equal(deferred.length, 1);

  busy = false;
  await deferred.shift()();
  assert.equal(reloads, 1);
  assert.equal(deferred.length, 0);
});

test("a profile change during Radar reload schedules one follow-up with the newest profile", async () => {
  const deferred = [];
  let reloads = 0;
  let releaseFirst;
  const coordinator = createCoalescedRadarReload({
    reload: () => {
      reloads += 1;
      if (reloads === 1) return new Promise((resolve) => { releaseFirst = resolve; });
      return Promise.resolve();
    },
    defer: (task) => deferred.push(task),
  });

  coordinator.request();
  const firstReload = deferred.shift()();
  await Promise.resolve();
  assert.equal(reloads, 1);
  coordinator.request();
  coordinator.request();
  assert.equal(deferred.length, 0);

  releaseFirst();
  await firstReload;
  assert.equal(deferred.length, 1);
  await deferred.shift()();
  assert.equal(reloads, 2);
  assert.equal(deferred.length, 0);
});

test("Future Radar query repeats category and resets to requested page", () => {
  const query = buildFutureRadarJobsQuery({
    page: 1,
    pageSize: 50,
    filters: { status: "open", q: "AI", company: "" },
    categories: ["quant_private_hedge", "big_four_professional_services", "quant_private_hedge"],
  });
  const params = new URLSearchParams(query);
  assert.equal(params.get("page"), "1");
  assert.equal(params.get("page_size"), "50");
  assert.equal(params.get("status"), "open");
  assert.equal(params.get("q"), "AI");
  assert.equal(params.has("company"), false);
  assert.deepEqual(params.getAll("category"), ["quant_private_hedge", "big_four_professional_services"]);
});

test("candidate pool query stays independent from official-job and T-tier filters", () => {
  const params = new URLSearchParams(buildFutureRadarCandidatesQuery({ page: 2, pageSize: 25 }));
  assert.equal(params.get("page"), "2");
  assert.equal(params.get("page_size"), "25");
  assert.deepEqual([...params.keys()].sort(), ["page", "page_size"]);
});

test("candidate verification normalizes review aliases without treating unknown discoveries as verified", () => {
  assert.equal(futureRadarCandidateVerification({ verification_status: "verified" }), "verified");
  assert.equal(futureRadarCandidateVerification({ review_status: "accepted" }), "verified");
  assert.equal(futureRadarCandidateVerification({ candidate_status: "expired" }), "closed");
  assert.equal(futureRadarCandidateVerification({ verification: "invalid" }), "rejected");
  assert.equal(futureRadarCandidateVerification({}), "pending");
  assert.equal(futureRadarCandidateVerification({ verification_status: "unknown-value" }), "pending");
});

test("Radar null scoring is not overwritten by stale legacy enrichment", () => {
  const [merged] = mergeFutureRadarJobs(
    [{ external_id: "same", company: "Example", title: "专项人才", tier_code: null, match_score: null }],
    [{ external_id: "same", company: "Example", title: "专项人才", tier_code: "T1", match_score: 82, role_score: 88,
      organization_assessment: { level: "group_headquarters", label: "集团总部" } }],
  );
  assert.equal(merged.tier_code, null);
  assert.equal(merged.match_score, null);
  assert.equal(merged.role_score, undefined);
  assert.equal(merged.organization_assessment, undefined);
});

test("Radar enriches ranked organization details without replacing a current or explicit null assessment", () => {
  const current = { level: "city_branch", label: "地市级分支机构" };
  const legacy = { level: "group_headquarters", label: "集团总部" };
  const jobs = mergeFutureRadarJobs(
    [
      { id: "missing", tier_code: "T1" },
      { id: "current", tier_code: "T2", organization_assessment: current },
      { id: "null", tier_code: "T1", organization_assessment: null },
    ],
    ["missing", "current", "null"].map((id) => ({ id, organization_assessment: legacy })),
  );
  assert.equal(jobs[0].organization_assessment, legacy);
  assert.equal(jobs[1].organization_assessment, current);
  assert.equal(jobs[2].organization_assessment, null);
});

test("recruitment programs never inherit legacy scores or organization assessments", () => {
  for (const program of [
    { listing_kind: "recruitment_program" },
    { scoring_status: "unscored_program_listing" },
    { tier_code: undefined },
  ]) {
    const [merged] = mergeFutureRadarJobs(
      [{ id: "program", ...program }],
      [{ id: "program", tier_code: "T0", job_score: 98, employer_score: 100,
        organization_assessment: { level: "group_headquarters", label: "集团总部" } }],
    );
    assert.equal(jobTierBucket(merged), "UNRANKED");
    assert.equal(merged.job_score, undefined);
    assert.equal(merged.employer_score, undefined);
    assert.equal(merged.organization_assessment, undefined);
  }
});

test("Radar keeps non-duplicate verified carryover jobs in the default pool", () => {
  const merged = mergeFutureRadarJobs(
    [{ external_id: "radar-1", company: "实时企业", title: "分析岗", tier_code: null }],
    [
      { external_id: "radar-1", company: "实时企业", title: "分析岗", tier_code: "T1" },
      { external_id: "verified-2", company: "已核验企业", title: "管培岗", tier_code: "T2" },
    ],
  );
  assert.deepEqual(merged.map((job) => job.external_id), ["radar-1", "verified-2"]);
  assert.equal(merged[0].tier_code, null);
});

test("the default opportunity view includes open and unknown active records", () => {
  assert.equal(isDefaultFutureRadarJobsView({ status: "active", sort: "changed", q: "" }), true);
  assert.equal(isDefaultFutureRadarJobsView({ status: "open", sort: "changed", q: "" }), false);
  assert.equal(isDefaultFutureRadarJobsView({ status: "all", sort: "changed", q: "" }), false);
  assert.equal(isDefaultFutureRadarJobsView({ status: "active", sort: "changed", company: "某公司" }), false);
});

test("manual scan feedback distinguishes active runs from provider rate limits", () => {
  assert.equal(parseRadarRetryAfter("61"), 61);
  assert.equal(formatRadarCooldown(61), "1 分 01 秒");
  assert.match(
    futureRadarRunErrorCopy({ status: 429, retryAfter: "61", message: "rate limited" }, "deep"),
    /深度发现信源.*外部服务速率限制.*Quick Scan 不受影响/,
  );
  assert.match(futureRadarRunErrorCopy({ status: 409 }, "quick"), /Quick Scan 已在扫描中.*不会创建重复任务/);
  assert.match(futureRadarRunErrorCopy({ message: "请求超时，请稍后重试。" }), /服务端可能仍在继续.*岗位池不会被清空/);
  assert.equal(
    futureRadarRunErrorCopy({ status: 428, message: "provider detail must not render" }),
    "隐私政策已更新，请先重新同意当前版本后再启动扫描。",
  );
  assert.equal(
    futureRadarRunSuccessCopy({ status: "success", sources_checked: 0 }, 7),
    "扫描完成：当前没有到期信源，未重复抓取；实时岗位池仍为 7 条。",
  );
  assert.match(
    futureRadarRunSuccessCopy({ status: "partial_success", sources_checked: 3, sources_succeeded: 2, sources_failed: 1, new_jobs: 4, updated_jobs: 2 }, 16),
    /2\/3 个信源完成.*新增 4、更新 2、关闭 0.*16 条/,
  );
  assert.match(
    futureRadarRunSuccessCopy({ status: "partial_success", sources_checked: 2, sources_succeeded: 1, sources_skipped: 1 }, 16),
    /1\/2 个信源完成，1 个运行中信源已跳过/,
  );
});

test("active run types restore per-type locks from the dashboard", () => {
  assert.deepEqual(futureRadarActiveRunTypes({ active_run_types: ["quick"] }), ["quick"]);
  assert.deepEqual(futureRadarActiveRunTypes({ active_runs: [{ scan_type: "deep" }] }), ["deep"]);
  assert.deepEqual(futureRadarActiveRunTypes({ active_run_types: ["scheduled"] }), ["scheduled"]);
  assert.deepEqual(futureRadarActiveRunTypes({ run_in_progress: true }), ["quick", "deep"]);
  assert.deepEqual(futureRadarActiveRunTypes({ run_in_progress: false }), []);
});

test("OpenAI quota failures produce a safe actionable notice without leaking diagnostics", () => {
  const failure = {
    status: "partial_success",
    sources_checked: 2,
    sources_succeeded: 1,
    sources_failed: 1,
    errors: [{
      source_id: "openai-public-web-search",
      message: "Error 429 insufficient_quota account balance; secret-sentinel-must-not-render",
    }],
  };
  const notice = futureRadarAiSearchNotice(failure);
  const runCopy = futureRadarRunSuccessCopy(failure, 9);
  const sourceCopy = futureRadarSourceErrorCopy({
    id: "openai-public-web-search",
    last_error: failure.errors[0].message,
  });
  assert.match(notice, /OpenAI 搜索暂不可用（API 额度不足）.*已核验官网源仍会继续扫描/);
  assert.match(runCopy, /API 额度不足.*官网源仍会继续扫描/);
  assert.match(sourceCopy, /API 额度不足.*官网源仍会继续扫描/);
  assert.doesNotMatch(`${notice} ${runCopy} ${sourceCopy}`, /secret-sentinel-must-not-render|insufficient_quota/);
});

test("unknown scan failures never echo raw server diagnostics", () => {
  const copy = futureRadarRunErrorCopy({
    status: 500,
    message: "Traceback with private-service-token=secret-value",
  });
  assert.equal(copy, "扫描未能启动，请稍后重试；当前岗位池不会被清空。");
  assert.doesNotMatch(copy, /Traceback|secret-value/);
});

test("structured four-part scoring factors render concise explanations instead of object text", () => {
  assert.deepEqual(
    formatScoringFactors({
      employer_platform: { label: "平台质量", weight: 35, score: 82, contribution: 29 },
      role_function: { label: "岗位职能与匹配", weight: 45, score: 91, contribution: 41 },
      career_value: { label: "职业发展与退出价值", weight: 10, score: 78, contribution: 8 },
      job_conditions: { label: "薪酬、地点与工作条件", weight: 10, score: 60, contribution: 6 },
    }),
    [
      "平台质量：较高（82/100，加权 29/35）",
      "岗位职能与匹配：高（91/100，加权 41/45）",
      "职业发展与退出价值：较高（78/100，加权 8/10）",
      "薪酬、地点与工作条件：中等（60/100，加权 6/10）",
    ],
  );
});

test("organization assessment is absent for old API responses instead of inventing a level", () => {
  for (const value of [undefined, null, "", "group_headquarters", [], {}, { unrelated: "value" }]) {
    assert.deepEqual(formatOrganizationAssessment(value), []);
  }
});

test("organization detail distinguishes text evidence and platform normalization from final T score", () => {
  assert.deepEqual(formatOrganizationAssessment({
    level: "province_branch", label: "省级分支机构", confidence: "explicit",
    base_platform_points: 16, platform_points: 12, platform_adjustment: -4,
    is_group_headquarters: false, basis: "招聘单位名称", evidence: ["单位名称明确标注省分公司"],
    note: "按实际用人单位调整平台分。",
  }), [
    "招聘单位层级：省级分支机构（文本明确线索）",
    "集团/平台基准 → 实际单位平台分：100/100 → 75/100（仅平台维度，非最终 T 分）",
    "识别依据：招聘单位名称；单位名称明确标注省分公司",
    "说明：按实际用人单位调整平台分。 层级识别仅供评分参考，不代表官方核验。",
  ]);
});

test("inferred and unknown organization levels stay qualified and do not assume group headquarters", () => {
  const inferred = formatOrganizationAssessment({
    level: "city_branch", label: "地市级分支机构", confidence: "inferred",
    base_platform_points: "14.4", platform_points: "10.4",
  });
  assert.equal(inferred[0], "招聘单位层级：地市级分支机构（依据线索推断）");
  assert.match(inferred[1], /90\/100 → 65\/100/);
  assert.match(inferred.at(-1), /不代表官方核验/);
  const unknown = formatOrganizationAssessment({
    level: "unknown", label: "单位层级未明确", confidence: "unknown",
    base_platform_points: 16, platform_points: 16,
  });
  assert.match(unknown[0], /招聘单位层级：单位层级未明确.*层级信息不足，待核对/);
  assert.doesNotMatch(unknown.join(" "), /集团总部/);
  const unspecified = formatOrganizationAssessment({
    level: "unspecified", label: "组织层级待核验", confidence: "unknown", basis: "none", evidence: [],
    base_platform_points: 14, platform_points: 14,
  });
  assert.match(unspecified[0], /招聘单位层级：组织层级待核验.*层级信息不足，待核对/);
  assert.doesNotMatch(unspecified.join(" "), /识别依据|none/);
  const missingConfidence = formatOrganizationAssessment({ level: "group_headquarters", label: "集团总部" });
  assert.match(missingConfidence[0], /层级未知.*待核对/);
  assert.doesNotMatch(missingConfidence.join(" "), /集团总部/);
});

test("organization platform points reject malformed values instead of fabricating zero or final scores", () => {
  const assessment = { level: "city_branch", label: "地市级分支机构", confidence: "explicit", base_platform_points: 16 };
  assert.match(formatOrganizationAssessment({ ...assessment, platform_points: 0 })[1], /100\/100 → 0\/100/);
  assert.match(formatOrganizationAssessment({ ...assessment, platform_points: 16 })[1], /100\/100 → 100\/100/);
  for (const value of [null, undefined, "", " ", true, [], {}, -1, 17, Infinity, NaN, "invalid"]) {
    assert.match(formatOrganizationAssessment({ ...assessment, platform_points: value })[1], /100\/100 → —/);
  }
});

test("legacy organization points use Python half-to-even rounding on the full 0–16 range", () => {
  const expectedScores = [0, 6, 12, 19, 25, 31, 38, 44, 50, 56, 62, 69, 75, 81, 88, 94, 100];
  expectedScores.forEach((score, points) => {
    const lines = formatOrganizationAssessment({ base_platform_points: points, platform_points: points });
    assert.equal(lines[1], `集团/平台基准 → 实际单位平台分：${score}/100 → ${score}/100（仅平台维度，非最终 T 分）`);
  });
});

test("organization details prefer valid server platform scores including zero over legacy points", () => {
  const assessment = { base_platform_points: 14, platform_points: 10 };
  for (const [base, actual] of [[92, 68], [0, 0], [100, 100], ["88", "62"]]) {
    const lines = formatOrganizationAssessment({ ...assessment, base_platform_score: base, platform_score: actual });
    assert.equal(lines[1], `集团/平台基准 → 实际单位平台分：${base}/100 → ${actual}/100（仅平台维度，非最终 T 分）`);
  }
  assert.match(formatOrganizationAssessment({ base_platform_score: 88, platform_score: 62 })[1], /88\/100 → 62\/100/);
});

test("invalid server platform scores fall back independently to valid legacy points", () => {
  const assessment = { base_platform_points: 14, platform_points: 10 };
  for (const value of [null, undefined, "", " ", true, [], {}, -1, 101, Infinity, NaN, "invalid"]) {
    const baseInvalid = formatOrganizationAssessment({ ...assessment, base_platform_score: value, platform_score: 70 });
    const actualInvalid = formatOrganizationAssessment({ ...assessment, base_platform_score: 90, platform_score: value });
    assert.match(baseInvalid[1], /88\/100 → 70\/100/);
    assert.match(actualInvalid[1], /90\/100 → 62\/100/);
  }
});

test("organization evidence is concise plain text and ignores object-shaped evidence", () => {
  const lines = formatOrganizationAssessment({
    level: "subsidiary", label: "独立子公司", confidence: "inferred",
    basis: "  单位名称\n及公告  ", evidence: [{ text: "unsupported evidence object" }, "公开招聘文字"],
    note: "说明".repeat(150),
  });
  assert.equal(lines[2], "识别依据：单位名称 及公告；公开招聘文字");
  assert.match(lines[3], /… 层级识别仅供评分参考/);
  assert.ok(lines[3].length < 200);
  assert.doesNotMatch(lines.join(" "), /\[object Object\]|unsupported evidence object/);
});

function renderScoringDetail(job) {
  const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
  const extract = (startMarker, endMarker) => {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.ok(start >= 0 && end > start);
    return source.slice(start, end);
  };
  class Element {
    constructor(tag) {
      this.tag = tag;
      this.className = "";
      this.children = [];
      this.dataset = {};
      this.classList = { toggle() {} };
      this._text = "";
    }
    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
    setAttribute() {}
    addEventListener() {}
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((node) => node.textContent).join(" "); }
    set innerHTML(value) { assert.fail(`Unexpected HTML write: ${value}`); }
  }
  const elements = { recruitmentJobs: new Element("section") };
  const context = {
    elements, TIER_CODES, formatOrganizationAssessment, jobTierBucket, partitionJobsByPriority,
    document: { createElement: (tag) => new Element(tag) },
    state: { recruitmentTierFilter: "ALL", futureRadar: { jobsLoaded: true, totalJobs: 1 } },
    futureRadarSelectionIsPending: () => false,
    futureRadarDisplayJobs: () => [job],
    filterRecruitmentByStarfield: (jobs) => jobs,
    futureRadarOpportunityDateCopy: () => ({ verified: false, closing: "时间待确认" }),
    finiteRadarScore: (value) => value == null ? null : Number(value),
    recruitmentVerification: () => "pending",
    futureRadarOpportunitySource: () => ({ tone: "pending", label: "公开线索", description: "来源待核对" }),
    recruitmentJobUrl: () => "",
    recruitmentScoringFactors: (item) => formatScoringFactors(item.scoring_factors),
    radarStatusCopy: () => "待核对",
    formatRadarTime: () => "待记录",
    sourceDisplayValue: (_value, fallback) => fallback,
  };
  vm.runInNewContext([
    extract("function makeElement(", "\nfunction setCrossExamCounts"),
    extract("function renderRecruitmentJobs(", "\nasync function addRecruitmentWatchFromJob"),
    "renderRecruitmentJobs();",
  ].join("\n"), context);
  const descendants = (node) => [node, ...node.children.flatMap(descendants)];
  return descendants(elements.recruitmentJobs).find((node) => node.className === "job-tier-reason");
}

test("T detail renders organization evidence as text while retaining the four scoring factors", () => {
  const detail = renderScoringDetail({
    tier_code: "T1", job_score: 83, employer_score: 75, role_score: 90, career_value_score: 80, job_condition_score: 60,
    organization_assessment: {
      level: "province_branch", label: "省级分支机构", confidence: "explicit",
      base_platform_points: 16, platform_points: 12,
      evidence: "<img src=x> 公告文字",
    },
    scoring_factors: { employer_platform: { label: "平台质量", weight: 35, score: 75, contribution: 26 } },
  });
  assert.match(detail.textContent, /招聘单位层级：省级分支机构/);
  assert.match(detail.textContent, /100\/100 → 75\/100.*非最终 T 分/);
  assert.match(detail.textContent, /<img src=x> 公告文字/);
  assert.match(detail.textContent, /FINAL SCORE · 83 \/ 100/);
  assert.match(detail.textContent, /EMPLOYER SCORE · 75.*ROLE SCORE · 90.*CAREER VALUE · 80.*JOB CONDITIONS · 60/);
  assert.match(detail.textContent, /平台质量：较高（75\/100，加权 26\/35）/);
});

test("organization detail and EMPLOYER SCORE share the same rounded score for new and legacy payloads", () => {
  for (const scores of [{}, { base_platform_score: 88, platform_score: 62 }]) {
    const detail = renderScoringDetail({
      tier_code: "T2", job_score: 72, employer_score: 62,
      organization_assessment: {
        level: "city_branch", label: "地市分支", confidence: "explicit",
        base_platform_points: 14, platform_points: 10, ...scores,
      },
    });
    assert.match(detail.textContent, /EMPLOYER SCORE · 62/);
    assert.match(detail.textContent, /集团\/平台基准 → 实际单位平台分：88\/100 → 62\/100/);
    assert.doesNotMatch(detail.textContent, /62\.5|63\/100/);
  }
});

test("unranked and old-API T details never display stale or invented organization scoring", () => {
  const stale = {
    tier_code: null, job_score: 98, employer_score: 100,
    organization_assessment: { level: "group_headquarters", label: "集团总部", confidence: "explicit" },
    scoring_factors: { employer_platform: { label: "旧平台评分", score: 100 } },
  };
  for (const job of [stale, { ...stale, tier_code: "T0", listing_kind: "recruitment_program" }]) {
    const detail = renderScoringDetail(job);
    assert.match(detail.textContent, /FINAL TIER · 未评分.*FINAL SCORE · —.*EMPLOYER SCORE · —/);
    assert.doesNotMatch(detail.textContent, /招聘单位层级|集团总部|旧平台评分|98 \/ 100/);
  }
  const legacyDetail = renderScoringDetail({ tier_code: "T1", job_score: 83 });
  assert.match(legacyDetail.textContent, /FINAL SCORE · 83 \/ 100/);
  assert.doesNotMatch(legacyDetail.textContent, /招聘单位层级|实际单位平台分|层级未知/);
});
