export const STARFIELD_DEFINITIONS = Object.freeze([
  { code: "state_energy_resources", label: "央企能源/资源" },
  { code: "state_tech_telecom", label: "央企科技/通信" },
  { code: "tobacco_monopoly", label: "烟草/专卖体系" },
  { code: "policy_state_banks", label: "政策行/国有大行" },
  { code: "securities_public_funds_asset_management", label: "券商/公募/资管" },
  { code: "insurance_integrated_finance", label: "保险/综合金融" },
  { code: "internet_tech", label: "互联网大厂/中厂" },
  { code: "consumer_foreign_consulting", label: "快消/外企/咨询" },
  { code: "quant_private_hedge", label: "量化/私募/对冲" },
  { code: "big_four_professional_services", label: "四大/专业服务" },
]);

export const TIER_CODES = Object.freeze(["T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3"]);

const STARFIELD_CODES = new Set(STARFIELD_DEFINITIONS.map(({ code }) => code));
const STARFIELD_LABELS = new Map(STARFIELD_DEFINITIONS.map(({ code, label }) => [code, label]));

const LEGACY_CATEGORY_ALIASES = new Map([
  ["央国企", "state_energy_resources"],
  ["央企能源", "state_energy_resources"],
  ["央企资源", "state_energy_resources"],
  ["央国企科技", "state_tech_telecom"],
  ["央企科技", "state_tech_telecom"],
  ["央企通信", "state_tech_telecom"],
  ["央企交通", "state_tech_telecom"],
  ["烟草/专卖", "tobacco_monopoly"],
  ["烟草", "tobacco_monopoly"],
  ["中烟", "tobacco_monopoly"],
  ["专卖体系", "tobacco_monopoly"],
  ["银行/金融", "policy_state_banks"],
  ["政策性金融", "policy_state_banks"],
  ["政策行", "policy_state_banks"],
  ["国有大行", "policy_state_banks"],
  ["券商/基金", "securities_public_funds_asset_management"],
  ["券商/公募/资管", "securities_public_funds_asset_management"],
  ["券商", "securities_public_funds_asset_management"],
  ["基金", "securities_public_funds_asset_management"],
  ["公募", "securities_public_funds_asset_management"],
  ["资管", "securities_public_funds_asset_management"],
  ["保险/综合金融", "insurance_integrated_finance"],
  ["保险", "insurance_integrated_finance"],
  ["综合金融", "insurance_integrated_finance"],
  ["互联网企业", "internet_tech"],
  ["互联网", "internet_tech"],
  ["科技企业", "internet_tech"],
  ["快消/外企/咨询", "consumer_foreign_consulting"],
  ["快消/消费", "consumer_foreign_consulting"],
  ["外企/咨询", "consumer_foreign_consulting"],
  ["快消", "consumer_foreign_consulting"],
  ["消费", "consumer_foreign_consulting"],
  ["外企", "consumer_foreign_consulting"],
  ["咨询", "consumer_foreign_consulting"],
  ["量化私募", "quant_private_hedge"],
  ["量化/私募/对冲", "quant_private_hedge"],
  ["量化", "quant_private_hedge"],
  ["私募", "quant_private_hedge"],
  ["对冲基金", "quant_private_hedge"],
  ["四大/专业服务", "big_four_professional_services"],
  ["四大", "big_four_professional_services"],
  ["专业服务", "big_four_professional_services"],
]);

export function canonicalStarfieldCode(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (STARFIELD_CODES.has(raw)) return raw;
  return LEGACY_CATEGORY_ALIASES.get(raw) || null;
}

export function starfieldLabel(value) {
  const code = canonicalStarfieldCode(value);
  return code ? STARFIELD_LABELS.get(code) || code : String(value ?? "");
}

function listValues(value) {
  if (Array.isArray(value)) return value;
  if (value == null || value === "") return [];
  return [value];
}

export function jobStarfieldCategories(job = {}) {
  const primary = canonicalStarfieldCode(job.primary_category);
  if (primary) return [primary];
  return [...new Set(listValues(job.employer_categories).map(canonicalStarfieldCode).filter(Boolean))];
}

export function filterJobsByStarfields(jobs = [], selectedCategories = []) {
  const selected = new Set(selectedCategories.map(canonicalStarfieldCode).filter(Boolean));
  if (!selected.size) return [...jobs];
  return jobs.filter((job) => jobStarfieldCategories(job).some((code) => selected.has(code)));
}

export function jobTierBucket(job = {}) {
  if (job.tier_code == null) return "UNRANKED";
  if (TIER_CODES.includes(job.tier_code)) return job.tier_code;
  if (job.tier_code === "不建议投") return "BELOW_PRIORITY";
  const score = job.job_score ?? job.match_score;
  if (score != null && Number.isFinite(Number(score)) && Number(score) < 60) return "BELOW_PRIORITY";
  return "INVALID";
}

export function partitionJobsByPriority(jobs = []) {
  const result = {
    priorityJobs: [],
    belowPriorityJobs: [],
    invalidJobs: [],
  };
  jobs.forEach((job) => {
    const bucket = jobTierBucket(job);
    if (bucket === "BELOW_PRIORITY") result.belowPriorityJobs.push(job);
    else if (bucket === "INVALID") result.invalidJobs.push(job);
    else result.priorityJobs.push(job);
  });
  return result;
}

export function createCoalescedRadarReload({
  reload,
  isBusy = () => false,
  defer = (task) => setTimeout(task, 120),
  onError = () => {},
} = {}) {
  if (typeof reload !== "function") throw new TypeError("reload must be a function");
  let requested = false;
  let scheduled = false;
  let running = false;

  const schedule = () => {
    if (scheduled || running || !requested) return;
    scheduled = true;
    defer(drain);
  };

  async function drain() {
    scheduled = false;
    if (!requested) return;
    if (isBusy()) {
      schedule();
      return;
    }
    requested = false;
    running = true;
    try {
      await reload();
    } catch (error) {
      onError(error);
    } finally {
      running = false;
      schedule();
    }
  }

  return {
    request() {
      requested = true;
      schedule();
    },
  };
}

export function formatScoringFactors(factors) {
  if (Array.isArray(factors)) return factors.map(String).filter(Boolean).slice(0, 6);
  if (!factors || typeof factors !== "object") return [];
  const labels = {
    employer: "平台质量", employer_platform: "平台质量",
    role: "岗位价值", role_function: "岗位价值",
    career_value: "职业发展", job_conditions: "工作条件",
  };
  const finiteScore = (value) => {
    if (value == null || value === "") return null;
    const score = Number(value);
    return Number.isFinite(score) ? score : null;
  };
  return Object.entries(factors).flatMap(([key, value]) => {
    const label = (value && typeof value === "object" && !Array.isArray(value) && value.label)
      || labels[key]
      || key;
    if (Array.isArray(value)) return value.map((item) => `${label}：${item}`);
    if (value && typeof value === "object") {
      const score = finiteScore(value.score);
      const contribution = finiteScore(value.contribution);
      const weight = finiteScore(value.weight);
      if (score == null) return [`${label}：等待岗位信息`];
      const level = score >= 85 ? "高" : score >= 70 ? "较高" : score >= 55 ? "中等" : "较低";
      const weighted = contribution == null || weight == null ? "" : `，加权 ${contribution}/${weight}`;
      return [`${label}：${level}（${score}/100${weighted}）`];
    }
    return value == null || value === "" ? [] : [`${label}：${value}`];
  }).slice(0, 6);
}

export function buildFutureRadarJobsQuery({ page = 1, pageSize = 50, filters = {}, categories = [] } = {}) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== "" && value != null) params.set(key, String(value));
  });
  [...new Set(categories.map(canonicalStarfieldCode).filter(Boolean))].forEach((category) => {
    params.append("category", category);
  });
  return params.toString();
}

export const RADAR_ENRICHMENT_KEYS = Object.freeze([
  "match_score", "job_score", "tier_code", "score_breakdown",
  "employer_score", "role_score", "career_value_score", "job_condition_score",
  "scoring_factors", "scoring_status", "scoring_version",
  "positive_reasons", "negative_reasons", "fit_tags", "employer_categories",
  "organization_category", "industry_tags", "role_tags", "primary_category", "days_left",
]);

const SCORING_ENRICHMENT_KEYS = new Set([
  "match_score", "job_score", "tier_code", "score_breakdown",
  "employer_score", "role_score", "career_value_score", "job_condition_score",
  "scoring_factors", "scoring_status", "scoring_version",
  "positive_reasons", "negative_reasons", "fit_tags",
]);

function jobUrl(job = {}) {
  return job.application_url || job.official_url || job.url || "";
}

function jobIdentityKeys(job = {}) {
  const semantic = [job.company, job.title, job.city || job.region]
    .map((value) => String(value || "").trim().toLocaleLowerCase("zh-CN"))
    .join("|");
  return [job.id, job.external_id, jobUrl(job), semantic === "||" ? null : semantic]
    .filter(Boolean)
    .map(String);
}

export function mergeFutureRadarJobs(radarJobs = [], legacyJobs = []) {
  const legacyIndex = new Map();
  legacyJobs.forEach((job) => jobIdentityKeys(job).forEach((key) => legacyIndex.set(key, job)));
  return radarJobs.map((job) => {
    const legacy = jobIdentityKeys(job).map((key) => legacyIndex.get(key)).find(Boolean);
    if (!legacy) return job;
    const merged = { ...job };
    const explicitlyUnranked = Object.hasOwn(job, "tier_code") && job.tier_code === null;
    RADAR_ENRICHMENT_KEYS.forEach((key) => {
      // An explicit null from Radar means “not scored / not classified yet” and
      // must not be overwritten by a stale legacy estimate.
      if (explicitlyUnranked && SCORING_ENRICHMENT_KEYS.has(key)) return;
      if (merged[key] === undefined && legacy[key] !== undefined) merged[key] = legacy[key];
    });
    return merged;
  });
}
