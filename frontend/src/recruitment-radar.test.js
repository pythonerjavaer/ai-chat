import test from "node:test";
import assert from "node:assert/strict";

import {
  STARFIELD_DEFINITIONS,
  buildFutureRadarCandidatesQuery,
  buildFutureRadarJobsQuery,
  createCoalescedRadarReload,
  filterJobsByStarfields,
  formatRadarCooldown,
  formatScoringFactors,
  futureRadarActiveRunTypes,
  futureRadarAiSearchNotice,
  futureRadarCandidateVerification,
  futureRadarCoverageCopy,
  futureRadarRunErrorCopy,
  futureRadarRunSuccessCopy,
  futureRadarSourceErrorCopy,
  isDefaultFutureRadarJobsView,
  jobTierBucket,
  mergeFutureRadarJobs,
  parseRadarRetryAfter,
  partitionJobsByPriority,
} from "./recruitment-radar.js";

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
    [{ external_id: "same", company: "Example", title: "专项人才", tier_code: "T1", match_score: 82, role_score: 88 }],
  );
  assert.equal(merged.tier_code, null);
  assert.equal(merged.match_score, null);
  assert.equal(merged.role_score, undefined);
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
