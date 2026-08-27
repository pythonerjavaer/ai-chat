from __future__ import annotations

import re
from datetime import date
from typing import Any


# Machine-readable organization categories. Classification deliberately uses
# metadata fields only; a company name or a role title is not a sector label.
CATEGORY_ORDER: tuple[str, ...] = (
    "state_energy_resources",
    "state_tech_telecom",
    "tobacco_monopoly",
    "policy_state_banks",
    "securities_public_funds_asset_management",
    "insurance_integrated_finance",
    "internet_tech",
    "consumer_foreign_consulting",
    "quant_private_hedge",
    "big_four_professional_services",
)

CATEGORY_ALIASES: dict[str, set[str]] = {
    "state_energy_resources": {
        "state_energy_resources", "央国企", "央企能源", "央企资源", "央企能源/资源",
        "央企能源与资源", "能源央企", "资源央企", "国有能源", "国有资源",
    },
    "state_tech_telecom": {
        "state_tech_telecom", "央国企科技", "央企科技", "央企通信", "央企交通",
        "央企科技/通信", "央企科技、通信与交通", "国有通信", "通信运营商",
    },
    "tobacco_monopoly": {
        "tobacco_monopoly", "烟草/专卖", "烟草", "中烟", "专卖体系", "烟草专卖体系",
    },
    "policy_state_banks": {
        "policy_state_banks", "银行/金融", "政策性金融", "政策行", "政策性银行",
        "国有大行", "政策行/国有大行", "国有银行",
    },
    "securities_public_funds_asset_management": {
        "securities_public_funds_asset_management", "券商/基金", "券商/公募/资管",
        "券商", "证券", "公募", "公募基金", "基金", "基金管理", "基金管理公司", "资管",
        "资产管理", "securities", "brokerage", "public_fund", "public fund",
        "mutual_fund", "mutual fund", "fund_management", "asset_management",
        "asset management",
    },
    "insurance_integrated_finance": {
        "insurance_integrated_finance", "保险/综合金融", "保险", "综合金融", "再保险",
        "insurance", "reinsurance", "integrated_finance",
    },
    "internet_tech": {
        "internet_tech", "互联网企业", "互联网大厂/中厂", "互联网", "科技企业",
        "民营科技企业", "人工智能", "云计算", "saas", "internet", "technology_company",
    },
    "consumer_foreign_consulting": {
        "consumer_foreign_consulting", "快消/消费", "快消", "消费", "消费品",
        "外企/咨询", "快消/外企/咨询", "外企", "咨询", "consumer", "fmcg",
        "foreign_enterprise", "consulting",
    },
    "quant_private_hedge": {
        "quant_private_hedge", "量化私募", "量化/私募/对冲", "量化", "私募",
        "私募基金", "私募证券", "对冲基金", "quant", "private_fund", "private fund",
        "hedge_fund", "hedge fund", "systematic_fund",
    },
    "big_four_professional_services": {
        "big_four_professional_services", "四大/专业服务", "四大", "专业服务",
        "会计师事务所", "big_four", "big four", "professional_services",
        "professional services", "accounting_firm", "accounting firm",
    },
}

# Profiles still save these older Chinese labels. Values are canonical codes.
EMPLOYER_TYPE_ALIASES: dict[str, set[str]] = {
    "央国企": {"state_energy_resources"},
    "央国企科技": {"state_tech_telecom"},
    "烟草/专卖": {"tobacco_monopoly"},
    "银行/金融": {"policy_state_banks"},
    "券商/基金": {"securities_public_funds_asset_management"},
    "券商/公募/资管": {"securities_public_funds_asset_management"},
    "保险/综合金融": {"insurance_integrated_finance"},
    "互联网企业": {"internet_tech"},
    "快消/消费": {"consumer_foreign_consulting"},
    "外企/咨询": {"consumer_foreign_consulting"},
    "快消/外企/咨询": {"consumer_foreign_consulting"},
    "量化私募": {"quant_private_hedge"},
    "量化/私募/对冲": {"quant_private_hedge"},
    "四大/专业服务": {"big_four_professional_services"},
}

# Exact product-owner boundaries. Only sufficiently specific jobs get a tier.
TIER_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"code": "T0", "min_score": 90, "label": "终极目标", "description": "平台、岗位与复合背景高度协同，必须申请"},
    {"code": "T0.5", "min_score": 85, "label": "准终极目标", "description": "非常强的重点岗位，仅有一项轻微短板"},
    {"code": "T1", "min_score": 80, "label": "核心主申", "description": "高质量平台与长期主线高度相关"},
    {"code": "T1.5", "min_score": 75, "label": "高质量重点", "description": "平台或岗位至少一项很强，值得重点准备"},
    {"code": "T2", "min_score": 70, "label": "值得申请", "description": "具备明确职业价值，但存在一项明显短板"},
    {"code": "T2.5", "min_score": 65, "label": "稳健补充", "description": "有一定价值，适合作为补充申请"},
    {"code": "T3", "min_score": 60, "label": "低优先级", "description": "仅作为保底，不挤占高优先级准备时间"},
)

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

SCORING_WEIGHTS = {
    "employer_platform": 35,
    "role_function": 45,
    "career_value": 10,
    "job_conditions": 10,
}
SCORING_VERSION = "future-radar-job-ranking-v2"

CORE_CITIES = {
    "北京", "上海", "深圳", "广州", "杭州", "南京", "苏州", "成都", "武汉", "西安",
    "天津", "重庆", "长沙", "合肥", "厦门", "南昌", "香港", "全国", "总部", "远程",
}

# Employer markers calibrate platform quality only; they never assign a tier
# and are never read by semantic category filtering.
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
    "国家烟草专卖局", "中国烟草总公司", "中烟工业", "上海烟草集团",
    "deloitte", "德勤", "pwc", "普华永道", "ey", "安永", "kpmg", "毕马威",
)

FINANCE_TERMS = (
    "金融", "finance", "会计", "accounting", "投资", "investment", "资本", "市场", "交易",
    "trading", "风险", "risk", "信贷", "credit", "估值", "审计", "audit", "合规",
    "compliance", "财务", "fp&a", "证券", "基金", "银行", "tax", "税务",
    "asset_management", "investment_research", "financial_markets",
)
TECH_TERMS = (
    "ai", "人工智能", "数据", "data", "python", "sql", "机器学习", "machine learning", "模型",
    "算法", "计算机", "computer science", "科技", "数字化", "fintech", "金融科技", "云计算",
    "saas", "data_science", "data_engineering", "machine_learning", "risk_technology",
)
PRODUCT_MANAGEMENT_TERMS = (
    "产品", "product", "战略", "strategy", "管理", "management", "管培", "治理", "governance",
    "商业分析", "business analyst", "business analytics", "项目管理", "转型", "研究", "research",
    "consulting", "advisory", "digital_transformation", "transaction_advisory",
)
CORE_ROLE_TERMS = (
    "金融科技", "fintech", "ai产品", "数据产品", "风险科技", "risk technology", "credit risk",
    "model risk", "模型治理", "ai governance", "investment analytics", "quant analytics",
    "战略", "strategy", "数字化转型", "digital transformation", "管培", "management trainee",
    "商业分析", "business analyst", "business analytics", "数据分析", "data analyst", "analytics",
    "数据岗", "人工智能应用", "产品策略", "量化", "fp&a", "regtech", "governance",
    "产品经理", "investment research", "quant research", "technology consulting", "deals",
    "transaction advisory", "financial services consulting", "management consulting",
)
PORTABLE_SKILL_TERMS = (
    "数据", "data", "ai", "人工智能", "产品", "product", "风险", "risk", "战略", "strategy",
    "投资", "investment", "商业分析", "business analyst", "sql", "python", "治理", "governance",
)
LOW_VALUE_TERMS = (
    "柜员", "客户经理", "销售", "纯销售", "普通运营", "客服", "事务", "行政支持", "录入",
    "测试", "运维", "devops", "实施", "售后", "地市支行", "基层支行", "customer service",
    "customer support", "shared service", "shared services", "routine operations", "routine support",
    "sales support", "administrative support", "back office support",
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

ROLE_TAG_MARKERS: dict[str, tuple[str, ...]] = {
    "ai_governance": ("ai governance", "人工智能治理", "算法治理"),
    "risk_technology": ("risk technology", "risk tech", "风险科技"),
    "model_risk": ("model risk", "模型风险", "模型治理"),
    "credit_risk": ("credit risk", "信用风险", "信贷风险"),
    "quant_research": ("quant research", "quantitative research", "systematic research", "量化研究", "系统化研究"),
    "data_science": ("data science", "data scientist", "数据科学"),
    "data_engineering": ("data engineering", "data engineer", "数据工程", "大数据工程"),
    "investment_research": ("investment research", "investment analyst", "投研", "投资研究", "基金研究"),
    "asset_management": ("asset management", "portfolio management", "资产管理", "组合管理"),
    "digital_transformation": ("digital transformation", "数字化转型", "数字转型"),
    "technology_strategy": ("technology strategy", "科技战略", "技术战略"),
    "technology_consulting": ("technology consulting", "tech consulting", "技术咨询", "科技咨询"),
    "financial_services_consulting": ("financial services consulting", "金融服务咨询"),
    "management_consulting": ("management consulting", "管理咨询", "战略咨询"),
    "transaction_advisory": ("transaction advisory", "transaction services", "deal advisory", "deals", "交易咨询", "并购咨询"),
    "cyber_data_risk": ("cyber risk", "data risk", "网络安全风险", "数据风险"),
    "financial_markets": ("financial markets", "global markets", "sales and trading", "金融市场", "全球市场"),
    "fintech": ("fintech", "financial technology", "金融科技"),
    "machine_learning": ("machine learning", "机器学习", "ml research"),
    "ai": ("artificial intelligence", "人工智能", "ai research", "ai product", "ai"),
    "data_analysis": ("data analytics", "data analyst", "business analytics", "数据分析", "商业分析"),
    "data": ("data consulting", "data product", "数据产品", "数据岗", "数据"),
    "quant": ("quantitative", "quant analyst", "quant developer", "quant", "量化"),
    "investment": ("investment", "portfolio", "投资", "投行"),
    "risk": ("risk", "风险", "风控"),
    "product": ("product management", "product manager", "产品管理", "产品经理", "产品"),
    "strategy": ("strategy", "strategic", "战略", "策略"),
    "technology": ("technology management", "technology", "科技管理", "技术管理", "科技"),
    "consulting": ("consulting", "consultant", "咨询"),
    "advisory": ("advisory", "顾问"),
    "audit": ("audit", "审计"),
    "tax": ("tax", "税务", "税收"),
    "compliance": ("compliance", "合规"),
    "operations": ("operations", "operation", "运营", "营运", "shared service"),
    "sales": ("sales", "销售", "渠道"),
    "customer_service": ("customer service", "客服", "客户服务"),
    "support": ("support", "支持岗", "行政支持", "后台支持"),
    "management_trainee": ("management trainee", "graduate program", "管培生", "管理培训生", "管培"),
    "research": ("research", "研究"),
}

HIGH_VALUE_ROLE_TAGS = {
    "ai_governance", "risk_technology", "model_risk", "credit_risk", "quant_research",
    "data_science", "data_engineering", "investment_research", "asset_management",
    "digital_transformation", "technology_strategy", "technology_consulting",
    "financial_services_consulting", "management_consulting", "transaction_advisory",
    "cyber_data_risk", "financial_markets", "fintech", "machine_learning", "ai",
    "data_analysis", "quant", "investment", "risk", "product", "strategy",
}
EXCEPTIONAL_ROLE_TAGS = {
    "quant_research", "data_science", "investment_research", "transaction_advisory",
    "technology_strategy", "ai_governance", "model_risk",
}
LOW_VALUE_ROLE_TAGS = {"operations", "sales", "customer_service", "support"}
PROFESSIONAL_ROLE_TAGS = {"audit", "tax", "compliance", "advisory", "consulting"}

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

INDUSTRY_TAG_MARKERS: dict[str, tuple[str, ...]] = {
    "energy_resources": ("能源", "资源", "电力", "石油", "矿业", "energy"),
    "telecom_technology": ("通信", "电信", "云计算", "科技", "telecom", "technology", "saas"),
    "tobacco": ("烟草", "专卖", "tobacco"),
    "banking": ("银行", "政策性金融", "banking"),
    "securities": ("券商", "证券", "期货", "securities", "brokerage"),
    "public_fund": ("公募", "公募基金", "public_fund", "mutual fund"),
    "asset_management": ("资管", "资产管理", "asset_management", "asset management"),
    "insurance": ("保险", "再保险", "insurance"),
    "internet_technology": ("互联网", "人工智能", "云计算", "internet", "technology", "saas"),
    "consumer": ("快消", "消费", "零售", "consumer", "fmcg"),
    "consulting": ("咨询", "consulting"),
    "quant": ("量化", "quant", "systematic"),
    "private_fund": ("私募", "private_fund", "private fund"),
    "hedge_fund": ("对冲基金", "hedge_fund", "hedge fund"),
    "professional_services": ("专业服务", "四大", "professional_services", "professional services"),
}

PRIVATE_HEDGE_PRIMARY_MARKERS = (
    "quant_private_hedge", "私募", "私募基金", "私募证券", "private_fund", "private fund",
    "对冲基金", "hedge_fund", "hedge fund", "systematic_fund",
)
PUBLIC_FUND_PRIMARY_MARKERS = (
    "公募", "公募基金", "public_fund", "public fund", "mutual_fund", "mutual fund",
)
QUANT_PRIMARY_MARKERS = ("量化", "quant", "systematic")

GENERIC_TITLE_PATTERNS = (
    re.compile(r"^(?:校园招聘|秋季招聘|秋招|校招|招聘)(?:正式)?(?:启动|公告|简章|计划|信息)?$", re.IGNORECASE),
    re.compile(
        r"^(?:专业人才岗?|综合培养生|专项人才|金融科技人才|科技人才|人才岗|"
        r"任意岗位|招聘岗位|岗位信息|全部岗位|各类岗位|职位|职位信息)$",
        re.IGNORECASE,
    ),
)
JD_EVIDENCE_MARKERS = (
    "负责", "职责", "工作内容", "参与", "研究", "开发", "分析", "构建", "管理", "支持",
    "审计", "税务", "咨询", "responsib", "develop", "research", "analy", "build", "design",
    "deliver", "advise", "audit", "tax", "operate", "support",
)


def tier_for_score(score: int) -> str:
    safe_score = max(0, min(100, int(score)))
    for definition in TIER_DEFINITIONS:
        if safe_score >= definition["min_score"]:
            return definition["code"]
    return "不建议投"


def _as_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


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


def _joined_fields(job: dict[str, Any], fields: tuple[str, ...]) -> str:
    return " ".join(
        item
        for field in fields
        for item in _as_values(job.get(field))
    ).casefold()


def _job_text(job: dict[str, Any]) -> str:
    return _joined_fields(
        job,
        (
            "title", "company", "city", "employer_type", "industry", "requirements",
            "description", "responsibilities", "organization_category", "primary_category",
            "industry_tags", "role_tags", "tags",
        ),
    )


def _role_source_text(job: dict[str, Any]) -> str:
    return _joined_fields(job, ("title", "description", "responsibilities", "requirements"))


def _marker_matches(text: str, marker: str) -> bool:
    marker = marker.casefold().strip()
    if not marker:
        return False
    if re.fullmatch(r"[a-z0-9_+.# -]+", marker):
        escaped = re.escape(marker).replace(r"\ ", r"\s+")
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None
    return marker in text


def _contains_any(text: str, markers: tuple[str, ...] | set[str]) -> bool:
    return any(_marker_matches(text, marker) for marker in markers)


def _category_codes_for_value(value: Any) -> set[str]:
    codes: set[str] = set()
    for raw in _as_values(value):
        text = raw.casefold().strip()
        if text in CATEGORY_ORDER:
            codes.add(text)
            continue
        private_context = _contains_any(
            text, {"私募", "私募基金", "对冲基金", "private fund", "private_fund", "hedge fund", "hedge_fund"}
        )
        for code, aliases in CATEGORY_ALIASES.items():
            for alias in aliases:
                alias_text = alias.casefold()
                if alias_text == "基金" and private_context:
                    continue
                if text == alias_text or _marker_matches(text, alias_text):
                    codes.add(code)
                    break
    return codes


def _selected_employer_categories(profile: dict[str, Any]) -> set[str]:
    selected: set[str] = set()
    for raw in _as_values(profile.get("employer_types")):
        text = raw.casefold()
        if text in CATEGORY_ORDER:
            selected.add(text)
            continue
        legacy = next(
            (codes for label, codes in EMPLOYER_TYPE_ALIASES.items() if label.casefold() == text),
            None,
        )
        selected.update(legacy or _category_codes_for_value(raw))
    return selected


def semantic_employer_categories(job: dict[str, Any]) -> set[str]:
    """Return canonical categories from structured metadata, never job prose."""
    categories: set[str] = set()
    for field in (
        "employer_type", "industry", "organization_category", "primary_category",
        "industry_tags", "tags",
    ):
        categories.update(_category_codes_for_value(job.get(field)))
    return categories


def _primary_category(job: dict[str, Any], categories: set[str]) -> str | None:
    for field in ("primary_category", "organization_category"):
        explicit = _category_codes_for_value(job.get(field))
        for code in CATEGORY_ORDER:
            if code in explicit:
                return code
    # `asset_management` is intentionally shared by public and private funds.
    # When a producer has not supplied a primary category, preserve the more
    # specific organization cue instead of letting CATEGORY_ORDER route every
    # cross-tagged private/quant fund into the broader public-asset bucket.
    metadata = _joined_fields(job, ("employer_type", "industry", "industry_tags", "tags"))
    if "quant_private_hedge" in categories:
        if _contains_any(metadata, set(PRIVATE_HEDGE_PRIMARY_MARKERS)):
            return "quant_private_hedge"
        if (
            "securities_public_funds_asset_management" in categories
            and _contains_any(metadata, set(PUBLIC_FUND_PRIMARY_MARKERS))
        ):
            return "securities_public_funds_asset_management"
        if _contains_any(metadata, set(QUANT_PRIMARY_MARKERS)):
            return "quant_private_hedge"
    return next((code for code in CATEGORY_ORDER if code in categories), None)


def primary_employer_category(job: dict[str, Any]) -> str | None:
    """Choose one UI starfield from structured employer metadata."""
    return _primary_category(job, semantic_employer_categories(job))


def _normalized_organization_category(job: dict[str, Any]) -> str | None:
    """Preserve a finer organization code instead of replacing it with UI grouping."""
    values = _as_values(job.get("organization_category"))
    if not values:
        return None
    raw = values[0].casefold().strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9_]{0,79}", raw):
        return raw
    explicit = _category_codes_for_value(raw)
    return next((code for code in CATEGORY_ORDER if code in explicit), None)


def _normalized_industry_tags(job: dict[str, Any]) -> list[str]:
    text = _joined_fields(job, ("industry", "industry_tags", "tags"))
    result = [
        code for code, markers in INDUSTRY_TAG_MARKERS.items()
        if _contains_any(text, set(markers))
    ]
    return list(dict.fromkeys(result))


def _normalize_role_tags(job: dict[str, Any]) -> list[str]:
    source_text = _role_source_text(job)
    explicit_values = [value.casefold() for value in _as_values(job.get("role_tags"))]
    explicit_text = " ".join(explicit_values)
    combined = f"{source_text} {explicit_text}".strip()
    return [
        code for code, markers in ROLE_TAG_MARKERS.items()
        if code in explicit_values or _contains_any(combined, set(markers))
    ]


def _role_text(job: dict[str, Any], role_tags: list[str] | None = None) -> str:
    normalized = role_tags if role_tags is not None else _normalize_role_tags(job)
    return f"{_role_source_text(job)} {' '.join(normalized)}".strip()


def _is_generic_role_title(title: str, company: str = "") -> bool:
    normalized = re.sub(r"[\s·|_—–-]+", "", title).casefold()
    normalized = re.sub(r"(?:20\d{2})(?:届|年)?", "", normalized)
    normalized_company = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", company).casefold()
    if normalized_company and normalized.startswith(normalized_company):
        normalized = normalized[len(normalized_company):]
    return not normalized or any(pattern.search(normalized) for pattern in GENERIC_TITLE_PATTERNS)


def _has_sufficient_role_evidence(job: dict[str, Any], role_tags: list[str]) -> bool:
    title = str(job.get("title", "")).strip()
    if not _is_generic_role_title(title, str(job.get("company", ""))):
        return True
    if _as_values(job.get("role_tags")):
        return bool(role_tags)
    jd_text = _joined_fields(job, ("description", "responsibilities", "requirements"))
    compact = re.sub(r"\s+", "", jd_text)
    jd_job = {
        "title": "",
        "description": job.get("description", ""),
        "responsibilities": job.get("responsibilities", ""),
        "requirements": job.get("requirements", ""),
        "role_tags": [],
    }
    jd_tags = _normalize_role_tags(jd_job)
    has_jd_action = _contains_any(jd_text, set(JD_EVIDENCE_MARKERS))
    if jd_tags and (has_jd_action or len(compact) >= 20):
        return True
    return len(compact) >= 60 or (len(compact) >= 20 and has_jd_action)


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


def _score_dimensions(
    job: dict[str, Any], profile: dict[str, Any], role_tags: list[str]
) -> tuple[dict[str, int], list[str], list[str], list[str]]:
    text = _job_text(job)
    role_text = _role_text(job, role_tags)
    company = str(job.get("company", "")).casefold()
    city_text = f"{job.get('city', '')} {job.get('title', '')}".casefold()
    tags = {str(tag).strip() for tag in (job.get("tags") or [])}
    role_tag_set = set(role_tags)
    positives: list[str] = []
    negatives: list[str] = []
    fit_tags: list[str] = []

    if _contains_any(company, set(ELITE_PLATFORM_MARKERS)):
        platform = 14
        positives.append("平台层级与行业资源强")
    elif _contains_any(company, set(STRONG_PLATFORM_MARKERS)):
        platform = 13
        positives.append("属于重点平台或核心行业机构")
    elif semantic_employer_categories(job):
        platform = 11
    else:
        platform = 8
    if _contains_any(text, set(HEADQUARTER_TERMS)):
        platform = min(16, platform + 2)
        positives.append("总部、总行或核心平台层级")
        fit_tags.append("总部/核心平台")
    if _contains_any(text, set(BRANCH_TERMS)):
        platform = max(4, platform - 3)
        negatives.append("地区分支或基层层级限制平台价值")

    high_value_role = bool(role_tag_set.intersection(HIGH_VALUE_ROLE_TAGS))
    low_value_role = bool(role_tag_set.intersection(LOW_VALUE_ROLE_TAGS)) or _contains_any(
        role_text, set(LOW_VALUE_TERMS)
    )
    job_quality = 8
    if high_value_role or _contains_any(role_text, set(CORE_ROLE_TERMS)):
        job_quality += 4
        positives.append("岗位靠近产品、风险、战略、投资或数字化核心")
    if role_tag_set.intersection(EXCEPTIONAL_ROLE_TAGS):
        job_quality += 2
        positives.append("岗位职能具备较高专业壁垒与长期价值")
    elif role_tag_set.intersection(PROFESSIONAL_ROLE_TAGS):
        job_quality += 2
    if "management_trainee" in role_tag_set or _contains_any(
        role_text, {"轮岗", "导师", "培养", "定岗", "graduate program", "管培"}
    ):
        job_quality += 2
        positives.append("存在明确培养或轮岗路径")
        fit_tags.append("培养路径")
    if _contains_any(text, set(HEADQUARTER_TERMS)):
        job_quality += 1
    if low_value_role:
        job_quality -= 5
        negatives.append("岗位偏销售、基层、重复运营或支持工作")
        fit_tags.append("低优先级")
    job_quality = max(2, min(15, job_quality))

    background_groups = sum(
        1
        for terms in (FINANCE_TERMS, TECH_TERMS, PRODUCT_MANAGEMENT_TERMS)
        if _contains_any(role_text, set(terms))
    )
    background_utilization = {0: 3, 1: 6, 2: 11, 3: 14}[background_groups]
    if background_groups >= 2:
        positives.append("岗位同时利用至少两类复合背景")
        fit_tags.append("复合背景")
    else:
        negatives.append("复合背景利用率有限")

    career_fit = 5
    if high_value_role or _contains_any(role_text, set(CORE_ROLE_TERMS)):
        career_fit += 5
        fit_tags.append("长期主线")
    if role_tag_set.intersection({"product", "strategy", "risk", "ai_governance", "data_analysis", "investment"}):
        career_fit += 2
    if role_tag_set.intersection(PROFESSIONAL_ROLE_TAGS):
        career_fit += 1
    technical_hard = _contains_any(role_text, set(HARD_TECH_TERMS))
    quant_hard = _contains_any(role_text, set(HIGH_QUANT_BARRIER_TERMS))
    if technical_hard:
        career_fit -= 3
        negatives.append("技术偏硬，与‘技术作为职业杠杆’的偏好存在距离")
        fit_tags.append("技术偏硬")
    if quant_hard:
        career_fit -= 2
        negatives.append("量化门槛偏高，数学或底层技术要求较重")
        fit_tags.append("量化高门槛")
    if low_value_role:
        career_fit -= 3
    career_fit = max(1, min(12, career_fit))

    career_ceiling = 6 + (2 if platform >= 13 else 1 if platform >= 11 else 0)
    if high_value_role:
        career_ceiling += 2
    if _contains_any(text, set(HEADQUARTER_TERMS)):
        career_ceiling += 1
    if low_value_role:
        career_ceiling -= 3
        negatives.append("长期晋升与职业天花板偏低")
    career_ceiling = max(2, min(12, career_ceiling))

    mobility = 3
    mobility += min(5, sum(1 for term in PORTABLE_SKILL_TERMS if _marker_matches(role_text, term)))
    if low_value_role:
        mobility -= 2
    mobility = max(1, min(8, mobility))
    if mobility >= 7:
        positives.append("能力可迁移到金融科技、数据产品、风险或战略方向")

    probability = 5
    if _contains_any(role_text, {"校园招聘", "校招", "应届", "graduate", "new analyst", "2027届"}):
        probability += 1
    if _contains_any(role_text, {"专业不限", "finance", "computer science", "business analytics", "金融", "计算机"}):
        probability += 1
    if _contains_any(role_text, {"博士", "phd", "数学竞赛", "工作经验", "仅限"}) or quant_hard:
        probability -= 2
        negatives.append("资格或竞争门槛较高，需作为冲刺评估")
        fit_tags.append("冲刺")
    probability = max(1, min(7, probability))

    compensation = 3
    if platform >= 14 or role_tag_set.intersection({"quant", "quant_research", "financial_markets", "investment"}):
        compensation += 2
    elif platform >= 11:
        compensation += 1
    compensation = min(6, compensation)

    work_life_balance = 3
    if _contains_any(role_text, set(HIGH_INTENSITY_TERMS)) or "高强度" in tags:
        work_life_balance -= 1
        negatives.append("工作强度较高，长期可持续性需权衡")
    if semantic_employer_categories(job).intersection(
        {"policy_state_banks", "state_energy_resources", "state_tech_telecom", "insurance_integrated_finance"}
    ):
        work_life_balance += 1
    if _contains_any(role_text, {"极端加班", "超低延迟"}):
        work_life_balance -= 1
    work_life_balance = max(1, min(5, work_life_balance))

    city = 3 if any(marker.casefold() in city_text for marker in CORE_CITIES) else 2
    if _contains_any(city_text, {"新加坡", "澳大利亚", "悉尼", "墨尔本"}):
        city = 0
        negatives.append("不在当前中国大陆与香港监控范围")

    further_education = 2 if work_life_balance >= 4 else 1 if work_life_balance >= 2 else 0

    desired_roles = _words(profile.get("desired_roles"))
    industries = _words(profile.get("industries"))
    if desired_roles and any(value in role_text for value in desired_roles):
        career_fit = min(12, career_fit + 1)
    if industries and any(value in text for value in industries):
        career_ceiling = min(12, career_ceiling + 1)

    dimensions = {
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
    return dimensions, positives, negatives, fit_tags


def _normalized_group_scores(dimensions: dict[str, int]) -> dict[str, int]:
    return {
        "employer_platform": round(dimensions["platform"] / BREAKDOWN_LIMITS["platform"] * 100),
        "role_function": round(
            (dimensions["job_quality"] + dimensions["background_utilization"] + dimensions["career_fit"])
            / (BREAKDOWN_LIMITS["job_quality"] + BREAKDOWN_LIMITS["background_utilization"] + BREAKDOWN_LIMITS["career_fit"])
            * 100
        ),
        "career_value": round(
            (dimensions["career_ceiling"] + dimensions["mobility"])
            / (BREAKDOWN_LIMITS["career_ceiling"] + BREAKDOWN_LIMITS["mobility"])
            * 100
        ),
        "job_conditions": round(
            (
                dimensions["probability"] + dimensions["compensation"]
                + dimensions["work_life_balance"] + dimensions["city"]
                + dimensions["further_education"]
            )
            / (
                BREAKDOWN_LIMITS["probability"] + BREAKDOWN_LIMITS["compensation"]
                + BREAKDOWN_LIMITS["work_life_balance"] + BREAKDOWN_LIMITS["city"]
                + BREAKDOWN_LIMITS["further_education"]
            )
            * 100
        ),
    }


def _weighted_contributions(group_scores: dict[str, int]) -> tuple[int, dict[str, int]]:
    raw = {
        key: group_scores[key] * SCORING_WEIGHTS[key] / 100
        for key in SCORING_WEIGHTS
    }
    score = max(0, min(100, round(sum(raw.values()))))
    contributions = {key: int(value) for key, value in raw.items()}
    remainder = score - sum(contributions.values())
    for key in sorted(raw, key=lambda item: raw[item] - contributions[item], reverse=True)[:remainder]:
        contributions[key] += 1
    return score, contributions


def _days_left(job: dict[str, Any]) -> int | None:
    closing_date = job.get("closing_date")
    if not closing_date:
        return None
    try:
        return (date.fromisoformat(str(closing_date)) - date.today()).days
    except (TypeError, ValueError):
        return None


def _scoring_factors(
    group_scores: dict[str, int | None], contributions: dict[str, int | None]
) -> dict[str, dict[str, int | str | None]]:
    labels = {
        "employer_platform": "平台质量",
        "role_function": "岗位职能与匹配",
        "career_value": "职业发展与退出价值",
        "job_conditions": "薪酬、地点与工作条件",
    }
    return {
        key: {
            "label": labels[key],
            "weight": SCORING_WEIGHTS[key],
            "score": group_scores[key],
            "contribution": contributions[key],
        }
        for key in SCORING_WEIGHTS
    }


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    categories = semantic_employer_categories(job)
    primary_category = _primary_category(job, categories)
    organization_category = _normalized_organization_category(job)
    industry_tags = _normalized_industry_tags(job)
    role_tags = _normalize_role_tags(job)
    days_left = _days_left(job)

    if not _has_sufficient_role_evidence(job, role_tags):
        empty_groups: dict[str, int | None] = {key: None for key in SCORING_WEIGHTS}
        return {
            **job,
            "job_score": None,
            "match_score": None,
            "employer_score": None,
            "role_score": None,
            "career_value_score": None,
            "job_condition_score": None,
            "score_breakdown": dict(empty_groups),
            "scoring_status": "unscored_insufficient_role_data",
            "scoring_version": SCORING_VERSION,
            "scoring_factors": _scoring_factors(empty_groups, empty_groups),
            "positive_reasons": [],
            "negative_reasons": ["尚未取得足够具体的岗位职责或 JD，暂不生成 T 级"],
            "match_reasons": [],
            "fit_tags": [],
            "technical_hard": False,
            "quant_barrier": False,
            "manual_override": False,
            "tier_code": None,
            "days_left": days_left,
            "primary_category": primary_category,
            "organization_category": organization_category,
            "industry_tags": industry_tags,
            "role_tags": role_tags,
        }

    dimensions, positives, negatives, fit_tags = _score_dimensions(job, profile, role_tags)
    group_scores = _normalized_group_scores(dimensions)
    score, contributions = _weighted_contributions(group_scores)
    tier_code = tier_for_score(score)

    return {
        **job,
        "job_score": score,
        "match_score": score,
        "employer_score": group_scores["employer_platform"],
        "role_score": group_scores["role_function"],
        "career_value_score": group_scores["career_value"],
        "job_condition_score": group_scores["job_conditions"],
        "score_breakdown": contributions,
        "scoring_status": "scored",
        "scoring_version": SCORING_VERSION,
        "scoring_factors": _scoring_factors(group_scores, contributions),
        "positive_reasons": positives[:3] or ["岗位具备可核验的基础职业价值"],
        "negative_reasons": negatives[:2] or ["仍需结合完整 JD 与个人约束继续核对"],
        "match_reasons": positives[:3] or ["已按岗位级职业价值模型完成评分"],
        "fit_tags": list(dict.fromkeys(fit_tags)),
        "technical_hard": "技术偏硬" in fit_tags,
        "quant_barrier": "量化高门槛" in fit_tags,
        "manual_override": False,
        "tier_code": tier_code,
        "days_left": days_left,
        "primary_category": primary_category,
        "organization_category": organization_category,
        "industry_tags": industry_tags,
        "role_tags": role_tags,
    }
