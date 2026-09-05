from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from typing import Any

from .recruitment_organizations import assess_organization, collect_organization_evidence
from .recruitment_rating import resolve_source_ratings
from .recruitment_directory import (
    canonical_employer_identity,
    employer_category_override,
    monitored_employer_identities,
)


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
    # Platform is the durable base of a role's option value.  Composite
    # background is useful evidence of fit, but it must not make an ordinary
    # employer outrank a strong platform/core-role opening by itself.
    "platform": 22,
    "job_quality": 15,
    "background_utilization": 8,
    "career_fit": 12,
    "career_ceiling": 12,
    "mobility": 8,
    "probability": 7,
    "compensation": 6,
    "work_life_balance": 5,
    "city": 3,
    "further_education": 2,
}

# These four values are presentation groups for the original eleven-dimension
# model.  They are the exact sums of the dimension limits below, not a second
# weighting pass.  In v3 the groups were normalized and weighted 35/45/10/10,
# which silently changed platform influence from 16% to 35% and caused the
# product owner's calibrated T tiers to drift in both directions.
SCORING_WEIGHTS = {
    "employer_platform": 22,
    "role_function": 35,
    "career_value": 20,
    "job_conditions": 23,
}
SCORING_VERSION = "future-radar-job-ranking-v4.3-platform-first-fit-bounded"

TIER_TARGET_SCORES = {
    "T0": 92,
    "T0.5": 87,
    "T1": 82,
    "T1.5": 77,
    "T2": 72,
    "T2.5": 67,
    "T3": 62,
}

TIER_MAX_SCORES = {
    "T0": 100,
    "T0.5": 89,
    "T1": 84,
    "T1.5": 79,
    "T2": 74,
    "T2.5": 69,
    "T3": 64,
}

# The institution baseline and the concrete job are deliberately separate.
# A headquarters/core vacancy may rise one half-step above its platform
# baseline when the other ten dimensions justify it; it may not leap several
# tiers on an attractive title alone.  Regional hiring units use the stricter
# hierarchy ceiling below.
INSTITUTION_JOB_MAX_SCORES = {
    "T0": 100,
    "T0.5": 89,
    "T1": 89,
    "T1.5": 84,
    "T2": 79,
    "T2.5": 74,
    "T3": 69,
}

# Hiring-unit hierarchy is a job-level safety boundary even when an employer
# has not yet been added to the institution calibration directory.
ORGANIZATION_LEVEL_MAX_SCORES = {
    "provincial_branch": 79,
    "city_branch": 69,
    "branch_unspecified": 69,
    "local_branch": 64,
    "research_institute": 79,
    "subsidiary": 69,
    "third_party": 59,
}

# Institution baselines are deliberately separate from final job tiers.  They
# are platform/hiring-unit calibration anchors, never industry classifications.
# A weak role at a great institution can still finish at T3 or below.
INSTITUTION_T0_MARKERS = (
    "中国人民银行", "国家开发银行", "中国进出口银行", "中国农业发展银行",
    "中央国债登记结算", "中国外汇交易中心", "上海清算所", "中国结算",
)
INSTITUTION_T05_MARKERS = (
    "工商银行", "中国工商银行", "建设银行", "中国建设银行", "中国银行", "交通银行",
    "中金公司", "中国国际金融", "中信证券", "point72", "goldman sachs", "高盛",
    "j.p. morgan", "jpmorgan", "morgan stanley", "blackrock", "贝莱德",
    "腾讯", "字节跳动", "byteplus", "byteplus（字节跳动）", "蚂蚁集团",
)
INSTITUTION_T1_MARKERS = (
    "南方基金", "易方达", "华夏基金", "嘉实基金", "富国基金", "汇添富", "博时基金",
    "麦肯锡", "kearney", "科尔尼", "l.e.k", "lek consulting", "波士顿咨询", "bcg",
    "amazon web services", "aws", "microsoft", "google", "apple", "nvidia",
    "阿里巴巴", "百度", "美团", "京东", "拼多多",
)
INSTITUTION_T15_MARKERS = (
    "农业银行", "中国农业银行", "邮储银行", "中国邮政储蓄银行",
    "中国移动", "中国电信", "中国联通", "中国联合网络通信", "中国铁塔",
    "中信期货", "中信建投", "华泰证券", "国泰海通", "平安银行", "中证信用",
    "华为", "大疆", "dji", "中芯国际", "smic", "hsbc", "汇丰", "ubs", "citi",
    "罗兰贝格", "roland berger", "deloitte", "德勤", "pwc", "普华永道",
    "ey", "安永", "kpmg", "毕马威", "天翼云", "联通数科", "平安科技",
    "华为终端云",
)
CORE_SUBSIDIARY_MARKERS = (
    "天翼云", "联通数科", "中证信用", "平安科技", "华为终端云",
)

# These exact public jobs were explicitly calibrated in the first usable
# Future Radar release.  A stable id or URL is necessary but not sufficient:
# company and title must also match, so a reused campaign URL cannot turn an
# unrelated vacancy into a trusted anchor.  Arbitrary source tags such as
# `T0` never enter this trust boundary.
CURATED_JOB_TIER_ANCHORS_BY_ID = {
    "monitor-756f4c9a12018115fe2580c0": ("Point72", "Point72 Academy Investment Analyst Program for Upcoming Graduates（2027 – HK）", "T0.5"),
    "monitor-435fbfa31b2181c0139a6f01": ("BytePlus（字节跳动）", "Strategy Manager Graduate（BytePlus）– 2027 Start", "T0.5"),
    "monitor-6c490d4b9a88994bbb291d52": ("HSBC 汇丰", "Markets – Sales and Trading – Graduate", "T1"),
    "monitor-af74366074399c64a116a5a1": ("Goldman Sachs 高盛", "2027 APEJ Hong Kong Compliance New Analyst", "T1"),
    "monitor-b5806098f02357821d7b882e": ("Goldman Sachs 高盛", "2027 APEJ Hong Kong Investment Banking Classic New Analyst", "T1"),
    "monitor-fb7614a0bb5f6827479f3b33": ("Roland Berger 罗兰贝格", "Campus Recruitment 2027 Junior Consultant – Shanghai", "T1"),
    "monitor-0043a79b1459d30533dbf2b4": ("Amazon Web Services", "Program Manager – Investment, Early Career – 2027, Strategic Investment & GTM", "T1"),
    "monitor-7285abedd82792d46f05bb6a": ("DJI 大疆创新", "2027“拓疆者”数字管理构建者计划｜数字管理研发工程师", "T1"),
    "monitor-fe54585fadc44217601f7f5f": ("Amazon Web Services", "Sales Ops Analyst – Beijing, Early Career – 2027", "T1.5"),
    "monitor-9c3093ed8f8bca9b0303dd74": ("Goldman Sachs 高盛", "2027 APEJ Hong Kong FICC and Equities Quantitative Strats New Analyst", "T1.5"),
    "monitor-4a1dc474206bbd808337476c": ("中芯国际", "大数据工程师-张江（2027届校招）", "T1"),
    "monitor-4db8c6bde4658af4c2fa6d6d": ("中芯国际", "大数据工程师（2027届校招）", "T1"),
    "monitor-b9b5b111ac5b0448e7291581": ("中芯国际", "算法工程师-张江（2027届校招）", "T1.5"),
    "monitor-e6f3117522b597feb95e1682": ("中芯国际", "算法工程师（2027届校招）", "T1.5"),
    "monitor-548ad831bfc0a3a96b7c8e3a": ("中芯国际", "智能制造算法工程师（2027届校招）", "T1.5"),
    "monitor-6c74b12148797b5a36ae9dcd": ("中芯国际", "智能制造算法工程师（2027届校招）", "T1.5"),
}
CURATED_JOB_ANCHOR_ID_BY_URL = {
    "https://job-boards.greenhouse.io/point72/jobs/8572402002": "monitor-756f4c9a12018115fe2580c0",
    "https://joinbytedance.com/search/7666025583887173893": "monitor-435fbfa31b2181c0139a6f01",
    "https://apply.careers.hsbc.com/emergingtalent/job/central-markets-sales-and-trading-graduate-hong/1365763657": "monitor-6c490d4b9a88994bbb291d52",
    "https://higher.gs.com/roles/170760": "monitor-af74366074399c64a116a5a1",
    "https://higher.gs.com/roles/170778": "monitor-b5806098f02357821d7b882e",
    "https://jobs.smartrecruiters.com/rolandberger/744000142586389-campus-recruitment-2027-junior-consultant-shanghai": "monitor-fb7614a0bb5f6827479f3b33",
    "https://amazon.jobs/en/jobs/10501900/program-manager-investment-early-career-2027-strategic-investment-gtm": "monitor-0043a79b1459d30533dbf2b4",
    "https://careers.dji.com/zh-cn/campus/digital-recruitment": "monitor-7285abedd82792d46f05bb6a",
    "https://www.amazon.jobs/en/jobs/10503293/sales-ops-analyst-beijing-early-career-2027": "monitor-fe54585fadc44217601f7f5f",
    "https://higher.gs.com/roles/182119": "monitor-9c3093ed8f8bca9b0303dd74",
    "https://smics.zhiye.com/campusxq?c=&jc=2&jobid=390852385&ky=&p=1%5e21%2c3%5e-1": "monitor-4a1dc474206bbd808337476c",
    "https://smics.zhiye.com/campusxq?c=&jc=2&jobid=390852326&ky=&p=1%5e21%2c3%5e-1": "monitor-4db8c6bde4658af4c2fa6d6d",
    "https://smics.zhiye.com/campusxq?c=&jc=2&jobid=390852390&ky=&p=1%5e21%2c3%5e-1": "monitor-b9b5b111ac5b0448e7291581",
    "https://smics.zhiye.com/campusxq?c=&jc=2&jobid=390852323&ky=&p=1%5e21%2c3%5e-1": "monitor-e6f3117522b597feb95e1682",
    "https://smics.zhiye.com/campusxq?c=&jc=2&jobid=390852320&ky=&p=1%5e21%2c3%5e-1": "monitor-548ad831bfc0a3a96b7c8e3a",
    "https://smics.zhiye.com/campusxq?c=&jc=2&jobid=390852243&ky=&p=1%5e21%2c3%5e-1": "monitor-6c74b12148797b5a36ae9dcd",
}

# The original product specification also named these concrete company-role
# examples.  They are intentionally narrow phrase signatures, not a rule that
# every job at the same company inherits the same tier.
CURATED_ROLE_TIER_RULES = (
    ("南方基金", "ai产品", "T1"),
    ("kearney", "business analyst", "T1"),
    ("科尔尼", "business analyst", "T1"),
    ("lek consulting", "associate", "T1"),
    ("中信期货", "风险", "T1.5"),
    ("平安银行资金运营中心", "金融市场培训生", "T1.5"),
    ("华为终端云", "ai产品", "T1.5"),
    ("中证信用", "风险数据产品", "T1.5"),
)

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
    "中国移动", "中国电信", "中国联通", "中国联合网络通信", "中国商飞", "华为", "腾讯", "阿里",
    "字节", "byteplus", "蚂蚁", "美团", "京东", "百度", "拼多多", "大疆", "dji",
    "中芯国际", "smic", "hsbc", "汇丰", "ubs", "citi", "罗兰贝格", "roland berger",
    "中信期货", "中信建投", "华泰证券", "国泰海通", "嘉实基金", "富国基金",
    "国家烟草专卖局", "中国烟草总公司", "中烟工业", "上海烟草集团",
    "deloitte", "德勤", "pwc", "普华永道", "ey", "安永", "kpmg", "毕马威",
)

FINANCE_TERMS = (
    "金融", "finance", "financial", "会计", "accounting", "投资", "investment", "资本", "金融市场", "交易",
    "trading", "信用风险", "credit risk", "市场风险", "market risk", "信贷", "credit", "估值", "审计", "audit",
    "财务", "fp&a", "证券", "基金", "银行", "tax", "税务",
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
LOW_VALUE_TITLE_TERMS = {
    "柜员", "客户经理", "渠道销售", "客服专员", "客服代表", "行政支持", "资料录入",
    "测试工程师", "运维工程师", "devops engineer", "实施工程师", "实施顾问", "售后",
    "shared service", "sales support", "administrative support", "back office support",
    "business support", "产品支撑", "售前支撑",
}
PRIMARY_ROUTINE_DUTY_TERMS = {
    "负责柜面", "负责客户拓展", "承担销售指标", "完成销售目标", "负责渠道销售",
    "负责客服", "接听客服热线", "处理客户投诉", "处理客户咨询", "负责工单处理",
    "负责资料录入", "负责行政事务", "负责测试执行", "负责缺陷记录", "负责驻场运维",
    "负责系统实施", "负责售后服务", "business development quota", "sales quota",
    "负责培训陪访", "负责业务培训", "负责产品培训", "负责业务宣传", "负责产品宣传",
    "负责产品支撑", "承担培训陪访", "承担业务宣传", "承担产品支撑",
    "培训陪访", "产品支撑", "业务宣传", "指标下达", "收入完成",
    "handle customer complaints", "answer customer calls", "execute test cases",
    "data entry", "routine filing",
}
PRIMARY_HARD_TECH_DUTY_TERMS = {
    "负责c++开发", "负责后端开发", "负责底层开发", "负责infra", "负责devops",
    "负责运维", "负责操作系统", "负责芯片研发", "负责算法开发", "负责大模型训练",
    "承担平台开发", "承担平台运维", "承担运维", "平台开发、运维",
    "develop c++", "build backend systems", "build infrastructure", "own devops",
}
HARD_TECH_TITLE_TERMS = {
    "c++开发", "后端开发", "底层开发", "infrastructure engineer", "infra engineer",
    "devops engineer", "运维工程师", "操作系统工程师", "芯片研发", "芯片设计",
    "芯片工程师", "算法工程师", "大模型训练", "深度学习训练", "测试工程师",
}
HIGH_QUANT_BARRIER_TERMS = (
    "hft", "超低延迟", "low latency", "随机微积分", "stochastic calculus", "数学竞赛", "quantitative strats",
    "pure alpha research", "纯alpha", "精通c++", "expert c++", "advanced c++", "high-performance c++",
)
HIGH_INTENSITY_TERMS = (
    "高强度", "investment banking", "投行", "sales and trading", "战略咨询", "consultant", "咨询", "hft",
)

# Some ATS adapters preserve the complete JD in `requirements`. Read the
# actual duty section there, not the list of accepted majors or the group's
# marketing introduction. These are evidence boundaries, not AI inferences.
ROLE_SECTION = re.compile(
    r"[【\[]?(?:岗位职责|工作职责|主要职责|职位职责|职责描述|工作内容|工作描述|岗位描述|"
    r"job responsibilities|key responsibilities|responsibilities|what you(?:'|’)ll do)"
    r"(?:\s*[:：]|\s*[】\]]|[ \t]*\r?\n)",
    re.IGNORECASE,
)
NON_ROLE_SECTION = re.compile(
    r"[【\[]?(?:任职资格|任职要求|任职条件|应聘条件|应聘要求|职位要求|岗位要求|资格要求|专业要求|学历要求|招聘条件|"
    r"基本条件|招聘对象|其他要求|研究方向|专业方向|工作地点|公司简介|企业简介|集团简介|关于我们|薪酬福利|"
    r"福利待遇|招聘部门|招聘单位|qualifications|requirements|about us|"
    r"about the company|what we offer|benefits)(?:\s*[:：]|\s*[】\]]|[ \t]*\r?\n)",
    re.IGNORECASE,
)
EMPLOYER_BOILERPLATE = re.compile(
    r"公司简介|企业简介|集团简介|关于我们|总部(?:位于|设在|坐落)|"
    r"(?:本公司|我们公司|本集团|我们集团|公司|集团)(?:是|成立|创立|拥有|业务覆盖|"
    r"业务涵盖|主营|致力于|布局|专注于)|"
    r"\b(?:about us|about (?:the |our )?company|our company|our group|"
    r"headquartered|founded in|established in)\b",
    re.IGNORECASE,
)
DUTY_ACTION = re.compile(
    r"^\s*(?:[\d一二三四五六七八九十]+[.、)）]\s*)?"
    r"(?:(?:本岗位|该岗位|主要|将)\s*)*(?:负责|参与|承担|开展|构建|制定|"
    r"完成|执行|协助|推进|组织|统筹)|"
    r"^\s*(?:\d+[.)]\s*)?(?:(?:you will|you'll|responsible for)\s+)?"
    r"(?:build|develop|research|analy[sz]e|design|deliver|conduct|manage|"
    r"support|operate|maintain|advise|audit)\b",
    re.IGNORECASE,
)
QUALIFICATION_LEAD = re.compile(
    r"^\s*(?:[\d一二三四五六七八九十]+[.、)）]\s*)?(?:"
    r"研究生|研究方向|专业方向|专业要求|学历要求|学位要求|任职条件|应聘条件|"
    r"(?:研究|管理|分析|开发|设计)(?:能力|经验|背景|经历|学位|学历|学专业)|"
    r"\b(?:research degree|research experience|research interests|research background|"
    r"management experience|development experience|analysis skills)\b)",
    re.IGNORECASE,
)
PAST_EXPERIENCE_REQUIREMENT = re.compile(
    r"(?:负责|参与|承担|开展|构建|制定|完成|执行|协助|推进|组织|统筹)过|"
    r"(?:曾经?|有过).{0,80}(?:负责|参与|项目|工作)|者优先|"
    r"(?:具备|具有|拥有|有).{0,100}(?:相关经验|项目经验|工作经验|经验者|经历者)|"
    r"\b(?:prior|previous|past)\s+experience\b|\bexperience\s+(?:preferred|required)\b",
    re.IGNORECASE,
)
ROLE_SENTENCE_BOUNDARY = re.compile(r"[\n\r]+|(?<=[。；;!?！？])\s*|(?<=\.)\s+")

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
# "资金运营 / 投资运营 / 交易运营" and professional support functions are
# not the same as repetitive content operations or back-office support.  The
# latter are detected from a routine title or explicit primary-duty evidence.
LOW_VALUE_ROLE_TAGS = {"sales", "customer_service", "operations", "support"}
PROFESSIONAL_ROLE_TAGS = {"audit", "tax", "compliance", "advisory", "consulting"}
PROFESSIONAL_OPERATIONS_TERMS = {
    "资金运营", "投资运营", "交易运营", "金融市场", "treasury", "trading operations",
    "investment operations", "portfolio operations", "settlement operations",
}
PROFESSIONAL_MARKETS_SALES_TERMS = {
    "sales and trading", "sales & trading", "global markets", "financial markets",
    "institutional sales", "markets sales", "金融市场", "销售交易", "机构销售",
}
PRIMARY_SALES_TERMS = {
    "纯销售", "客户经理", "渠道销售", "渠道拓展", "客户拓展", "商机转化",
    "销售目标", "销售业绩", "营销目标", "业绩指标", "陪访", "account executive",
    "relationship manager", "business development", "sales target", "sales quota",
}
ROUTINE_SUPPORT_TITLE_TERMS = {
    "business support", "support analyst", "support specialist", "operations analyst",
    "operation analyst", "行政支持", "后台支持", "支持岗", "普通运营", "共享服务",
    "shared service",
}
STRATEGIC_OPERATIONS_TITLE_TERMS = {
    "strategy and operations", "strategy & operations", "strategic operations",
    "战略运营", "策略运营", "风险运营", "投资运营", "资金运营", "交易运营",
}

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


def _duty_text(value: Any, *, qualification_field: bool = False) -> str:
    """Keep job duties; never turn a company's introduction/major list into JD.

    Full ATS pages can arrive in `requirements`, so dropping that field would
    lose real duties. Labeled duty sections take precedence. In an unlabeled
    qualification field only explicit work-action sentences are useful.
    """
    text = " ".join(_as_values(value)).strip()
    if not text:
        return ""
    sections = list(ROLE_SECTION.finditer(text))
    if sections:
        parts = []
        for index, marker in enumerate(sections):
            end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
            stop = NON_ROLE_SECTION.search(text, marker.end(), end)
            parts.append(text[marker.end():stop.start() if stop else end])
        text = " ".join(parts)
        qualification_field = False
    else:
        # A standalone JD must not accidentally include the qualifications or
        # employer introduction that follows the work itself.
        stop = NON_ROLE_SECTION.search(text)
        if stop and not qualification_field:
            text = text[:stop.start()]
    sentences = []
    for sentence in ROLE_SENTENCE_BOUNDARY.split(text):
        sentence = sentence.strip()
        if not sentence or EMPLOYER_BOILERPLATE.search(sentence) or QUALIFICATION_LEAD.search(sentence):
            continue
        if qualification_field and (
            PAST_EXPERIENCE_REQUIREMENT.search(sentence) or not DUTY_ACTION.search(sentence)
        ):
            continue
        sentences.append(sentence)
    return " ".join(sentences).casefold()


def _role_source_text(job: dict[str, Any]) -> str:
    parts = [str(job.get("title") or "").casefold()]
    # Explicit responsibilities are authoritative for the actual work. Do not
    # let a broad company/program description override a concrete duty field.
    duties = _duty_text(job.get("responsibilities"))
    if duties:
        parts.append(duties)
    else:
        parts.extend((
            _duty_text(job.get("description")),
            _duty_text(job.get("requirements"), qualification_field=True),
        ))
    return " ".join(part for part in parts if part).strip()


def _organization_assessment(job: dict[str, Any]) -> dict[str, Any]:
    company = str(job.get("company") or "").casefold()
    organization_job = job
    parent_company = str(job.get("parent_company") or "")
    internal_parent_unit = bool(re.search(
        r"(?:事业部|部门|部|司|局|中心|研究院|研究所)$", _identity_text(company),
    ))
    if (
        internal_parent_unit
        and parent_company
        and canonical_employer_identity(company)
        and canonical_employer_identity(company) == canonical_employer_identity(parent_company)
    ):
        # An internal department/research unit is not an independent subsidiary
        # merely because a richer source also includes its parent company.
        organization_job = {key: value for key, value in job.items() if key != "parent_company"}
    if _company_matches_any(company, CORE_SUBSIDIARY_MARKERS):
        # The full legal name and the familiar short name must describe the
        # same separately incorporated core subsidiary.
        organization_job = {**job, "subsidiary": job.get("subsidiary") or job.get("company")}
    else:
        structured_core = next(
            (
                str(job.get(field) or "")
                for field in (
                    "subsidiary", "subsidiary_name", "hiring_entity", "recruiting_entity",
                    "employer_entity", "hiring_unit", "recruitment_unit",
                )
                if _company_matches_any(
                    str(job.get(field) or ""), CORE_SUBSIDIARY_MARKERS,
                )
            ),
            "",
        )
        if structured_core:
            # A maintained core subsidiary named in a structured hiring field
            # is the actual platform being scored, even when ``company`` holds
            # only the parent group used by the source campaign.
            organization_job = {**organization_job, "subsidiary": structured_core}
    # Both passes use the same public hiring-entity evidence. The platform
    # baseline changes after identity lookup; parsing the JD again cannot.
    organization_evidence = collect_organization_evidence(organization_job)
    probe = assess_organization(
        organization_job, base_platform_points=8, platform_band="平台识别探针",
        evidence=organization_evidence,
    )
    scoring_company = str(probe.get("entity_name") or company).casefold()
    institution_identity = _institution_identity_for_hiring_unit(scoring_company)
    if (
        not institution_identity
        and str(probe.get("level") or "") in {
            "provincial_branch", "city_branch", "local_branch", "branch_unspecified",
        }
        and str(probe.get("entity_source") or "") != "company"
        and _EXPLICIT_BRANCH_UNIT.search(scoring_company)
        and not _UNSAFE_PARENT_INHERITANCE.search(scoring_company)
    ):
        # Some ATS feeds keep the parent in ``company`` but write only
        # ``山东分公司数字化发展部`` in the structured hiring-unit field.  Keep
        # that lower unit as the organization being scored, while using the
        # exact maintained parent identity solely for its institution baseline.
        institution_identity = canonical_employer_identity(company)
    calibration_company = institution_identity or scoring_company
    institution_tier, _ = _institution_identity_calibration(calibration_company)
    if institution_tier in {"T0", "T0.5", "T1"}:
        points, band = 14, "头部平台基准"
    elif institution_tier == "T1.5":
        points, band = 13, "重点平台基准"
    elif institution_tier == "T2":
        points, band = 11, "监控机构基准"
    elif _company_matches_any(scoring_company, ELITE_PLATFORM_MARKERS):
        points, band = 14, "头部平台基准"
    elif _company_matches_any(scoring_company, STRONG_PLATFORM_MARKERS):
        points, band = 13, "重点平台基准"
    else:
        # Category membership is a search/filter concern, not platform
        # evidence.  An unknown employer must not gain seven weighted points
        # merely because a producer labelled it telecom, bank or internet.
        points, band = 8, "平台资料有限；行业分类不参与机构定级"
    assessment = assess_organization(
        organization_job, base_platform_points=points, platform_band=band,
        evidence=organization_evidence,
    )
    # Publish the same rounded dimension used by the direct 100-point score.
    # Consumers must not re-round 62.5 differently in JavaScript.
    return {
        **assessment,
        "employer_identity": scoring_company,
        "institution_identity": calibration_company,
        "base_platform_score": round(assessment["base_platform_points"] / 16 * 100),
        "platform_score": round(
            round(assessment["platform_points"] / 16 * BREAKDOWN_LIMITS["platform"])
            / BREAKDOWN_LIMITS["platform"] * 100
        ),
    }


@lru_cache(maxsize=1024)
def _marker_rule(marker: str) -> tuple[str, re.Pattern[str] | None]:
    """Compile the static matching vocabulary once, without caching job text."""
    marker = marker.casefold().strip()
    if marker and re.fullmatch(r"[a-z0-9_+.# -]+", marker):
        escaped = re.escape(marker).replace(r"\ ", r"\s+")
        return marker, re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return marker, None


def _marker_matches(text: str, marker: str) -> bool:
    normalized, pattern = _marker_rule(marker)
    if not normalized:
        return False
    # A single escaped token must occur literally before its boundary regex
    # can match. Multi-word markers retain their flexible whitespace rules.
    if pattern and " " not in normalized and normalized not in text:
        return False
    return pattern.search(text) is not None if pattern else normalized in text


def _contains_any(text: str, markers: tuple[str, ...] | set[str]) -> bool:
    return any(_marker_matches(text, marker) for marker in markers)


def _company_matches_marker(company: str, marker: str) -> bool:
    """Match one maintained employer identity, never an arbitrary prefix."""
    canonical_company = canonical_employer_identity(company)
    canonical_marker = canonical_employer_identity(marker)
    if canonical_marker:
        # A maintained marker must also resolve through the maintained identity
        # grammar.  Never fall back to raw startswith after that check fails.
        return bool(canonical_company) and canonical_company == canonical_marker

    company_key = _identity_text(company)
    marker_key = _identity_text(marker)
    if not company_key or not marker_key:
        return False
    if company_key == marker_key:
        return True
    if not company_key.startswith(marker_key):
        return False
    suffix = company_key[len(marker_key):]
    # Fallback only exists for narrow calibration entries that are not yet in
    # the public employer directory.  Accept legal/hiring-unit continuations;
    # reject lexical collisions such as 中国银行间市场交易商协会.
    legal = r"(?:集团)?(?:股份有限公司|有限责任公司|有限公司|公司)"
    unit = (
        r"(?:集团总部|总部|总行|[\u4e00-\u9fff]{2,24}"
        r"(?:省|市|自治区|自治州|地区|县|区|旗)?"
        r"(?:分公司|分行|支行|支公司|分部|营业部|办事处))"
    )
    return bool(re.fullmatch(rf"(?:{legal}|(?:{legal})?{unit})", suffix))


def _company_matches_any(company: str, markers: tuple[str, ...] | set[str]) -> bool:
    # Only public name/maintained marker comparisons are shared. Profiles,
    # mutable job payloads and computed scores never enter this cache.
    if len(company) <= 500:
        return _cached_company_matches_any(company, frozenset(markers))
    return any(_company_matches_marker(company, marker) for marker in markers)


@lru_cache(maxsize=4096)
def _cached_company_matches_any(company: str, markers: frozenset[str]) -> bool:
    return any(_company_matches_marker(company, marker) for marker in markers)


def _identity_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


_EXPLICIT_BRANCH_UNIT = re.compile(
    r"分公司|分行|支行|支公司|分部|营业部|办事处|"
    r"\bbranch(?:\s+office)?\b",
    re.IGNORECASE,
)
_UNSAFE_PARENT_INHERITANCE = re.compile(
    r"协会|学会|商会|交易商|银行业|银行间|银行保险信息|移动互联网|"
    r"合作伙伴|供应商|服务商|代理商|外包|劳务|派遣|"
    r"\b(?:association|partner|supplier|vendor|contractor|outsourc\w*|staffing)\b",
    re.IGNORECASE,
)
_BOUNDED_BRANCH_UNIT_SUFFIX = re.compile(
    r"[0-9a-z\u4e00-\u9fff]{0,32}"
    r"(?:分公司|分行|支行|支公司|分部|营业部|办事处|branch(?:office)?)"
    r"[0-9a-z\u4e00-\u9fff]{0,48}",
    re.IGNORECASE,
)


def _institution_identity_for_hiring_unit(value: Any) -> str:
    """Resolve a maintained parent without erasing a lower hiring unit.

    The public directory intentionally rejects arbitrary prefix continuations.
    That is the correct general identity boundary, but an ATS can append both
    an explicit branch and its internal department, for example
    ``中信证券山东分公司数字化发展部``.  Resolve only when a prefix is itself an
    exact maintained identity and the remaining suffix contains an explicit
    branch noun.  The organization assessment still retains the branch level,
    so this cannot manufacture a headquarters score.
    """
    raw = str(value or "").strip()
    if not raw or len(raw) > 500 or _UNSAFE_PARENT_INHERITANCE.search(raw):
        return ""
    direct = canonical_employer_identity(raw)
    if direct:
        return direct
    if not _EXPLICIT_BRANCH_UNIT.search(raw):
        return ""

    compact = _identity_text(raw)
    branch = _EXPLICIT_BRANCH_UNIT.search(compact)
    if not branch:
        return ""
    # Prefer the longest exact maintained prefix.  This preserves a full legal
    # employer name when present and avoids choosing a shorter lexical prefix.
    for end in range(branch.start(), 1, -1):
        suffix = compact[end:]
        if not _BOUNDED_BRANCH_UNIT_SUFFIX.fullmatch(suffix):
            continue
        parent = canonical_employer_identity(compact[:end])
        if parent:
            return parent
    return ""


_TIER_ORDER = tuple(definition["code"] for definition in TIER_DEFINITIONS)


def _worse_tier(left: str, right: str) -> str:
    return _TIER_ORDER[max(_TIER_ORDER.index(left), _TIER_ORDER.index(right))]


def _institution_identity_calibration(company: str) -> tuple[str | None, str]:
    """Resolve one identity once for both platform points and institution tier."""
    if _company_matches_any(company, INSTITUTION_T0_MARKERS):
        return "T0", "政策性金融/核心金融基础设施"
    if _company_matches_any(company, INSTITUTION_T05_MARKERS):
        return "T0.5", "准终极平台"
    if _company_matches_any(company, INSTITUTION_T1_MARKERS):
        return "T1", "核心主申平台"
    if _company_matches_any(company, INSTITUTION_T15_MARKERS):
        return "T1.5", "高质量重点平台"
    if _company_matches_any(company, ELITE_PLATFORM_MARKERS):
        return "T1", "头部平台"
    if _company_matches_any(company, STRONG_PLATFORM_MARKERS):
        return "T2", "重点监控平台"
    canonical = canonical_employer_identity(company)
    if canonical and canonical in monitored_employer_identities():
        # This is an exact identity from the maintained left-hand monitor
        # directory, not a score inferred from an industry label.  Unreviewed
        # directory employers receive a deliberately conservative baseline;
        # the concrete role can still move independently under the original
        # eleven-dimension model.
        return "T2", "重点监控机构基准"
    return None, "机构资料不足"


def _institution_baseline(
    job: dict[str, Any], organization: dict[str, Any],
) -> dict[str, Any]:
    """Return an institution/hiring-unit baseline independent of the job.

    The directory is a versioned product calibration, not a social prestige
    ranking.  An industry category alone never establishes a baseline.  The
    actual hiring unit then applies the original HQ > province > city > local
    rule without pretending that a provincial headquarters is group HQ.
    """
    company = str(
        organization.get("institution_identity")
        or organization.get("employer_identity")
        or job.get("company")
        or ""
    ).casefold()
    tier, band = _institution_identity_calibration(company)
    if tier is None:
        return {
            "tier_code": None,
            "score": None,
            "band": "机构资料不足",
            "reason": "只有行业分类或单位名称，未据此制造机构 T 级",
        }

    level = str(organization.get("level") or "unspecified")
    is_core_subsidiary = _company_matches_any(company, CORE_SUBSIDIARY_MARKERS)
    is_bank = _company_matches_any(company, {
        "中国人民银行", "国家开发银行", "中国进出口银行", "中国农业发展银行",
        "工商银行", "中国工商银行", "农业银行", "中国农业银行", "建设银行",
        "中国建设银行", "中国银行", "交通银行", "邮储银行", "中国邮政储蓄银行",
    })
    is_telecom = _company_matches_any(company, {
        "中国移动", "中国电信", "中国联通", "中国联合网络通信", "中国铁塔",
    })
    if level == "provincial_branch":
        if is_bank:
            tier = _worse_tier(tier, "T1.5")
        elif is_telecom:
            tier = _worse_tier(tier, "T2")
        else:
            tier = _worse_tier(tier, "T2")
    elif level == "city_branch":
        tier = _worse_tier(tier, "T2.5")
    elif level == "local_branch":
        tier = _worse_tier(tier, "T3")
    elif level == "branch_unspecified":
        # Unknown city/province names must not be optimistically treated as a
        # provincial headquarters. Exact province units are recognized above.
        tier = _worse_tier(tier, "T2.5")
    elif level == "subsidiary" and not is_core_subsidiary:
        tier = _worse_tier(tier, "T2.5")
    elif level == "research_institute" and tier not in {"T0", "T0.5"}:
        tier = _worse_tier(tier, "T1.5")
    elif level == "third_party":
        tier = "T3"

    return {
        "tier_code": tier,
        "score": TIER_TARGET_SCORES[tier],
        "band": band,
        "reason": f"{band}；实际招聘主体按{organization.get('label', '组织层级待核验')}校准",
    }


def _curated_job_tier_anchor(
    job: dict[str, Any], organization: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve only exact code-owned identities; never consume source T tags."""
    company = _identity_text(job.get("company"))
    title = _identity_text(job.get("title"))

    def identity_tier(anchor_id: str) -> str | None:
        anchor = CURATED_JOB_TIER_ANCHORS_BY_ID.get(anchor_id)
        if not anchor:
            return None
        expected_company, expected_title, tier = anchor
        if company == _identity_text(expected_company) and title == _identity_text(expected_title):
            return tier
        return None

    for field in ("id", "external_id", "source_item_id"):
        tier = identity_tier(str(job.get(field) or "").strip())
        if tier:
            return tier, "first_release"
    for field in ("application_url", "canonical_url", "official_url", "url"):
        url = str(job.get(field) or "").strip().casefold().rstrip("/")
        tier = identity_tier(CURATED_JOB_ANCHOR_ID_BY_URL.get(url, ""))
        if tier:
            return tier, "first_release"

    excluded_named_example_titles = {
        "intern", "internship", "实习", "director", "总监", "assistant", "助理",
        "support", "支持", "operations", "运营", "客服", "销售",
    }
    duty_job = {**job, "title": ""}
    duty_text = _role_source_text(duty_job)
    duty_tags = set(_normalize_role_tags(duty_job))
    named_example_has_core_evidence = bool(
        duty_text
        and duty_tags.intersection(HIGH_VALUE_ROLE_TAGS | PROFESSIONAL_ROLE_TAGS)
    )
    named_company = str(
        (organization or {}).get("employer_identity") or job.get("company") or ""
    )
    for company_marker, title_marker, tier in CURATED_ROLE_TIER_RULES:
        if (
            _company_matches_marker(named_company, company_marker)
            and _identity_text(title_marker) in title
            and not _contains_any(str(job.get("title") or "").casefold(), excluded_named_example_titles)
            and named_example_has_core_evidence
        ):
            return tier, "named_example"
    return None, None


@lru_cache(maxsize=2048)
def _category_codes_for_text(text: str) -> frozenset[str]:
    """Memoize one normalized taxonomy value, never a job/profile or score.

    Category rules are module constants. Changed record fields use new content
    keys; a deployment loads fresh rules and caches. Immutable entries cannot
    be changed by a caller that combines or edits its returned categories.
    """
    if text in CATEGORY_ORDER:
        return frozenset((text,))
    codes: set[str] = set()
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
    return frozenset(codes)


def _category_codes_for_value(value: Any) -> set[str]:
    codes: set[str] = set()
    for raw in _as_values(value):
        text = raw.casefold().strip()
        # Bound retained input size as well as the LRU entry count. Unusually
        # long metadata still gets the same classification, without retention.
        lookup = (
            _category_codes_for_text
            if len(text) <= 512
            else _category_codes_for_text.__wrapped__
        )
        codes.update(lookup(text))
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
    """Choose one UI starfield from a directory identity or employer metadata."""
    return (employer_category_override(job)
            or _primary_category(job, semantic_employer_categories(job)))


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
    # A broad campaign's precomputed role tags are not evidence of this job's
    # duties. Re-derive scoring tags from the actual title/work instead of
    # perpetuating a stale or over-broad AI/finance annotation.
    return [
        code for code, markers in ROLE_TAG_MARKERS.items()
        if _contains_any(source_text, set(markers))
    ]


def _role_text(job: dict[str, Any], role_tags: list[str] | None = None) -> str:
    normalized = role_tags if role_tags is not None else _normalize_role_tags(job)
    return f"{_role_source_text(job)} {' '.join(normalized)}".strip()


def _is_low_value_role(role_tags: list[str], role_text: str, title: str = "") -> bool:
    tags = set(role_tags).intersection(LOW_VALUE_ROLE_TAGS)
    title_text = str(title).casefold()
    title_role_tags = set(_normalize_role_tags({"title": title_text}))
    title_has_core_role = bool(title_role_tags.intersection(HIGH_VALUE_ROLE_TAGS))
    professional_markets_sales = (
        "financial_markets" in role_tags
        and _contains_any(role_text, PROFESSIONAL_MARKETS_SALES_TERMS)
    )
    title_is_sales = (
        "sales" in tags
        and not professional_markets_sales
        and _contains_any(title_text, {"sales", "销售", "渠道", "客户经理", "business development"})
        and not title_has_core_role
    )
    duties_are_sales = (
        "sales" in tags
        and not professional_markets_sales
        and _contains_any(role_text, PRIMARY_SALES_TERMS)
    )
    title_is_customer_service = (
        "customer_service" in tags
        and _contains_any(title_text, {"customer service", "customer support", "客服", "客户服务"})
        and not title_has_core_role
    )
    if title_is_customer_service or title_is_sales or duties_are_sales:
        return True
    routine_duty_count = sum(
        1 for marker in PRIMARY_ROUTINE_DUTY_TERMS if _marker_matches(role_text, marker)
    )
    # One implementation/support phrase can be incidental to a substantive
    # product, research or consulting role.  Two independent routine duties
    # show that the actual job is support-led even when its title says 产品经理.
    if routine_duty_count >= 2:
        return True
    if routine_duty_count == 1 and not title_has_core_role:
        return True
    if _contains_any(title_text, LOW_VALUE_TITLE_TERMS):
        return True
    if tags.intersection({"operations", "support"}):
        if _contains_any(role_text, PROFESSIONAL_OPERATIONS_TERMS):
            return False
        strategic_operations = _contains_any(title_text, STRATEGIC_OPERATIONS_TITLE_TERMS)
        routine_title = _contains_any(title_text, ROUTINE_SUPPORT_TITLE_TERMS)
        if routine_title and not strategic_operations:
            return True
        if strategic_operations or title_has_core_role:
            return False
        # A verb such as "support" inside legal/research/professional work is
        # not enough.  Generic support/operations is low only when the title or
        # primary-duty evidence above establishes that it is the actual role.
        return False
    return False


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
    jd_text = _role_source_text({**job, "title": ""})
    compact = re.sub(r"\s+", "", jd_text)
    jd_job = {
        "title": "",
        "responsibilities": jd_text,
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
    job: dict[str, Any], profile: dict[str, Any], role_tags: list[str],
    organization: dict[str, Any],
) -> tuple[dict[str, int], list[str], list[str], list[str]]:
    text = _job_text(job)
    role_text = _role_text(job, role_tags)
    city_text = f"{job.get('city', '')} {job.get('title', '')}".casefold()
    tags = {str(tag).strip() for tag in (job.get("tags") or [])}
    role_tag_set = set(role_tags)
    positives: list[str] = []
    negatives: list[str] = []
    fit_tags: list[str] = []

    # The organization assessor deliberately keeps its conservative 0–16
    # evidence scale.  Convert it only at final scoring, so hierarchy checks
    # still use the same calibrated company/headquarters evidence.
    platform_evidence = organization["platform_points"]
    platform = round(platform_evidence / 16 * BREAKDOWN_LIMITS["platform"])
    is_headquarters = organization["is_group_headquarters"]
    if is_headquarters:
        positives.append("招聘单位明确为集团总部或总行；未将地区本部当作集团总部")
        fit_tags.append("集团总部/总行")
    elif organization["platform_adjustment"] < 0:
        negatives.append(organization["note"])
        fit_tags.append(organization["label"])
    elif organization["confidence"] == "unknown":
        negatives.append("招聘单位层级资料不足，未计入总部或核心子机构加分")
    if platform_evidence >= 13:
        positives.append("实际招聘平台具有较强资源；平台基准不直接决定岗位 T 级")

    high_value_role = bool(role_tag_set.intersection(HIGH_VALUE_ROLE_TAGS))
    low_value_role = _is_low_value_role(role_tags, role_text, str(job.get("title") or ""))
    job_quality = 8
    if high_value_role or _contains_any(role_text, set(CORE_ROLE_TERMS)):
        job_quality += 4
        positives.append("岗位靠近产品、风险、战略、投资或数字化核心")
    if role_tag_set.intersection(EXCEPTIONAL_ROLE_TAGS):
        job_quality += 2
        positives.append("岗位职能具备较高专业壁垒与长期价值")
    elif role_tag_set.intersection(PROFESSIONAL_ROLE_TAGS):
        job_quality += 2
    if _contains_any(
        role_text, {"轮岗", "导师", "定岗", "rotational", "mentorship", "structured training"}
    ):
        job_quality += 2
        positives.append("岗位职责明确提及培养、导师或轮岗安排")
        fit_tags.append("培养路径")
    elif "management_trainee" in role_tag_set:
        negatives.append("管培名称不等于明确的轮岗、定岗或管理晋升路径")
    if is_headquarters:
        job_quality += 1
    if low_value_role:
        job_quality -= 5
        negatives.append("岗位偏销售、基层、重复运营或支持工作")
        fit_tags.append("低优先级")
    job_quality = max(2, min(15, job_quality))

    # Routine work cannot be promoted by AI/finance buzzwords elsewhere in the
    # same JD. This adjusts the role component, not a company-specific T cap.
    routine_title = _contains_any(str(job.get("title") or "").casefold(), {
        "柜员", "客户经理", "客服", "销售", "行政", "录入", "shared service",
        "customer service", "customer support", "sales support", "back office support",
    })
    if routine_title:
        job_quality = min(job_quality, 7)

    background_groups = sum(
        1
        for terms in (FINANCE_TERMS, TECH_TERMS, PRODUCT_MANAGEMENT_TERMS)
        if _contains_any(role_text, set(terms))
    )
    background_utilization = {0: 2, 1: 4, 2: 7, 3: 8}[background_groups]
    if routine_title or low_value_role:
        background_utilization = min(background_utilization, 5)
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
    is_quant_role = bool(role_tag_set.intersection({"quant", "quant_research"}))
    title_text = str(job.get("title") or "").casefold()
    technical_hard = _contains_any(title_text, HARD_TECH_TITLE_TERMS) or _contains_any(
        role_text, PRIMARY_HARD_TECH_DUTY_TERMS,
    )
    quant_hard = is_quant_role and _contains_any(
        role_text, set(HIGH_QUANT_BARRIER_TERMS),
    )
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
    if routine_title or low_value_role:
        career_fit = min(career_fit, 6)

    career_ceiling = 6 + (2 if platform_evidence >= 13 else 1 if platform_evidence >= 11 else 0)
    if high_value_role:
        career_ceiling += 2
    if is_headquarters:
        career_ceiling += 1
    if organization["level"] in {"city_branch", "local_branch", "third_party"}:
        career_ceiling -= 1
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

    qualification_text = f"{role_text} {_joined_fields(job, ('requirements',))}"
    probability = 5
    if _contains_any(qualification_text, {"校园招聘", "校招", "应届", "graduate", "new analyst", "2027届"}):
        probability += 1
    if _contains_any(qualification_text, {"专业不限", "finance", "computer science", "business analytics", "金融", "计算机"}):
        probability += 1
    if _contains_any(qualification_text, {"博士", "phd", "数学竞赛", "工作经验", "仅限"}) or quant_hard:
        probability -= 2
        negatives.append("资格或竞争门槛较高，需作为冲刺评估")
        fit_tags.append("冲刺")
    probability = max(1, min(7, probability))

    # A famous parent does not establish this subsidiary/branch's salary.
    # Unpublished compensation stays neutral rather than earning a logo bonus.
    compensation = 3

    work_life_balance = 3
    if _contains_any(role_text, set(HIGH_INTENSITY_TERMS)) or "高强度" in tags:
        work_life_balance -= 1
        negatives.append("岗位类型可能具有较高工作强度，实际安排需核对")
    if _contains_any(role_text, {"轮班", "倒班", "夜班", "驻场", "on-call", "on call"}):
        work_life_balance -= 1
        negatives.append("职责包含值守、轮班或驻场安排，可持续性需权衡")
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


def _group_contributions(dimensions: dict[str, int]) -> dict[str, int]:
    """Aggregate the original 11 direct points into four UI groups."""
    return {
        "employer_platform": dimensions["platform"],
        "role_function": (
            dimensions["job_quality"] + dimensions["background_utilization"]
            + dimensions["career_fit"]
        ),
        "career_value": dimensions["career_ceiling"] + dimensions["mobility"],
        "job_conditions": (
            dimensions["probability"] + dimensions["compensation"]
            + dimensions["work_life_balance"] + dimensions["city"]
            + dimensions["further_education"]
        ),
    }


def _calibrated_job_score(
    job: dict[str, Any], dimensions: dict[str, int], role_tags: list[str],
    organization: dict[str, Any], institution: dict[str, Any],
) -> tuple[int, str | None, str | None]:
    """Apply narrow evidence-based calibration without rewriting dimensions."""
    raw_score = max(0, min(100, sum(dimensions.values())))
    curated_tier, anchor_kind = _curated_job_tier_anchor(job, organization)
    candidate_score = TIER_TARGET_SCORES[curated_tier] if curated_tier else raw_score
    level = str(organization.get("level") or "unspecified")
    ceiling = 100
    reasons: list[str] = []
    if curated_tier:
        reasons.append(
            "首版受控岗位锚点" if anchor_kind == "first_release"
            else "最初规则中的具名岗位校准"
        )
    institution_tier = institution.get("tier_code")

    # A baseline is not the final job tier. Standalone/core units can contain a
    # role above their institution baseline, while explicit regional hiring
    # units retain the original HQ > province > city > local ceiling.
    branch_levels = {"provincial_branch", "city_branch", "local_branch", "branch_unspecified"}
    if institution_tier in INSTITUTION_JOB_MAX_SCORES:
        maximum = (
            TIER_MAX_SCORES[institution_tier]
            if level in branch_levels
            else INSTITUTION_JOB_MAX_SCORES[institution_tier]
        )
        ceiling = min(ceiling, maximum)
        reasons.append(f"机构基准 {institution_tier} 与实际招聘主体层级")
    elif institution_tier is None and candidate_score >= 80 and anchor_kind != "first_release":
        # The original T1 definition requires a high-quality platform. An
        # unknown employer therefore remains at T1.5 or below until its exact
        # identity is calibrated; an attractive title alone cannot supply the
        # missing institution dimension.
        ceiling = min(ceiling, 79)
        reasons.append("机构平台资料不足，暂不进入 T0–T1")

    core_subsidiary = _company_matches_any(
        str(organization.get("employer_identity") or job.get("company") or ""),
        CORE_SUBSIDIARY_MARKERS,
    )
    level_ceiling = ORGANIZATION_LEVEL_MAX_SCORES.get(level)
    if level == "subsidiary" and core_subsidiary:
        level_ceiling = None
    if level == "research_institute" and institution_tier in {"T0", "T0.5"}:
        level_ceiling = None
    if level_ceiling is not None:
        ceiling = min(ceiling, level_ceiling)
        reasons.append(f"实际招聘主体为{organization.get('label', '非总部层级')}")

    role_tag_set = set(role_tags)
    role_text = _role_text(job, role_tags)
    low_value_role = _is_low_value_role(role_tags, role_text, str(job.get("title") or ""))
    if low_value_role and anchor_kind != "first_release":
        ceiling = min(ceiling, 64)
        reasons.append("纯销售、客服、重复运营或普通支持岗位不高于 T3")

    # A broker headquarters is valuable, but an ordinary accounting/support
    # vacancy is not a core investment, research, risk or strategy role.  This
    # keeps the platform-first rebalance from turning a logo into a T1 offer.
    ordinary_finance_role = _contains_any(
        str(job.get("title") or "").casefold(),
        {"财务", "会计", "税务", "核算", "finance accounting"},
    ) and not bool(role_tag_set.intersection(HIGH_VALUE_ROLE_TAGS))
    if ordinary_finance_role and institution.get("tier_code") in {"T0", "T0.5", "T1"}:
        ceiling = min(ceiling, 74)
        reasons.append("普通财务/会计岗位不因平台名称进入高优先级")

    # T0 is a scarcity gate: a T0 institution alone is not enough.  The
    # concrete role must use at least two parts of the composite background and
    # belong to the core product/risk/data/investment/strategy family.
    if institution.get("tier_code") == "T0" and candidate_score >= 90:
        composite = dimensions["background_utilization"] >= 7
        structured_trainee = (
            "management_trainee" in role_tag_set
            and _contains_any(role_text, {"轮岗", "导师", "定岗", "rotational", "mentorship"})
        )
        core_role = bool(role_tag_set.intersection(HIGH_VALUE_ROLE_TAGS)) or structured_trainee
        if not (composite and core_role):
            ceiling = min(ceiling, 89)
            reasons.append("未同时满足 T0 的复合背景与核心岗位稀缺门槛")
    elif candidate_score >= 90:
        ceiling = min(ceiling, 89)
        reasons.append("T0 仅保留给极少数 T0 机构的高度匹配核心岗位")

    score = min(candidate_score, ceiling)
    reason = (
        "；".join(dict.fromkeys(reasons))
        if curated_tier or score != raw_score
        else None
    )
    return score, reason, anchor_kind


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


def _score_system_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    categories = semantic_employer_categories(job)
    primary_category = _primary_category(job, categories)
    organization_category = _normalized_organization_category(job)
    industry_tags = _normalized_industry_tags(job)
    role_tags = _normalize_role_tags(job)
    days_left = _days_left(job)
    organization = _organization_assessment(job)
    institution = _institution_baseline(job, organization)

    if not _has_sufficient_role_evidence(job, role_tags):
        empty_groups: dict[str, int | None] = {key: None for key in SCORING_WEIGHTS}
        empty_dimensions: dict[str, int | None] = {key: None for key in BREAKDOWN_LIMITS}
        return {
            **job,
            "job_score": None,
            "match_score": None,
            "raw_job_score": None,
            "calibration_adjustment": None,
            "calibration_reason": None,
            "employer_score": None,
            "role_score": None,
            "career_value_score": None,
            "job_condition_score": None,
            "institution_score": institution["score"],
            "institution_tier_code": institution["tier_code"],
            "institution_reason": institution["reason"],
            "score_breakdown": dict(empty_groups),
            "dimension_scores": empty_dimensions,
            "scoring_status": "unscored_insufficient_role_data",
            "scoring_version": SCORING_VERSION,
            "scoring_factors": _scoring_factors(empty_groups, empty_groups),
            "organization_assessment": organization,
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

    dimensions, positives, negatives, fit_tags = _score_dimensions(job, profile, role_tags, organization)
    raw_score = max(0, min(100, sum(dimensions.values())))
    score, calibration_reason, anchor_kind = _calibrated_job_score(
        job, dimensions, role_tags, organization, institution,
    )
    manual_override = bool(anchor_kind)
    group_scores = _normalized_group_scores(dimensions)
    contributions = _group_contributions(dimensions)
    tier_code = tier_for_score(score)
    if anchor_kind == "first_release":
        positives.insert(0, "该岗位使用首版已核验的个人 T 级校准锚点")
        fit_tags.append("受控校准")
    elif anchor_kind == "named_example":
        positives.insert(0, "该岗位按最初规则中的具名示例进行受控校准")
        fit_tags.append("受控校准")
    elif calibration_reason:
        negatives.insert(0, calibration_reason)

    return {
        **job,
        "job_score": score,
        "match_score": score,
        "raw_job_score": raw_score,
        "calibration_adjustment": score - raw_score,
        "calibration_reason": calibration_reason,
        "employer_score": group_scores["employer_platform"],
        "role_score": group_scores["role_function"],
        "career_value_score": group_scores["career_value"],
        "job_condition_score": group_scores["job_conditions"],
        "institution_score": institution["score"],
        "institution_tier_code": institution["tier_code"],
        "institution_reason": institution["reason"],
        "score_breakdown": contributions,
        "dimension_scores": dict(dimensions),
        "scoring_status": "scored",
        "scoring_version": SCORING_VERSION,
        "scoring_factors": _scoring_factors(group_scores, contributions),
        "organization_assessment": organization,
        "positive_reasons": positives[:3] or ["岗位具备可核验的基础职业价值"],
        "negative_reasons": negatives[:2] or ["仍需结合完整 JD 与个人约束继续核对"],
        "match_reasons": positives[:3] or ["已按岗位级职业价值模型完成评分"],
        "fit_tags": list(dict.fromkeys(fit_tags)),
        "technical_hard": "技术偏硬" in fit_tags,
        "quant_barrier": "量化高门槛" in fit_tags,
        "manual_override": manual_override,
        "tier_code": tier_code,
        "days_left": days_left,
        "primary_category": primary_category,
        "organization_category": organization_category,
        "industry_tags": industry_tags,
        "role_tags": role_tags,
    }


def score_job(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    result = _score_system_job(job, profile)
    rating = resolve_source_ratings(job)
    rating["source_ratings"] = [
        {key: value for key, value in source.items() if key not in {"rating_key", "observed_at"}}
        for source in rating["source_ratings"]
    ]
    if rating["source_rating"]:
        rating["source_rating"] = {
            key: value for key, value in rating["source_rating"].items()
            if key not in {"rating_key", "observed_at"}
        }
    result.update(rating)
    result["system_tier_code"] = result["tier_code"]
    result["system_job_score"] = result["job_score"]
    if rating["rating_status"] != "applied":
        return result
    source = rating["source_rating"]
    # Do not manufacture a score from a tier, or a tier from a score. The
    # independent system result remains available with an explicit label.
    result.update({
        "tier_code": source.get("tier_code"),
        "job_score": source.get("score"),
        "match_score": source.get("score"),
        "scoring_status": "source_rated",
        "manual_override": True,
        "calibration_adjustment": None,
        "calibration_reason": source.get("reason"),
        "positive_reasons": ["保留监控来源对该具体岗位的原始评级"],
        "negative_reasons": [],
        "match_reasons": [source.get("reason") or "采用来源明确给出的岗位评级"],
        "fit_tags": [*result.get("fit_tags", []), "来源原始评级"],
    })
    return result
