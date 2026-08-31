export const STARFIELD_DEFINITIONS = Object.freeze([
  { code: "state_energy_resources", label: "央企能源/资源" },
  { code: "state_tech_telecom", label: "央企科技/通信" },
  { code: "tobacco_monopoly", label: "烟草/专卖体系" },
  { code: "policy_state_banks", label: "银行与政策性金融" },
  { code: "securities_public_funds_asset_management", label: "券商/公募/资管" },
  { code: "insurance_integrated_finance", label: "保险/综合金融" },
  { code: "internet_tech", label: "互联网大厂/中厂" },
  { code: "consumer_foreign_consulting", label: "快消/外企/咨询" },
  { code: "quant_private_hedge", label: "量化/私募/对冲" },
  { code: "big_four_professional_services", label: "四大/专业服务" },
]);

export const TIER_CODES = Object.freeze(["T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3"]);
export const DEFAULT_FUTURE_RADAR_STATUS = "active";
// Cold full-pool ranking can take over a minute. This is a read deadline only,
// not a scan interval, run lock or a change to the default API/auth timeout.
export const FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS = 120_000;

export function futureRadarTierQuery(tier = "FOCUS") {
  return {
    priority_only: tier === "FOCUS",
    tier_code: ["FOCUS", "ALL"].includes(tier) ? "" : tier,
  };
}

export function futureRadarVisibleCategoryCount(stats = {}, category, { view = "jobs", status = "ready" } = {}) {
  if (status !== "ready") {
    return { text: "—", status, title: status === "error"
      ? "当前筛选读取失败；上次成功快照不作为当前数量"
      : status === "loading" ? "正在读取当前筛选的统计" : "尚未读取当前筛选的统计" };
  }
  const value = (counts) => {
    if (!counts || typeof counts !== "object" || Array.isArray(counts)) return null;
    const raw = Object.hasOwn(counts, category) ? counts[category] : 0;
    if (raw == null || raw === "" || !Number.isFinite(Number(raw)) || Number(raw) < 0) return null;
    return Math.floor(Number(raw)).toLocaleString("zh-CN");
  };
  // The legacy category_counts is pre-tier: it cannot represent this view.
  const opportunities = value(stats.visible_category_counts);
  const companies = value(stats.visible_category_company_counts);
  if (opportunities == null) return { text: "—", status: "unavailable", title: "当前筛选统计尚不可用；未使用旧分类总数代替" };
  return {
    text: view === "companies" ? `${companies ?? "—"}组 · ${opportunities}条` : `${opportunities}条`,
    status: view === "companies" && companies == null ? "unavailable" : "ready",
    title: view === "companies"
      ? "当前筛选范围的企业展示分组数与机会条数；集团展示分组不改变实际招聘单位"
      : "当前筛选范围的机会条数；不是监控企业数量",
  };
}

export function futureRadarCoverageCopy(scope = {}, coverage = null, status = "pending") {
  const count = (value) => Math.max(0, Math.floor(Number(value) || 0));
  const targets = count(scope.target_count);
  const scopeText = targets
    ? `搜索范围：左侧全部 ${count(scope.category_count)} 类 · ${count(scope.list_entry_count)} 个名录条目 · 别名合并后 ${targets} 家企业`
    : "正在读取完整企业搜索范围";
  if (!coverage || !Number.isFinite(Number(coverage.target_count))) {
    return { scopeText, resultText: "尚无逐企业扫描完成记录。深度扫描将依次搜索全部名录，结果进入此池。", incomplete: true };
  }
  const total = count(coverage.target_count);
  const searched = Math.min(total, count(coverage.searched_count));
  const failed = count(coverage.failed_count);
  const prefix = status === "error" ? "最新尝试失败；保留上次完成记录：" : "上次完成记录：";
  return {
    scopeText,
    resultText: `${prefix}${searched}/${total} 家完成搜索与解析 · ${failed} 家未完成 · ${count(coverage.employers_with_candidates_count)} 家发现候选。企业搜索覆盖不等于每家都有在招岗位。`,
    incomplete: failed > 0 || searched < total || status === "error",
  };
}

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
  if (job.listing_kind === "recruitment_program" || job.scoring_status === "unscored_program_listing") return "UNRANKED";
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

export function formatOrganizationAssessment(assessment) {
  if (!assessment || typeof assessment !== "object" || Array.isArray(assessment)) return [];
  const conciseText = (value, limit = 160) => {
    const text = listValues(value)
      .filter((item) => typeof item === "string")
      .map((item) => item.replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .slice(0, 3)
      .join("；");
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  };
  const finiteRange = (value, maximum) => {
    if (typeof value !== "number" && typeof value !== "string") return null;
    if (typeof value === "string" && !value.trim()) return null;
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 && number <= maximum ? number : null;
  };
  const platformScore = (value) => {
    const points = finiteRange(value, 16);
    if (points == null) return null;
    const normalized = points / 16 * 100;
    const lower = Math.floor(normalized);
    // Legacy payloads need the backend's Python round() rule before weighting.
    return normalized - lower === 0.5 ? lower + lower % 2 : Math.round(normalized);
  };
  const level = conciseText(assessment.level, 60);
  const label = conciseText(assessment.label, 60);
  const basisText = conciseText(assessment.basis);
  const basis = ["none", "unknown"].includes(basisText.toLowerCase()) ? "" : basisText;
  const evidence = conciseText(assessment.evidence);
  const note = conciseText(assessment.note);
  const baseScore = finiteRange(assessment.base_platform_score, 100)
    ?? platformScore(assessment.base_platform_points);
  const actualScore = finiteRange(assessment.platform_score, 100)
    ?? platformScore(assessment.platform_points);
  if (!level && !label && !basis && !evidence && !note && baseScore == null && actualScore == null) return [];

  const confidence = conciseText(assessment.confidence).toLowerCase();
  const unresolvedLevel = ["unknown", "unspecified"].includes(level);
  const unknown = !level || unresolvedLevel || !label || !["explicit", "inferred"].includes(confidence);
  const levelLabel = unknown ? (unresolvedLevel && label ? label : "层级未知") : label;
  const confidenceCopy = unknown ? "层级信息不足，待核对"
    : confidence === "explicit" ? "文本明确线索" : "依据线索推断";
  const displayScore = (value) => value == null ? "—" : `${value}/100`;
  const lines = [
    `招聘单位层级：${levelLabel}（${confidenceCopy}）`,
    `集团/平台基准 → 实际单位平台分：${displayScore(baseScore)} → ${displayScore(actualScore)}（仅平台维度，非最终 T 分）`,
  ];
  const explanation = conciseText([...new Set([basis, evidence].filter(Boolean))]);
  if (explanation) lines.push(`识别依据：${explanation}`);
  lines.push(`说明：${note ? `${note} ` : ""}层级识别仅供评分参考，不代表官方核验。`);
  return lines;
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

export function buildFutureRadarCompanyJobsQuery({ parentQuery = "", companyKey, page = 1, pageSize = 50 } = {}) {
  if (!companyKey) throw new TypeError("companyKey is required");
  // Clone the successful parent selection, retaining repeated categories and
  // every search/T/status filter. Never expand from an unrelated saved page.
  const params = new URLSearchParams(parentQuery);
  params.set("view", "jobs");
  params.set("company_key", String(companyKey));
  params.set("page", String(Math.max(1, Number(page) || 1)));
  params.set("page_size", String(Math.max(1, Math.min(100, Number(pageSize) || 50))));
  params.set("compact", "true");
  return params.toString();
}

export function buildFutureRadarCandidatesQuery({ page = 1, pageSize = 50 } = {}) {
  return new URLSearchParams({
    page: String(Math.max(1, Number(page) || 1)),
    page_size: String(Math.max(1, Number(pageSize) || 50)),
  }).toString();
}

export function futureRadarCandidateVerification(candidate = {}) {
  const raw = String(
    candidate.verification_status
      ?? candidate.verification
      ?? candidate.review_status
      ?? candidate.candidate_status
      ?? "pending",
  ).trim().toLowerCase();
  if (["verified", "accepted", "approved"].includes(raw)) return "verified";
  if (["rejected", "invalid", "failed"].includes(raw)) return "rejected";
  if (["closed", "expired"].includes(raw)) return "closed";
  if (["conflicted", "conflict"].includes(raw)) return "conflicted";
  return "pending";
}

export function futureRadarOpportunitySource(job = {}) {
  const verification = futureRadarCandidateVerification(job);
  if (verification === "verified") {
    return { label: "官网已确认", tone: "healthy", description: "该机会已由官方招聘来源确认；具体申请条件以原文为准。" };
  }
  const sourceValues = [job.source_id, job.source, job.source_name, job.discovery_source,
    ...listValues(job.sources), ...listValues(job.discovered_by), ...listValues(job.discovery_sources)];
  const names = sourceValues.map((source) => typeof source === "string"
    ? source
    : [source?.source_id, source?.id, source?.name, source?.source_name, source?.platform].filter(Boolean).join(" ")).join(" ");
  const label = /chatgpt|chat[-_ ]?bridge|聊天|受控同步/i.test(names)
    ? "聊天线索"
    : /openai|search|搜索|发现|discovery/i.test(names) ? "搜索发现" : "公开线索";
  return {
    label,
    tone: verification === "conflicted" ? "warning" : "pending",
    description: verification === "conflicted"
      ? "这条机会已进入主池；不同来源的关键信息有差异，日期与条件请对照原文。"
      : "这条机会来自招聘线索，已直接进入主池；官网尚未确认全部岗位信息与日期。",
  };
}

export function futureRadarPublicOpportunityUrl(job = {}) {
  const sources = [...listValues(job.sources), ...listValues(job.discovered_by)];
  const candidates = [job.application_url, job.official_url, job.source_url, job.candidate_url, job.url,
    ...sources.map((source) => source?.source_url || source?.url)];
  for (const value of candidates) {
    if (typeof value !== "string" || !value) continue;
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase();
      if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) continue;
      if (!host.includes(".") || /^(localhost|127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host)) continue;
      if (host === "chatgpt.com" || host.endsWith(".chatgpt.com") || /[?&](token|api[_-]?key|password|authorization|cookie)=/i.test(url.search)) continue;
      return url.href;
    } catch { /* Ignore malformed source links; do not fabricate an endpoint. */ }
  }
  return "";
}

export function futureRadarOpportunityDateCopy(job = {}) {
  const verified = futureRadarCandidateVerification(job) === "verified";
  const dateValue = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? String(value) : null;
  const opening = dateValue(job.opening_date);
  const closing = dateValue(job.closing_date);
  return {
    opening: opening ? `${verified ? "开放" : "来源标注开放"} ${opening}` : "开放日期未标注",
    closing: closing ? `${verified ? "截止" : "来源标注截止"} ${closing}` : "截止日期未标注",
    verified,
  };
}

export function isDefaultFutureRadarJobsView(filters = {}) {
  const defaults = { status: DEFAULT_FUTURE_RADAR_STATUS, sort: "changed" };
  return Object.entries(filters).every(([key, value]) => {
    if (key in defaults) return (value || defaults[key]) === defaults[key];
    return value == null || value === "";
  });
}

export function futureRadarOpportunityErrorCopy(error, hasSnapshot = false) {
  const status = Number(error?.status);
  if (status === 401) return "主机会池登录状态已失效（HTTP 401）。请重新登录后查看机会。";
  const http = Number.isInteger(status) && status >= 400 && status <= 599 ? `（HTTP ${status}）` : "";
  const timedOut = error?.code === "REQUEST_TIMEOUT" || error?.name === "AbortError"
    || /^请求超时/.test(String(error?.message || ""));
  const reason = http || (timedOut ? "（读取超时，服务暂未返回）" : "");
  return hasSnapshot
    ? `主机会池刷新失败${reason}。当前保留上次成功的主池数据，请点击“刷新机会”重试。`
    : `主机会池加载失败${reason}。请点击“刷新机会”重试。`;
}

export function parseRadarRetryAfter(value, now = Date.now()) {
  if (value == null || value === "") return 0;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.ceil(seconds));
  const retryAt = Date.parse(String(value));
  return Number.isFinite(retryAt) ? Math.max(0, Math.ceil((retryAt - now) / 1000)) : 0;
}

export function formatRadarCooldown(seconds) {
  const remaining = Math.max(0, Math.ceil(Number(seconds) || 0));
  const minutes = Math.floor(remaining / 60);
  const trailingSeconds = remaining % 60;
  if (!minutes) return `${trailingSeconds} 秒`;
  if (!trailingSeconds) return `${minutes} 分钟`;
  return `${minutes} 分 ${String(trailingSeconds).padStart(2, "0")} 秒`;
}

function normalizeRadarRunType(value) {
  const raw = String(value ?? "").trim().toLowerCase();
  if (!raw) return null;
  if (raw === "quick" || /(^|[_-])quick([_-]|$)/.test(raw)) return "quick";
  if (raw === "deep" || /(^|[_-])deep([_-]|$)/.test(raw)) return "deep";
  if (raw === "scheduled" || /(^|[_-])scheduled([_-]|$)/.test(raw)) return "scheduled";
  return null;
}

export function futureRadarActiveRunTypes(dashboard = {}) {
  const direct = dashboard.active_run_types ?? dashboard.active_scan_types;
  const activeRuns = Array.isArray(dashboard.active_runs) ? dashboard.active_runs : [];
  const values = [
    ...(Array.isArray(direct) ? direct : direct ? [direct] : []),
    ...activeRuns.map((run) => run?.scan_type ?? run?.run_type ?? run?.trigger_type ?? run),
  ];
  const types = [...new Set(values.map(normalizeRadarRunType).filter(Boolean))];
  if (types.length) return types;

  const lastRun = dashboard.last_scan || dashboard.last_run || dashboard.latest_run || {};
  const lastStatus = String(lastRun.status || dashboard.status || "").toLowerCase();
  const explicitlyRunning = dashboard.run_in_progress === true
    || dashboard.is_running === true
    || /running|in_progress/.test(lastStatus);
  if (!explicitlyRunning) return [];
  const inferred = normalizeRadarRunType(lastRun.scan_type || lastRun.run_type || lastRun.trigger_type);
  // Older servers exposed only a global boolean. Disable both manual entries in
  // that ambiguous legacy state; current servers always expose active_run_types.
  return inferred ? [inferred] : ["quick", "deep"];
}

function radarDiagnosticText(value, depth = 0, seen = new WeakSet()) {
  if (depth > 4 || value == null) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (value instanceof Error) return value.message || "";
  if (typeof value !== "object" || seen.has(value)) return "";
  seen.add(value);
  const entries = Array.isArray(value) ? value : Object.values(value);
  return entries.map((item) => radarDiagnosticText(item, depth + 1, seen)).filter(Boolean).join(" ");
}

export function futureRadarAiSearchNotice(value = {}) {
  const diagnostics = radarDiagnosticText(value).toLowerCase();
  const isAiSearch = /openai-public-web-search|openai[_ -]?web[_ -]?search|openai.{0,20}(网页|web).{0,20}(搜索|search)|all recruitment web-search pools failed|ai (网页搜索|补漏)/i.test(diagnostics);
  const isQuotaFailure = /insufficient_quota|quota.{0,30}(exceed|limit)|exceed.{0,30}quota|billing[_ -]?(hard[_ -]?limit|not[_ -]?active)|credit.{0,20}balance|account.{0,20}balance|额度不足|余额不足|账户余额/.test(diagnostics);
  if (!isAiSearch && !isQuotaFailure) return "";
  return isQuotaFailure
    ? "OpenAI 搜索暂不可用（API 额度不足）；补充额度后可恢复 AI 补漏。已核验官网源仍会继续扫描。"
    : "OpenAI 搜索暂不可用；已核验官网源仍会继续扫描。";
}

export function futureRadarSourceErrorCopy(source = {}) {
  return futureRadarAiSearchNotice(source)
    || "最近一次信源检查未完成；已核验岗位池仍保留，底层错误详情仅记录在服务端。";
}

export function futureRadarRunErrorCopy(error = {}, scanType = "quick") {
  const status = Number(error.status || 0);
  if (status === 429) {
    if (scanType === "deep") {
      return "深度发现信源暂时触发外部服务速率限制，请稍后再试；Quick Scan 不受影响。";
    }
    return "外部信源请求受限，请稍后再试；已有岗位池不会被清空。";
  }
  if (status === 409) {
    return `${scanType === "deep" ? "Deep Scan" : "Quick Scan"} 已在扫描中，不会创建重复任务；完成后即可再次启动。`;
  }
  if (status === 401) return "登录状态已失效，请重新登录后再扫描。";
  if (status === 428) return "隐私政策已更新，请先重新同意当前版本后再启动扫描。";
  const detail = String(error.message || "未知错误").trim();
  const aiNotice = futureRadarAiSearchNotice(error);
  if (aiNotice) return aiNotice;
  if (/请求超时|timed?\s*out|timeout/i.test(detail)) {
    return "扫描请求等待超时；服务端可能仍在继续，请稍后查看“扫描记录”。当前岗位池不会被清空。";
  }
  if (status === 403) return "当前账号暂时不能启动扫描；已核验岗位池仍可正常查看。";
  if ([502, 503, 504].includes(status)) return "扫描服务暂时不可用，请稍后重试；当前岗位池不会被清空。";
  return "扫描未能启动，请稍后重试；当前岗位池不会被清空。";
}

export function futureRadarRunSuccessCopy(run = {}, totalJobs = 0) {
  const status = String(run.status || "success").toLowerCase();
  const checked = Number(run.sources_checked || 0);
  const succeeded = Number(run.sources_succeeded || 0);
  const failed = Number(run.sources_failed || 0);
  const skipped = Number(run.sources_skipped || 0);
  const added = Number(run.new_jobs || 0);
  const updated = Number(run.updated_jobs || 0);
  const closed = Number(run.closed_jobs || 0);
  const pool = Math.max(0, Number(totalJobs) || 0);
  const aiNotice = futureRadarAiSearchNotice(run);
  if (!checked) return `扫描完成：当前没有到期信源，未重复抓取；实时岗位池仍为 ${pool} 条。${aiNotice ? ` ${aiNotice}` : ""}`;
  if (status === "failed") return `扫描完成但 ${failed || checked} 个信源均未成功；岗位池保留已有 ${pool} 条。${aiNotice ? ` ${aiNotice}` : ""}`;
  const sourceCopy = failed || skipped
    ? `${succeeded}/${checked} 个信源完成${skipped ? `，${skipped} 个运行中信源已跳过` : ""}`
    : `${checked} 个信源已核对`;
  return `扫描完成：${sourceCopy}，新增 ${added}、更新 ${updated}、关闭 ${closed}；实时岗位池 ${pool} 条。${aiNotice ? ` ${aiNotice}` : ""}`;
}

export const RADAR_ENRICHMENT_KEYS = Object.freeze([
  "match_score", "job_score", "tier_code", "score_breakdown",
  "employer_score", "role_score", "career_value_score", "job_condition_score",
  "scoring_factors", "scoring_status", "scoring_version", "organization_assessment",
  "positive_reasons", "negative_reasons", "fit_tags", "employer_categories",
  "organization_category", "industry_tags", "role_tags", "primary_category", "days_left",
]);

const SCORING_ENRICHMENT_KEYS = new Set([
  "match_score", "job_score", "tier_code", "score_breakdown",
  "employer_score", "role_score", "career_value_score", "job_condition_score",
  "scoring_factors", "scoring_status", "scoring_version", "organization_assessment",
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
  const seen = new Set();
  const mergedJobs = radarJobs.map((job) => {
    const legacy = jobIdentityKeys(job).map((key) => legacyIndex.get(key)).find(Boolean);
    jobIdentityKeys(job).forEach((key) => seen.add(key));
    if (!legacy) return job;
    const merged = { ...job };
    const explicitlyUnranked = (Object.hasOwn(job, "tier_code") && job.tier_code == null)
      || job.listing_kind === "recruitment_program"
      || job.scoring_status === "unscored_program_listing";
    RADAR_ENRICHMENT_KEYS.forEach((key) => {
      // An explicit null from Radar means “not scored / not classified yet” and
      // must not be overwritten by a stale legacy estimate.
      if (explicitlyUnranked && SCORING_ENRICHMENT_KEYS.has(key)) return;
      if (merged[key] === undefined && legacy[key] !== undefined) merged[key] = legacy[key];
    });
    return merged;
  });
  legacyJobs.forEach((job) => {
    const keys = jobIdentityKeys(job);
    if (keys.some((key) => seen.has(key))) return;
    keys.forEach((key) => seen.add(key));
    mergedJobs.push(job);
  });
  return mergedJobs;
}
