"""Shared, public employer scopes and deterministic category identity lookup.

This module is intentionally data-only at import: no config, database, network,
credentials or model client. Search, ingestion and additive migrations reuse the
same maintained scopes rather than inventing another employer-ranking list.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any


PERSONAL_MONITOR_POOLS = [
    {
        "id": "state_energy_resources",
        "primary_category": "state_energy_resources",
        "name": "央企能源与资源",
        "focus": "能源、矿产、化工、核工业与国家级资源集团面向应届生的公开机会、提前批和免笔试政策",
        "employers": [
            "中国石油", "中国石化", "中国海油", "国家能源集团", "国家电网", "中国华能",
            "华电集团", "大唐集团", "国家电投", "中国核工业集团", "中国核建", "中国能建",
            "中国电建", "中国煤炭科工", "中国有色矿业", "中国黄金", "国家管网集团",
            "中国中化", "中国五矿", "中国铝业集团", "中国宝武", "鞍钢集团", "中国稀土集团",
        ],
    },
    {
        "id": "state_tech_transport",
        "primary_category": "state_tech_telecom",
        "name": "央企科技、通信与交通",
        "focus": "通信运营商、航天军工、电子科技、铁路航空与央企研究院的高信号岗位",
        "employers": [
            "中国移动", "中国电信", "中国联通", "中国铁塔", "中国电子", "中国电科",
            "中国电子科技集团", "中国航天科技", "中国航天科工", "航空工业", "中国商飞",
            "中国铁路", "国家铁路局", "中国交通建设", "中国一汽", "中国兵器工业集团",
            "中国信通院", "中国科学院", "中国船舶集团", "中国兵器装备集团", "中国邮政集团",
            "中国航空集团", "中国南方航空", "中国东方航空", "招商局集团", "中国远洋海运",
        ],
    },
    {
        "id": "tobacco_monopoly",
        "primary_category": "tobacco_monopoly",
        "name": "烟草与高等级专卖体系",
        "focus": "国家烟草专卖体系、中国烟草总公司及省级中烟工业公司的总部、技术、财务、数字化与管理类校园机会",
        "employers": [
            "国家烟草专卖局", "中国烟草总公司", "中国烟草", "中烟工业", "上海烟草集团",
            "云南中烟", "湖南中烟", "湖北中烟", "广东中烟", "浙江中烟", "江苏中烟",
            "山东中烟", "河南中烟", "安徽中烟", "四川中烟", "重庆中烟", "贵州中烟",
            "福建中烟", "陕西中烟", "广西中烟", "江西中烟", "河北中烟",
        ],
    },
    {
        "id": "policy_and_major_banks",
        "primary_category": "policy_state_banks",
        "name": "银行与政策性金融",
        "focus": "政策行、国有大行与商业银行的金融科技、风控、研究和明确发布的应届生机会",
        "employers": [
            "中国人民银行", "国家开发银行", "中国进出口银行", "中国农业发展银行",
            "工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行",
        ],
    },
    {
        "id": "securities_funds_asset",
        "primary_category": "securities_public_funds_asset_management",
        "name": "券商、公募与资管",
        "focus": "头部券商、公募基金、传统大型资产管理机构与 AMC 的研究、投行、风控、产品和金融科技岗位",
        "employers": [
            "中信证券", "中金公司", "华泰证券", "国泰海通", "中信建投", "招商证券",
            "广发证券", "申万宏源", "银河证券", "光大证券", "东方证券", "长城证券", "中信期货",
            "中信资产", "东方资产", "中国华融", "易方达", "华夏基金", "嘉实基金",
            "南方基金", "汇添富", "富国基金", "博时基金", "广发基金", "招商基金",
            "兴证全球基金", "景顺长城基金", "鹏华基金", "国投证券", "国信证券",
        ],
    },
    {
        "id": "insurance_fintech",
        "primary_category": "insurance_integrated_finance",
        "name": "保险与综合金融",
        "focus": "头部保险、再保险、银行保险和综合金融科技岗位；与政策金融分开维护",
        "employers": [
            "中国人保", "中国人寿", "中国太平", "中国再保险", "中国平安", "平安银行",
            "平安科技", "平安产险", "平安养老险", "平安理财", "太平洋保险", "新华保险",
            "泰康保险集团", "阳光保险", "友邦保险", "招商信诺", "中邮保险",
        ],
    },
    {
        "id": "internet_tech_scale",
        "primary_category": "internet_tech",
        "name": "互联网大厂与中厂",
        "focus": "互联网、AI、数据、产品、策略、风控与金融科技的应届生机会，不把城市分类页当作具体机会",
        "employers": [
            "腾讯", "阿里巴巴", "字节跳动", "百度", "拼多多", "蚂蚁集团", "美团",
            "京东", "小米", "网易", "快手", "滴滴", "携程", "华为", "科大讯飞",
            "同程旅行", "得物", "B站", "金山办公", "小红书", "BytePlus",
            "DJI", "大疆", "中芯国际", "SMIC", "联想", "荣耀", "OPPO", "vivo",
            "蔚来", "小鹏汽车", "理想汽车", "宁德时代", "比亚迪",
        ],
    },
    {
        "id": "consumer_global_consulting",
        "primary_category": "consumer_foreign_consulting",
        "name": "快消、外企与咨询",
        "focus": "快消、消费品牌、外企与战略咨询的管培、商业分析、市场和职能岗位",
        "employers": [
            "宝洁", "联合利华", "欧莱雅", "雀巢", "玛氏", "可口可乐", "百事", "耐克", "Babycare",
            "达能", "亿滋", "蒙牛", "伊利", "安踏", "阿迪达斯", "宜家", "LVMH",
            "强生", "星巴克", "麦当劳", "Kearney 科尔尼", "麦肯锡", "波士顿咨询",
            "Roland Berger", "罗兰贝格", "埃森哲", "Microsoft", "Google",
            "Amazon/AWS", "Amazon", "AWS", "Apple", "NVIDIA", "J.P. Morgan",
            "Goldman Sachs", "Morgan Stanley",
            "UBS", "Citi", "HSBC", "BlackRock",
        ],
    },
    {
        "id": "quant_private_capital",
        "primary_category": "quant_private_hedge",
        "name": "量化、私募与对冲",
        "focus": "仅保留有明确校园职位和官方投递链接的量化基金、私募证券、对冲基金和研究岗位；不把远期开放窗口当截止预警",
        "employers": ["幻方", "明汯", "衍复", "灵均", "宽德", "高瓴", "红杉中国", "Point72"],
    },
    {
        "id": "professional_services",
        "primary_category": "big_four_professional_services",
        "name": "四大与专业服务",
        "focus": "高质量专业服务机构的咨询、战略、交易、数据、人工智能、风险、审计与税务校园岗位",
        "employers": ["Deloitte", "德勤", "PwC", "普华永道", "EY", "安永", "KPMG", "毕马威"],
    },
]


EMPLOYER_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "国家烟草专卖局": ("中国烟草总公司", "中国烟草", "中烟工业"),
    "中国电子科技集团": ("中国电科",),
    "大疆": ("DJI", "大疆创新", "深圳市大疆创新科技"),
    "中芯国际": ("SMIC",),
    "B站": ("哔哩哔哩", "Bilibili"),
    "罗兰贝格": ("Roland Berger",),
    "亚马逊 / AWS": (
        "Amazon/AWS", "Amazon", "AWS", "Amazon Web Services", "亚马逊",
    ),
    "科尔尼": ("Kearney 科尔尼", "Kearney"),
    # The sidebar uses familiar short names; official notices commonly use
    # these legal/brand names.  Alias matching selects a discovery target only,
    # and never bypasses the separate official-page evidence gate.
    "工商银行": ("中国工商银行", "ICBC"),
    "农业银行": ("中国农业银行",),
    "建设银行": ("中国建设银行", "CCB"),
    "中国银行": ("Bank of China", "BOC"),
    "交通银行": ("Bank of Communications", "BOCOM"),
    "邮储银行": ("中国邮政储蓄银行", "邮政储蓄银行", "PSBC"),
    "中国移动": ("中国移动通信集团", "China Mobile"),
    "中国电信": ("中国电信集团", "China Telecom"),
    "中国联通": ("中国联合网络通信集团", "中国联合网络通信", "China Unicom"),
    "腾讯": ("腾讯科技", "腾讯计算机系统", "Tencent"),
    "Microsoft": ("微软",),
    "Google": ("谷歌",),
    "Apple": ("苹果",),
    "NVIDIA": ("英伟达",),
    "J.P. Morgan": ("JPMorgan", "摩根大通"),
    "Goldman Sachs": ("高盛",),
    "Morgan Stanley": ("摩根士丹利",),
    "UBS": ("瑞银",),
    "Citi": ("Citibank", "花旗",),
    "HSBC": ("汇丰",),
    "BlackRock": ("贝莱德",),
    "德勤": ("Deloitte",),
    "普华永道": ("PwC",),
    "安永": ("EY",),
    "毕马威": ("KPMG",),
    # Public brand/legal bilingual forms of employers ALREADY in the scope.
    # These do not add employers or merge an unnamed subsidiary into its group.
    "麦肯锡": ("McKinsey", "McKinsey & Company"),
    "埃森哲": ("Accenture",),
    "波士顿咨询": ("Boston Consulting Group", "BCG"),
    "中国能建": ("中国能源建设集团",),
    "中国电建": ("中国电力建设集团",),
    "中国电子": ("中国电子信息产业集团",),
    "中国人保": ("中国人民保险集团", "PICC"),
    "中国平安": ("中国平安保险集团", "Ping An"),
    "中国信通院": ("中国信息通信研究院", "CAICT"),
}


# Category lookup reuses the maintained search aliases above. It does not
# classify arbitrary occurrences in a sentence, infer ownership of unnamed
# subsidiaries, or replace the actual employing unit with its parent brand.
_IDENTITY_NOISE = re.compile(
    r"招聘|岗位|合作伙伴|供应商|代理商|服务商|外包|劳务|派遣|加盟|客户|对接|服务于|"
    r"\b(?:recruitment|partner|supplier|vendor|contractor|outsourcing|staffing)\b", re.I,
)
_LEGAL_SUFFIX = re.compile(
    r"(?:(?:集团)?(?:股份有限公司|有限责任公司|有限公司|股份公司)|集团|公司|"
    r"limited|ltd|incorporated|inc|corporation|corp|plc)$", re.I,
)
_REGIONS = (
    "北京", "天津", "上海", "重庆", "河北", "河南", "云南", "辽宁", "黑龙江", "湖南",
    "安徽", "山东", "新疆", "江苏", "浙江", "江西", "湖北", "广西", "甘肃", "山西",
    "内蒙古", "陕西", "吉林", "福建", "贵州", "广东", "青海", "西藏", "四川", "宁夏",
    "海南", "广州", "深圳", "杭州", "南京", "苏州", "无锡", "镇江", "南通", "成都",
    "武汉", "西安", "长沙", "合肥", "厦门", "南昌", "石家庄", "郑州", "济南", "太原",
    "沈阳", "大连", "长春", "哈尔滨", "海口", "昆明", "贵阳", "南宁", "兰州", "西宁",
    "银川", "乌鲁木齐", "阿克苏", "拉萨", "呼和浩特", "宁波", "泉州", "佛山", "东莞",
    "江门", "烟台", "青岛", "香港", "澳门", "中国", "中国大陆", "大中华区", "亚太",
)
_REGION_SUFFIX = re.compile(
    r"^(?:" + "|".join(sorted(_REGIONS, key=len, reverse=True)) + r")"
    r"(?:省|市)?(?:分公司|分行|支行|公司|分部|办事处)?(?:本部|总部)?$"
)
_FOREIGN_REGION_SUFFIXES = {
    "china", "mainlandchina", "greaterchina", "hongkong", "hongkongchina",
    "australia", "singapore", "apac", "asiapacific",
}
_NAMED_UNITS = {"总部", "集团总部", "总行", "信用卡中心", "总行信用卡中心"}
_NUMBERED_RESEARCH_UNIT = re.compile(
    r"^(?:集团)?(?:公司)?(?:第?[零〇一二三四五六七八九十百两0-9]{1,10}院)?"
    r"第?[零〇一二三四五六七八九十百两0-9]{1,10}(?:研究所|研究院|所)$"
)


def _identity_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _legal_identity_keys(value: Any) -> set[str]:
    key = _identity_key(value)
    result = {key} if key else set()
    # Two removals handle e.g. an existing '集团' brand followed by its legal
    # company form, but never remove arbitrary internal words.
    for _ in range(2):
        shorter = _LEGAL_SUFFIX.sub("", key)
        if not shorter or shorter == key:
            break
        result.add(shorter)
        key = shorter
    return result


@lru_cache(maxsize=1)
def _directory_index() -> dict[str, str]:
    categories: dict[str, set[str]] = {}
    for pool in PERSONAL_MONITOR_POOLS:
        category = str(pool["primary_category"])
        for employer in pool["employers"]:
            for key in _legal_identity_keys(employer):
                categories.setdefault(key, set()).add(category)
    for canonical, configured_aliases in EMPLOYER_ALIAS_GROUPS.items():
        aliases = tuple(dict.fromkeys((canonical, *configured_aliases)))
        matched = set().union(*(
            categories.get(key, set())
            for alias in aliases for key in _legal_identity_keys(alias)
        ))
        if len(matched) != 1:
            continue
        # Official/public feeds often print both names, such as 'HSBC 汇丰'.
        # Only already maintained aliases of the SAME employer may be joined.
        forms = (*aliases, *(f"{first} {second}" for first in aliases
                             for second in aliases if first != second))
        for alias in forms:
            for key in _legal_identity_keys(alias):
                categories.setdefault(key, set()).update(matched)
    return {key: next(iter(value)) for key, value in categories.items()
            if len(key) >= 2 and len(value) == 1}


def employer_directory_category(company: Any) -> str:
    """Exact maintained identity plus bounded legal/geographic suffixes only.

    This is a navigation category, NOT employer verification or a headquarters
    assertion. Unknown affiliates/partners remain unknown without metadata.
    """
    raw = str(company or "").strip()
    if not raw or len(raw) > 500 or _IDENTITY_NOISE.search(raw):
        return ""
    return _cached_directory_category(raw)


@lru_cache(maxsize=4096)
def _cached_directory_category(raw: str) -> str:
    index = _directory_index()
    keys = _legal_identity_keys(raw)
    direct = {index[key] for key in keys if key in index}
    if len(direct) == 1:
        return next(iter(direct))
    if len(direct) > 1:
        return ""
    matched: set[str] = set()
    for key in keys:
        for root, category in index.items():
            if not key.startswith(root):
                continue
            suffix = key[len(root):]
            suffix = re.sub(r"^(?:股份有限公司|有限责任公司|有限公司|集团公司)", "", suffix)
            if (suffix in _NAMED_UNITS or suffix in _FOREIGN_REGION_SUFFIXES
                    or _REGION_SUFFIX.fullmatch(suffix)
                    or _NUMBERED_RESEARCH_UNIT.fullmatch(suffix)):
                matched.add(category)
    return next(iter(matched)) if len(matched) == 1 else ""


def explicit_employer_type_category(value: Any) -> str:
    """Read the employer TYPE, not a role's AI/consulting/quant keywords."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    if not text or len(text) > 200:
        return ""
    if re.search(r"烟草|中烟|专卖体系", text):
        return "tobacco_monopoly"
    if re.search(r"私募|对冲基金|量化私募|\b(?:private fund|hedge fund|systematic fund)\b", text):
        return "quant_private_hedge"
    if re.search(r"公募|基金管理|资产管理|资管|银行理财|证券公司|证券机构|券商|期货公司|"
                 r"\b(?:asset management|public fund|mutual fund|securities|brokerage)\b", text):
        return "securities_public_funds_asset_management"
    if re.search(r"保险|再保险|综合金融|\b(?:insurance|reinsurance)\b", text):
        return "insurance_integrated_finance"
    if re.search(r"四大|专业服务|会计师事务所|\b(?:big four|professional services|accounting firm)\b", text):
        return "big_four_professional_services"
    if (re.search(r"银行|城商行|农商行|政策性金融|\b(?:bank|banks|banking)\b", text)
            and not re.search(r"非银行|银行业服务|\bnon[ -]?bank", text)):
        return "policy_state_banks"
    if re.search(r"央企科技|央企通信|央企交通|央国企科技|国有通信|通信运营商", text):
        return "state_tech_telecom"
    if re.search(r"央企能源|央企资源|能源央企|资源央企|国有能源|国有资源", text):
        return "state_energy_resources"
    if re.search(r"外资|外商|外企|外籍企业|快消|消费品|\b(?:foreign|fmcg|consumer)\b", text):
        return "consumer_foreign_consulting"
    if re.search(r"互联网|科技企业|科技集团|科技制造|科技平台|\b(?:internet|technology company)\b", text):
        return "internet_tech"
    return ""


def employer_category_override(item: dict[str, Any]) -> str:
    """Resolve maintained identities, or fill an unclassified employer type.

    A directory identity can repair an old broad metadata-derived label. A
    generic type must not replace a producer's more specific organization or
    explicit primary category: a banking group can recruit through a fund
    subsidiary, and shared metadata is not proof that the subsidiary is a bank.
    """
    identity = employer_directory_category(item.get("company"))
    if identity:
        return identity
    if item.get("primary_category") or item.get("organization_category"):
        return ""
    return explicit_employer_type_category(item.get("employer_type"))
