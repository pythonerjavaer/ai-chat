from __future__ import annotations

from datetime import date
from typing import Any


# Stable profile values. Real feeds use many labels, so filtering relies on the
# semantic categories below instead of brittle employer-type string equality.
EMPLOYER_TYPE_ALIASES: dict[str, set[str]] = {
    "央国企": {"央国企", "央企能源", "央企资源"},
    "央国企科技": {"央国企科技", "央企科技", "央企通信", "央企交通"},
    "银行/金融": {"银行/金融", "政策性金融", "政策行", "国有大行"},
    "券商/基金": {"券商/基金", "券商", "基金", "资管"},
    "保险/综合金融": {"保险/综合金融", "保险", "综合金融"},
    "互联网企业": {"互联网企业", "互联网", "科技企业"},
    "快消/消费": {"快消/消费", "快消", "消费"},
    "外企/咨询": {"外企/咨询", "外企", "咨询"},
    "快消/外企/咨询": {"快消/消费", "快消", "消费", "外企/咨询", "外企", "咨询"},
    "量化私募": {"量化私募", "量化", "私募", "对冲基金"},
}

# Exact product-owner score boundaries. Scores below 60 are deliberately kept
# out of the priority pool instead of being mislabeled T3.
TIER_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"code": "T0", "min_score": 90, "label": "终极目标", "description": "平台、岗位与复合背景高度协同，必须申请"},
    {"code": "T0.5", "min_score": 85, "label": "准终极目标", "description": "非常强的重点岗位，仅有一项轻微短板"},
    {"code": "T1", "min_score": 80, "label": "核心主申", "description": "高质量平台与长期主线高度相关"},
    {"code": "T1.5", "min_score": 75, "label": "高质量重点", "description": "平台或岗位至少一项很强，值得重点准备"},
    {"code": "T2", "min_score": 70, "label": "值得申请", "description": "具备明确职业价值，但存在一项明显短板"},
    {"code": "T2.5", "min_score": 65, "label": "稳健补充", "description": "有一定价值，适合作为补充申请"},
    {"code": "T3", "min_score": 60, "label": "低优先级", "description": "仅作为保底，不挤占高优先级准备时间"},
)

TIER_TARGET_SCORES = {
    "T0": 92,
    "T0.5": 87,
    "T1": 82,
    "T1.5": 77,
    "T2": 72,
    "T2.5": 67,
    "T3": 62,
}

BREAKDOWN_LIMITS = {
    "platform": 16,
    "job_quality": 15,
    "background_utilization": 14,
    "career_fit": 12,
    "career_ceiling": 12,
    "mobility": 8,
    "probability": 7,
    "compensation": 6,
    "work_life_balance": 5,
    "city": 3,
    "further_education": 2,
}

CORE_CITIES = {
    "北京", "上海", "深圳", "广州", "杭州", "南京", "苏州", "成都", "武汉", "西安",
    "天津", "重庆", "长沙", "合肥", "厦门", "南昌", "香港", "全国", "总部", "远程",
}

ELITE_PLATFORM_MARKERS = (
    "中国人民银行", "国家开发银行", "中国进出口银行", "中国农业发展银行",
    "工商银行", "建设银行", "中国银行", "交通银行", "中金公司", "中信证券",
    "南方基金", "易方达", "华夏基金", "point72", "goldman sachs", "高盛",
    "j.p. morgan", "morgan stanley", "blackrock", "麦肯锡", "kearney", "科尔尼",
    "amazon web services", "aws", "microsoft", "google", "apple", "nvidia",
)

STRONG_PLATFORM_MARKERS = (
    "农业银行", "邮储银行", "国家电网", "中国石油", "中国石化", "中国海油",
    "中国移动", "中国电信", "中国联通", "中国商飞", "华为", "腾讯", "阿里",
    "字节", "byteplus", "蚂蚁", "美团", "京东", "百度", "拼多多", "大疆", "dji",
    "中芯国际", "smic", "hsbc", "汇丰", "ubs", "citi", "罗兰贝格", "roland berger",
    "中信期货", "中信建投", "华泰证券", "国泰海通", "嘉实基金", "富国基金",
)

STATE_ENERGY_MARKERS = (
    "中国石油", "中国石化", "中国海油", "国家能源", "国家电网", "华能", "华电",
    "大唐", "国家电投", "核工业", "中核", "中国能建", "中国电建", "煤炭", "矿业", "黄金",
)
STATE_TECH_MARKERS = (
    "中国移动", "中国电信", "中国联通", "中国铁塔", "中国电子", "中国电科", "航天",
    "航空工业", "中国商飞", "中国铁路", "铁道", "交通建设", "中国一汽", "信通院", "中国船舶", "兵器",
)
BANK_MARKERS = (
    "人民银行", "国家开发银行", "进出口银行", "农业发展银行", "工商银行", "农业银行",
    "中国银行", "建设银行", "交通银行", "邮储银行", "政策性银行", "国有大行",
)
SECURITIES_MARKERS = (
    "证券", "基金", "资管", "资产管理", "期货", "投行", "investment banking", "asset management",
)
INSURANCE_MARKERS = ("保险", "人保", "人寿", "太平", "平安", "再保险", "insur")
INTERNET_MARKERS = (
    "腾讯", "阿里", "字节", "byteplus", "百度", "拼多多", "美团", "京东", "小米", "网易",
    "快手", "滴滴", "携程", "华为", "大疆", "dji", "中芯", "smic", "互联网", "云计算", "saas",
)
CONSUMER_MARKERS = (
    "宝洁", "联合利华", "欧莱雅", "雀巢", "玛氏", "可口可乐", "百事", "耐克", "babycare",
    "快消", "消费品牌", "零售",
)
FOREIGN_CONSULTING_MARKERS = (
    "咨询", "consult", "kearney", "科尔尼", "麦肯锡", "罗兰贝格", "roland berger", "德勤",
    "普华永道", "毕马威", "安永", "accenture", "埃森哲", "amazon", "microsoft", "google",
    "apple", "nvidia", "goldman", "高盛", "morgan stanley", "ubs", "citi", "hsbc", "汇丰", "blackrock",
)
QUANT_MARKERS = (
    "量化", "quant", "hft", "alpha research", "对冲基金", "私募", "point72", "幻方", "明汯", "衍复", "灵均", "宽德",
)

FINANCE_TERMS = (
    "金融", "finance", "会计", "accounting", "投资", "investment", "资本", "市场", "交易", "trading",
    "风险", "risk", "信贷", "credit", "估值", "审计", "合规", "财务", "fp&a", "证券", "基金", "银行",
)
TECH_TERMS = (
    "ai", "人工智能", "数据", "data", "python", "sql", "机器学习", "machine learning", "模型",
    "算法", "计算机", "computer science", "科技", "数字化", "fintech", "金融科技", "云计算", "saas",
)
PRODUCT_MANAGEMENT_TERMS = (
    "产品", "product", "战略", "strategy", "管理", "management", "管培", "治理", "governance",
    "商业分析", "business analyst", "business analytics", "项目管理", "转型", "研究", "research",
)
CORE_ROLE_TERMS = (
    "金融科技", "fintech", "ai产品", "数据产品", "风险科技", "risk technology", "credit risk",
    "model risk", "模型治理", "ai governance", "investment analytics", "quant analytics",
    "战略", "strategy", "数字化转型", "digital transformation", "管培", "management trainee",
    "商业分析", "business analyst", "business analytics", "数据分析", "data analyst", "analytics",
    "数据岗", "人工智能应用", "产品策略", "量化", "fp&a", "regtech", "合规", "governance", "产品经理",
)
PORTABLE_SKILL_TERMS = (
    "数据", "data", "ai", "人工智能", "产品", "product", "风险", "risk", "战略", "strategy",
    "投资", "investment", "商业分析", "business analyst", "sql", "python", "治理", "governance",
)
LOW_VALUE_TERMS = (
    "柜员", "客户经理", "销售", "纯销售", "普通运营", "客服", "事务", "行政支持", "录入",
    "测试", "运维", "devops", "实施", "售后", "地市支行", "基层支行",
)
HARD_TECH_TERMS = (
    "c++", "java后端", "后端开发", "底层", "infra", "devops", "运维", "操作系统", "芯片",
    "算法工程师", "大模型训练", "深度学习训练", "测试工程师",
)
HIGH_QUANT_BARRIER_TERMS = (
    "hft", "超低延迟", "alpha research", "随机微积分", "数学竞赛", "quantitative strats", "量化研究",
)
HIGH_INTENSITY_TERMS = (
    "高强度", "investment banking", "投行", "sales and trading", "战略咨询", "consultant", "咨询", "hft",
)
HEADQUARTER_TERMS = ("总部", "总行", "集团总部", "研究院", "核心科技子公司", "head office")
BRANCH_TERMS = ("地市", "支行", "分理处", "县级", "基层", "普通分公司")

ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "产品": ("产品", "product"),
    "产品经理": ("产品", "product manager", "product management", "strategy manager", "战略", "strategy"),
    "数据": ("数据", "data", "analytics", "分析"),
    "数据分析": ("数据分析", "data analytics", "business analytics", "analyst"),
    "ai": ("ai", "人工智能", "artificial intelligence", "machine learning", "机器学习"),
    "人工智能": ("ai", "人工智能", "artificial intelligence", "machine learning", "机器学习"),
    "金融科技": ("金融科技", "fintech", "risk technology", "investment technology"),
    "风险": ("风险", "risk", "风控", "credit", "合规", "compliance"),
    "战略": ("战略", "strategy", "strategic"),
    "管培": ("管培", "management trainee", "graduate program"),
    "商业分析": ("商业分析", "business analyst", "business analytics"),
    "投资": ("投资", "investment", "投研", "资产管理"),
}

INDUSTRY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "互联网": ("互联网", "科技企业", "云计算", "saas", "人工智能"),
    "金融": ("金融", "银行", "证券", "基金", "资管", "保险", "投资"),
    "金融科技": ("金融科技", "fintech", "风险科技", "数据金融"),
    "快消": ("快消", "消费品牌", "消费品", "零售"),
    "咨询": ("咨询", "consult"),
    "新能源": ("新能源", "能源", "电力", "储能"),
}


def tier_for_score(score: int) -> str:
    safe_score = max(0, min(100, int(score)))
    for definition in TIER_DEFINITIONS:
        if safe_score >= definition["min_score"]:
            return definition["code"]
    return "不建议投"


def _words(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _expanded_terms(values: Any, synonyms: dict[str, tuple[str, ...]]) -> set[str]:
    expanded: set[str] = set()
    for value in _words(values):
        expanded.add(value)
        expanded.update(term.casefold() for term in synonyms.get(value, ()))
    return expanded


def _job_text(job: dict[str, Any]) -> str:
    tags = " ".join(str(value) for value in (job.get("tags") or []))
    return (
        " ".join(
            str(job.get(key, ""))
            for key in ("title", "company", "city", "employer_type", "industry", "requirements")
        ).casefold()
        + " "
        + tags.casefold()
    )


def _role_text(job: dict[str, Any]) -> str:
    tags = " ".join(str(value) for value in (job.get("tags") or []))
    return (
        " ".join(
            str(job.get(key, ""))
            for key in ("title", "industry", "requirements")
        ).casefold()
        + " "
        + tags.casefold()
    )


def _contains_any(text: str, markers: tuple[str, ...] | set[str]) -> bool:
    return any(marker.casefold() in text for marker in markers)


def _selected_employer_categories(profile: dict[str, Any]) -> set[str]:
    selected: set[str] = set()
    for value in _words(profile.get("employer_types")):
        canonical = next((key for key in EMPLOYER_TYPE_ALIASES if key.casefold() == value), value)
        selected.update(item.casefold() for item in EMPLOYER_TYPE_ALIASES.get(canonical, {canonical}))
    return selected


def semantic_employer_categories(job: dict[str, Any]) -> set[str]:
    text = _job_text(job)
    categories: set[str] = set()
    if _contains_any(text, STATE_ENERGY_MARKERS):
        categories.update({"央国企", "央企能源", "央企资源"})
    if _contains_any(text, STATE_TECH_MARKERS):
        categories.update({"央国企科技", "央企科技", "央企通信", "央企交通"})
    if _contains_any(text, BANK_MARKERS):
        categories.update({"银行/金融", "政策性金融", "政策行", "国有大行"})
    if _contains_any(text, SECURITIES_MARKERS):
        categories.update({"券商/基金", "券商", "基金", "资管"})
    if _contains_any(text, INSURANCE_MARKERS):
        categories.update({"保险/综合金融", "保险", "综合金融"})
    if _contains_any(text, INTERNET_MARKERS):
        categories.update({"互联网企业", "互联网", "科技企业"})
    if _contains_any(text, CONSUMER_MARKERS):
        categories.update({"快消/消费", "快消", "消费", "快消/外企/咨询"})
    if _contains_any(text, FOREIGN_CONSULTING_MARKERS):
        categories.update({"外企/咨询", "外企", "咨询", "快消/外企/咨询"})
    if _contains_any(text, QUANT_MARKERS):
        categories.update({"量化私募", "量化", "私募", "对冲基金"})
    employer_type = str(job.get("employer_type", "")).strip().casefold()
    if employer_type:
        categories.add(employer_type)
    return {value.casefold() for value in categories}


def job_matches_profile(job: dict[str, Any], profile: dict[str, Any]) -> bool:
    """Apply real filters: OR within one field, AND across populated fields."""
    text = _job_text(job)
    desired_roles = _expanded_terms(profile.get("desired_roles"), ROLE_SYNONYMS)
    industries = _expanded_terms(profile.get("industries"), INDUSTRY_SYNONYMS)
    locations = _words(profile.get("locations"))
    employer_categories = _selected_employer_categories(profile)

    if desired_roles and not any(value in text for value in desired_roles):
        return False
    if industries and not any(value in text for value in industries):
        return False
    if locations:
        city_text = f"{job.get('city', '')} {job.get('title', '')}".casefold()
        if not any(
            value in city_text or (value in {"可远程", "远程"} and "远程" in text)
            for value in locations
        ):
            return False
    if employer_categories and not employer_categories.intersection(semantic_employer_categories(job)):
        return False
    return True


def _manual_tier(tags: Any) -> str | None:
    if not isinstance(tags, list):
        return None
    for raw in tags:
        value = (
            str(raw).strip().upper().replace("T1.0", "T1").replace("T2.0", "T2").replace("T3.0", "T3")
        )
        if value in TIER_TARGET_SCORES:
            return value
    return None


def _rebalance_breakdown(breakdown: dict[str, int], target: int) -> dict[str, int]:
    """Make a curated tier anchor and its eleven displayed dimensions agree."""
    balanced = dict(breakdown)
    delta = target - sum(balanced.values())
    order = (
        "background_utilization", "career_fit", "job_quality", "platform",
        "career_ceiling", "mobility", "probability", "compensation",
        "work_life_balance", "city", "further_education",
    )
    if delta > 0:
        for key in order:
            room = BREAKDOWN_LIMITS[key] - balanced[key]
            step = min(room, delta)
            balanced[key] += step
            delta -= step
            if delta == 0:
                break
    elif delta < 0:
        for key in reversed(order):
            step = min(balanced[key], -delta)
            balanced[key] -= step
            delta += step
            if delta == 0:
                break
    return balanced


def _score_breakdown(
    job: dict[str, Any], profile: dict[str, Any]
) -> tuple[dict[str, int], list[str], list[str], list[str]]:
    text = _job_text(job)
    role_text = _role_text(job)
    company = str(job.get("company", "")).casefold()
    city_text = f"{job.get('city', '')} {job.get('title', '')}".casefold()
    tags = {str(tag).strip() for tag in (job.get("tags") or [])}
    positives: list[str] = []
    negatives: list[str] = []
    fit_tags: list[str] = []

    if _contains_any(company, ELITE_PLATFORM_MARKERS):
        platform = 14
        positives.append("平台层级与行业资源强")
    elif _contains_any(company, STRONG_PLATFORM_MARKERS):
        platform = 13
        positives.append("属于重点平台或核心行业机构")
    elif semantic_employer_categories(job):
        platform = 11
    else:
        platform = 8
    if _contains_any(text, HEADQUARTER_TERMS):
        platform = min(16, platform + 2)
        positives.append("总部、总行或核心平台层级")
        fit_tags.append("总部/核心平台")
    if _contains_any(text, BRANCH_TERMS):
        platform = max(4, platform - 3)
        negatives.append("地区分支或基层层级限制平台价值")

    job_quality = 8
    if _contains_any(role_text, CORE_ROLE_TERMS):
        job_quality += 4
        positives.append("岗位靠近产品、风险、战略、投资或数字化核心")
    if _contains_any(role_text, ("轮岗", "导师", "培养", "定岗", "graduate program", "管培")):
        job_quality += 2
        positives.append("存在明确培养或轮岗路径")
        fit_tags.append("培养路径")
    if _contains_any(text, HEADQUARTER_TERMS):
        job_quality += 1
    if _contains_any(role_text, LOW_VALUE_TERMS):
        job_quality -= 5
        negatives.append("岗位偏销售、基层、重复运营或支持工作")
        fit_tags.append("低优先级")
    job_quality = max(2, min(15, job_quality))

    background_groups = sum(
        1
        for terms in (FINANCE_TERMS, TECH_TERMS, PRODUCT_MANAGEMENT_TERMS)
        if _contains_any(role_text, terms)
    )
    background_utilization = {0: 3, 1: 6, 2: 11, 3: 14}[background_groups]
    if background_groups >= 2:
        positives.append("同时利用 Finance / Accounting、CS / AI 与业务管理中的至少两类背景")
        fit_tags.append("复合背景")
    else:
        negatives.append("复合背景利用率有限")

    career_fit = 5
    if _contains_any(role_text, CORE_ROLE_TERMS):
        career_fit += 5
        fit_tags.append("长期主线")
    if _contains_any(role_text, ("产品", "战略", "风险", "治理", "商业分析", "investment analytics", "数据分析")):
        career_fit += 2
    technical_hard = _contains_any(role_text, HARD_TECH_TERMS)
    quant_hard = _contains_any(role_text, HIGH_QUANT_BARRIER_TERMS)
    if technical_hard:
        career_fit -= 3
        negatives.append("技术偏硬，与‘技术作为职业杠杆’的偏好存在距离")
        fit_tags.append("技术偏硬")
    if quant_hard:
        career_fit -= 2
        negatives.append("量化门槛偏高，数学或底层技术要求较重")
        fit_tags.append("量化高门槛")
    if _contains_any(role_text, LOW_VALUE_TERMS):
        career_fit -= 3
    career_fit = max(1, min(12, career_fit))

    career_ceiling = 6 + (2 if platform >= 13 else 1 if platform >= 11 else 0)
    if _contains_any(role_text, CORE_ROLE_TERMS):
        career_ceiling += 2
    if _contains_any(text, HEADQUARTER_TERMS):
        career_ceiling += 1
    if _contains_any(role_text, LOW_VALUE_TERMS):
        career_ceiling -= 3
        negatives.append("长期晋升与职业天花板偏低")
    career_ceiling = max(2, min(12, career_ceiling))

    mobility = 3
    mobility += min(5, sum(1 for term in PORTABLE_SKILL_TERMS if term in role_text))
    if _contains_any(role_text, LOW_VALUE_TERMS):
        mobility -= 2
    mobility = max(1, min(8, mobility))
    if mobility >= 7:
        positives.append("能力可迁移到金融科技、数据产品、风险或战略方向")

    probability = 5
    if _contains_any(role_text, ("校园招聘", "校招", "应届", "graduate", "new analyst", "2027届")):
        probability += 1
    if _contains_any(role_text, ("专业不限", "finance", "computer science", "business analytics", "金融", "计算机")):
        probability += 1
    if _contains_any(role_text, ("博士", "phd", "数学竞赛", "工作经验", "仅限")) or quant_hard:
        probability -= 2
        negatives.append("资格或竞争门槛较高，需作为冲刺评估")
        fit_tags.append("冲刺")
    probability = max(1, min(7, probability))

    compensation = 3
    if platform >= 14 or _contains_any(role_text, ("投行", "量化", "对冲基金", "互联网大厂", "全球市场")):
        compensation += 2
    elif platform >= 11:
        compensation += 1
    compensation = min(6, compensation)

    work_life_balance = 3
    if _contains_any(role_text, HIGH_INTENSITY_TERMS) or "高强度" in tags:
        work_life_balance -= 1
        negatives.append("工作强度较高，长期可持续性需权衡")
    if _contains_any(text, ("政策性", "国有大行", "央企", "保险", "国家电网")):
        work_life_balance += 1
    if _contains_any(role_text, ("极端加班", "超低延迟")):
        work_life_balance -= 1
    work_life_balance = max(1, min(5, work_life_balance))

    city = 3 if any(marker.casefold() in city_text for marker in CORE_CITIES) else 2
    if _contains_any(city_text, ("新加坡", "澳大利亚", "悉尼", "墨尔本")):
        city = 0
        negatives.append("不在当前中国大陆与香港监控范围")

    further_education = 2 if work_life_balance >= 4 else 1 if work_life_balance >= 2 else 0

    # Saved coordinates are real filters first. Small nudges only break ties;
    # they never replace the eleven-dimensional personal model.
    desired_roles = _words(profile.get("desired_roles"))
    industries = _words(profile.get("industries"))
    if desired_roles and any(value in text for value in desired_roles):
        career_fit = min(12, career_fit + 1)
    if industries and any(value in text for value in industries):
        career_ceiling = min(12, career_ceiling + 1)

    breakdown = {
        "platform": platform,
        "job_quality": job_quality,
        "background_utilization": background_utilization,
        "career_fit": career_fit,
        "career_ceiling": career_ceiling,
        "mobility": mobility,
        "probability": probability,
        "compensation": compensation,
        "work_life_balance": work_life_balance,
        "city": city,
        "further_education": further_education,
    }
    return breakdown, positives, negatives, fit_tags


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    breakdown, positives, negatives, fit_tags = _score_breakdown(job, profile)
    raw_score = sum(breakdown.values())
    manual_tier = _manual_tier(job.get("tags"))
    manual_override = manual_tier is not None
    if manual_tier:
        score = TIER_TARGET_SCORES[manual_tier]
        breakdown = _rebalance_breakdown(breakdown, score)
        positives.insert(0, "该岗位已有按个人规则校准的层级锚点")
    else:
        score = raw_score
    score = max(0, min(100, int(score)))
    tier_code = tier_for_score(score)

    closing_date = job.get("closing_date")
    days_left = None
    if closing_date:
        try:
            days_left = (date.fromisoformat(closing_date) - date.today()).days
        except (TypeError, ValueError):
            pass

    return {
        **job,
        "match_score": score,
        "score_breakdown": breakdown,
        "positive_reasons": positives[:3] or ["岗位仍具备可核验的基础职业价值"],
        "negative_reasons": negatives[:2] or ["公开信息有限，需打开原始公告继续核对"],
        "match_reasons": positives[:3] or ["按个人长期职业模型完成基础评分"],
        "fit_tags": list(dict.fromkeys(fit_tags)),
        "technical_hard": "技术偏硬" in fit_tags,
        "quant_barrier": "量化高门槛" in fit_tags,
        "manual_override": manual_override,
        "tier_code": tier_code,
        "days_left": days_left,
    }
