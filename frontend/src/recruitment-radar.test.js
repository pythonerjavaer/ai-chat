import test from "node:test";
import assert from "node:assert/strict";

import {
  STARFIELD_DEFINITIONS,
  buildFutureRadarJobsQuery,
  createCoalescedRadarReload,
  filterJobsByStarfields,
  formatScoringFactors,
  jobTierBucket,
  mergeFutureRadarJobs,
  partitionJobsByPriority,
} from "./recruitment-radar.js";

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

test("Radar null scoring is not overwritten by stale legacy enrichment", () => {
  const [merged] = mergeFutureRadarJobs(
    [{ external_id: "same", company: "Example", title: "专项人才", tier_code: null, match_score: null }],
    [{ external_id: "same", company: "Example", title: "专项人才", tier_code: "T1", match_score: 82, role_score: 88 }],
  );
  assert.equal(merged.tier_code, null);
  assert.equal(merged.match_score, null);
  assert.equal(merged.role_score, undefined);
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
