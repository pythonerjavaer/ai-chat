import hashlib
import json
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from openai import OpenAI

from .config import settings
from .live_sources import PERSONAL_MONITOR_POOLS, PRIORITY_EMPLOYERS
from .recruitment_directory import EMPLOYER_ALIAS_GROUPS
from .future_radar.public_discovery import (
    OfficialDiscoveryCancelled, check_discovery_cancellation, discover_official_job_pages,
)
from .recruitment_watch import (
    WatchFetchError,
    fetch_watch_page,
    normalize_public_https_urls,
)


logger = logging.getLogger(__name__)
WEB_SEARCH_SOURCE = "OpenAI 网页搜索"
WEB_SEARCH_STATE_KEY = "recruitment_web_search"
# One broad category prompt could never cover the 8–40 employers displayed in
# that category: its structured output was capped at ten rows.  Deep discovery
# now gives every normalized employer its own required hosted-search request.
# A model's multi-employer checklist is not evidence that it searched them all.
# Keep the batch type for compatibility, but a batch always has exactly one
# employer; the outer executor is responsible for bounded parallelism.
EMPLOYERS_PER_SEARCH_BATCH = 1
SEARCH_MAX_OUTPUT_TOKENS = 16_000
BLOCKED_DISCOVERY_HOSTS = {
    "baidu.com",
    "bing.com",
    "google.com",
    "linkedin.com",
    "xiaohongshu.com",
    "weibo.com",
    "zhihu.com",
}

# Search engines and social sites are not evidence, even when they happen to
# contain the claimed title.  A small set of recruitment-only subdomains is
# allowed before the parent-domain block is applied.  In particular, Baidu's
# public careers site lives below the otherwise blocked ``baidu.com`` root.
EXPLICIT_RECRUITMENT_HOSTS = {
    "talent.baidu.com",
}

# A discovery result is only promoted when its final host is known to belong
# to the claimed employer or to an established multi-tenant ATS.  This mapping
# is intentionally conservative: an unknown host remains a useful, pending
# candidate and can be added after its ownership has been checked.
OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER = {
    "百度": ("talent.baidu.com",),
    "拼多多": ("pddglobalhr.com",),
    "大疆": ("careers.dji.com",),
    "荣耀": ("honor.com",),
    "中国电信": ("chinatelecom.com.cn",),
    "海尔": ("haier.net",),
    "小米": ("xiaomi.com",),
    "腾讯": ("join.qq.com", "careers.tencent.com"),
    "阿里巴巴": ("talent.alibaba.com", "job.alibaba.com"),
    "字节跳动": ("jobs.bytedance.com",),
    "美团": ("zhaopin.meituan.com",),
    "京东": ("campus.jd.com", "zhaopin.jd.com"),
    "华为": ("career.huawei.com",),
    "网易": ("campus.163.com", "hr.163.com"),
    "快手": ("campus.kuaishou.cn", "zhaopin.kuaishou.cn"),
    "滴滴": ("talent.didiglobal.com",),
    "携程": ("careers.trip.com", "job.ctrip.com"),
    "科大讯飞": ("career.iflytek.com",),
    "哔哩哔哩": ("job.bilibili.com",),
    "B站": ("job.bilibili.com",),
    "OPPO": ("career.oppo.com",),
    "vivo": ("hr.vivo.com",),
    "蔚来": ("careers.nio.com",),
    "小鹏": ("jobs.xiaopeng.com",),
    "理想汽车": ("campus.lixiang.com",),
    "比亚迪": ("hr.byd.com",),
    "中国银行": ("bankofchina.com",),
    "工商银行": ("icbc.com.cn",),
    "农业银行": ("abchina.com",),
    "建设银行": ("ccb.com",),
    "交通银行": ("bankcomm.com",),
    "邮储银行": ("psbc.com",),
    "国家开发银行": ("cdb.com.cn",),
    "中国进出口银行": ("eximbank.gov.cn",),
    "中国农业发展银行": ("adbc.com.cn",),
}

KNOWN_AUTHORIZED_ATS_DOMAINS = {
    "mokahr.com",
    "hotjob.cn",
    "hotjob.net",
    "beisen.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "successfactors.com",
    "successfactors.eu",
    "oraclecloud.com",
    "jobs.feishu.cn",
    "zhiye.com",
}

EMPLOYER_TYPE_BY_POOL = {
    "state_energy_resources": "央国企",
    "state_tech_transport": "央国企科技",
    "tobacco_monopoly": "烟草/专卖",
    "policy_and_major_banks": "银行/金融",
    "securities_funds_asset": "券商/公募/资管",
    "insurance_fintech": "保险/综合金融",
    "internet_tech_scale": "互联网企业",
    "consumer_global_consulting": "外企/咨询",
    "quant_private_capital": "量化/私募/对冲",
    "professional_services": "四大/专业服务",
}

# These institutions publish campus and affiliated-unit recruitment under
# specific official notices. A management-trainee label may be real, but it
# must not be presented as an official fact without the exact source wording.
MANAGEMENT_TRAINEE_REVIEW_EMPLOYERS = {
    "中国人民银行",
    "人行",
    "中国农业发展银行",
    "农发行",
}

# Explicit aliases only.  Substring heuristics are unsafe here: for example,
# ``中国电子`` and ``中国电子科技集团`` are different employers even though one
# name contains the other.  These groups collapse only obvious bilingual,
# brand, or legal-name duplicates already present in the left-hand scope.


@dataclass(frozen=True)
class WebRecruitmentSearchResult:
    jobs: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int
    model: str
    failed_pools: tuple[str, ...] = ()
    target_employers: tuple[str, ...] = ()
    searched_employers: tuple[str, ...] = ()
    employers_with_candidates: tuple[str, ...] = ()
    failed_employers: tuple[str, ...] = ()
    search_batches: int = 0
    failed_batches: tuple[str, ...] = ()
    # Separate hosted-search execution from actual official-list coverage.
    # A successful model request is not proof that every ATS page was read.
    official_discovery: tuple[dict[str, Any], ...] = ()

    @property
    def target_count(self) -> int:
        return len(set(self.target_employers))

    @property
    def searched_count(self) -> int:
        return len(set(self.searched_employers))

    @property
    def failed_count(self) -> int:
        return len(set(self.failed_employers))

    @property
    def batch_count(self) -> int:
        return self.search_batches

    @property
    def coverage_percent(self) -> float:
        if not self.target_count:
            return 100.0
        return round(
            self.searched_count / self.target_count * 100,
            2,
        )


@dataclass(frozen=True)
class EmployerSearchTarget:
    """One logical employer after conservative alias normalization."""

    id: str
    canonical_name: str
    aliases: tuple[str, ...]
    pool_id: str
    primary_category: str
    pool_name: str
    focus: str


@dataclass(frozen=True)
class EmployerSearchBatch:
    """A bounded hosted-search request whose targets are all explicit."""

    id: str
    pool: dict[str, Any]
    targets: tuple[EmployerSearchTarget, ...]


@dataclass(frozen=True)
class CandidatePageEvidence:
    """Deterministic evidence collected from the supplied original page."""

    readable: bool
    # Backward-compatible promotion gate used by both radar adapters.  The
    # inspector only sets it when every evidence dimension below is true; it no
    # longer means that a title-shaped string merely appeared somewhere.
    title_confirmed: bool
    closed: bool = False
    page_text: str = ""
    employer_confirmed: bool = False
    domain_confirmed: bool = False
    cohort_confirmed: bool = False
    open_confirmed: bool = False
    identity_confirmed: bool = False
    final_url: str = ""


def _employer_alias_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).casefold())


def build_employer_search_targets(
    pools: list[dict[str, Any]] | None = None,
) -> tuple[EmployerSearchTarget, ...]:
    """Expand every configured list entry into one normalized search target.

    Every raw entry is assigned to exactly one target.  Only explicit aliases
    are merged, so similarly named but legally distinct employers are not
    accidentally collapsed.
    """
    alias_lookup: dict[str, tuple[str, tuple[str, ...]]] = {}
    for canonical_name, configured_aliases in EMPLOYER_ALIAS_GROUPS.items():
        aliases = tuple(dict.fromkeys((canonical_name, *configured_aliases)))
        for alias in aliases:
            alias_lookup[_employer_alias_key(alias)] = (canonical_name, aliases)

    targets: list[EmployerSearchTarget] = []
    for pool in pools if pools is not None else PERSONAL_MONITOR_POOLS:
        grouped_aliases: dict[str, list[str]] = {}
        canonical_names: dict[str, str] = {}
        for raw_value in pool.get("employers", []):
            raw_name = re.sub(r"\s+", " ", str(raw_value)).strip()
            if not raw_name:
                continue
            configured = alias_lookup.get(_employer_alias_key(raw_name))
            canonical_name, known_aliases = configured or (raw_name, (raw_name,))
            canonical_key = _employer_alias_key(canonical_name)
            canonical_names.setdefault(canonical_key, canonical_name)
            bucket = grouped_aliases.setdefault(canonical_key, [])
            for alias in (*known_aliases, raw_name):
                if alias and alias not in bucket:
                    bucket.append(alias)

        for canonical_key, aliases in grouped_aliases.items():
            canonical_name = canonical_names[canonical_key]
            stable_suffix = hashlib.sha256(
                f"{pool['id']}\0{canonical_key}".encode("utf-8")
            ).hexdigest()[:12]
            targets.append(EmployerSearchTarget(
                id=f"{pool['id']}:{stable_suffix}",
                canonical_name=canonical_name,
                aliases=tuple(aliases),
                pool_id=str(pool["id"]),
                primary_category=str(pool.get("primary_category") or pool["id"]),
                pool_name=str(pool.get("name") or pool["id"]),
                focus=str(pool.get("focus") or ""),
            ))
    return tuple(targets)


def build_employer_search_batches(
    pools: list[dict[str, Any]] | None = None,
    *,
    batch_size: int | None = None,
) -> tuple[EmployerSearchBatch, ...]:
    """Give every target its own request, regardless of the legacy batch size."""
    del batch_size
    selected_pools = list(pools if pools is not None else PERSONAL_MONITOR_POOLS)
    targets = build_employer_search_targets(selected_pools)
    targets_by_pool: dict[str, list[EmployerSearchTarget]] = {}
    for target in targets:
        targets_by_pool.setdefault(target.pool_id, []).append(target)
    effective_batch_size = EMPLOYERS_PER_SEARCH_BATCH
    batches: list[EmployerSearchBatch] = []
    for pool in selected_pools:
        pool_targets = targets_by_pool.get(str(pool["id"]), [])
        for offset in range(0, len(pool_targets), effective_batch_size):
            chunk = tuple(pool_targets[offset: offset + effective_batch_size])
            batches.append(EmployerSearchBatch(
                id=f"{pool['id']}:{offset // effective_batch_size + 1}",
                pool=dict(pool),
                targets=chunk,
            ))
    assigned_ids = [target.id for batch in batches for target in batch.targets]
    expected_ids = [target.id for target in targets]
    if len(assigned_ids) != len(set(assigned_ids)) or set(assigned_ids) != set(expected_ids):
        raise RuntimeError("Employer search batching did not assign every target exactly once.")
    return tuple(batches)


def _search_result_schema(batch: EmployerSearchBatch) -> dict[str, Any]:
    target_ids = [target.id for target in batch.targets]
    return {
        "type": "object",
        "properties": {
            "checked_employers": {
                "type": "array",
                "minItems": len(target_ids),
                "maxItems": len(target_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "enum": target_ids},
                        "status": {
                            "type": "string",
                            "enum": [
                                "open_jobs_found",
                                "no_current_opening",
                                "official_page_not_found",
                            ],
                        },
                    },
                    "required": ["target_id", "status"],
                    "additionalProperties": False,
                },
            },
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "enum": target_ids},
                        "company": {"type": "string"},
                        "title": {"type": "string"},
                        "city": {"type": "string"},
                        "industry": {"type": "string"},
                        "official_url": {"type": "string"},
                        "opening_date": {"type": ["string", "null"]},
                        "closing_date": {"type": ["string", "null"]},
                        "requirements": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": [
                        "target_id",
                        "company",
                        "title",
                        "city",
                        "industry",
                        "official_url",
                        "opening_date",
                        "closing_date",
                        "requirements",
                        "category",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["checked_employers", "jobs"],
        "additionalProperties": False,
    }


def _search_prompt(batch: EmployerSearchBatch) -> str:
    today = date.today().isoformat()
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    pool = batch.pool
    category = EMPLOYER_TYPE_BY_POOL[pool["id"]]
    target_lines = []
    for target in batch.targets:
        aliases = " / ".join(target.aliases)
        target_lines.append(
            f"- target_id={target.id}; 雇主={target.canonical_name}; 别名={aliases}"
        )
    targets = "\n".join(target_lines)
    return f"""
今天是 {today}。这是“{pool['name']}”公司级覆盖批次 {batch.id}。
必须将下列每一个 target_id 作为独立搜索目标，分别搜索它当前仍可申请的校园招聘、应届生、Graduate、管培生、提前批或留学生招聘岗位。
目标毕业届别是 {target_year} 届。搜索词应包含 {target_year}、校园招聘或 graduate；不要只检索当前日历年份的旧届招聘。混合届别项目只有明确接收 {target_year} 届时才能返回。

本批次目标：
{targets}

category 固定填写：{category}

要求：
1. 逐个搜索全部 {len(batch.targets)} 个 target_id，不得遗漏。每个 target_id 在 checked_employers 中恰好返回一次；没有开放岗位也要返回 no_current_opening，不得用其他雇主补位。
2. 只返回当前开放且能直接投递或查看原公告的岗位，排除社招、实习、城市招聘导航页、转载汇总页和已过期岗位。
3. official_url 必须是企业招聘官网或企业授权 ATS 的直接 HTTPS 链接，不得填搜索结果页、公众号转载、社交媒体或臆造链接。
4. opening_date / closing_date 只有原文明确写明时才填写 YYYY-MM-DD，否则为 null；不得把发布日期当截止日期。
5. city 未公告时写“地点待公告确认”。requirements 简洁记录毕业年份、学历、专业、语言或笔试门槛；无法确认时明确写“待官方原文核对”。
6. 中国人民银行和中国农业发展银行只能使用官方原文中的实际岗位名称；不得自行把笼统校园招聘或所属单位招聘改写成“管培生”。如果原文确实使用该称谓，保留原称并标记“待官方核验”。
7. jobs 中的 target_id 必须对应该岗位的目标雇主。返回本次实际搜索发现的全部符合条件的岗位，不要只摘选四条；不同岗位可以共用同一个官方招聘项目链接。保留官方公司名称及分支机构名称，不要为了匹配简称改写雇主。
8. 搜索完成不等于该企业所有岗位已被穷尽。不得声称已覆盖企业所有岗位；没有找到有效结果时如实返回空 jobs，不要猜测或补齐。
9. title 保留原公告中的具体岗位名称，不自行拼接年份或“校园招聘”；requirements 中保留原文证实的适用毕业届别，不能依据今天的年份推断。
10. “{target_year}届是否接收待确认”“如果原文面向{target_year}届才可投递”是条件或疑问，不是本届招聘证据，不能返回为当前开放岗位。只有旧届公告时如实返回无当前结果；原文只发布校招项目时保留项目原称，不自行虚构具体岗位。
""".strip()


def _date_or_none(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _priority_employer(company: str, employers: set[str] | None = None) -> str | None:
    normalized = company.strip().lower()
    matches = [
        employer
        for employer in (employers or PRIORITY_EMPLOYERS)
        if employer in normalized or normalized in employer
    ]
    return max(matches, key=len) if matches else None


_CHINESE_LEGAL_SUFFIXES = (
    "集团股份有限公司", "股份有限公司", "集团有限公司",
    "有限责任公司", "有限公司", "集团", "总公司", "公司",
)
_BRANCH_LOCATIONS = (
    "北京", "上海", "天津", "重庆", "广东", "广州", "深圳", "浙江", "杭州",
    "江苏", "南京", "苏州", "无锡", "福建", "福州", "厦门", "山东", "济南",
    "青岛", "四川", "成都", "湖北", "武汉", "湖南", "长沙", "河南", "郑州",
    "河北", "石家庄", "山西", "太原", "陕西", "西安", "安徽", "合肥",
    "江西", "南昌", "辽宁", "沈阳", "大连", "吉林", "长春", "黑龙江", "哈尔滨",
    "云南", "昆明", "贵州", "贵阳", "海南", "海口", "广西", "南宁",
    "甘肃", "兰州", "青海", "西宁", "宁夏", "银川", "新疆", "乌鲁木齐",
    "西藏", "拉萨", "内蒙古", "呼和浩特", "香港", "澳门", "台湾", "新加坡",
)
_BRANCH_LOCATION_PATTERN = (
    r"(?:(?:" + "|".join(sorted(_BRANCH_LOCATIONS, key=len, reverse=True))
    + r")(?:省|市|(?:壮族|回族|维吾尔)?自治区|特别行政区)?){1,2}"
)
_BRANCH_UNIT_PATTERN = (
    r"(?:总行|总部|分行|支行|分公司|营业部|办事处|代表处|研究院|研究所|设计院|分院|分所|"
    r"研发中心|开发中心|技术中心|数据中心|研究中心|分局|分厂|供电局|供电公司|发电厂|电厂)"
)


def _without_legal_suffix(value: str) -> str:
    for suffix in _CHINESE_LEGAL_SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _matches_chinese_company_or_branch(company_key: str, alias_key: str) -> bool:
    company_base = _without_legal_suffix(company_key)
    alias_base = _without_legal_suffix(alias_key)
    if not alias_base:
        return False
    # Notices may put the registered city before a known brand/legal name,
    # e.g. 深圳市腾讯计算机系统有限公司. Do not strip arbitrary leading text.
    company_forms = {company_base}
    location_prefix = re.match(_BRANCH_LOCATION_PATTERN, company_base)
    if location_prefix:
        company_forms.add(company_base[location_prefix.end():])
    for form in company_forms:
        if form == alias_base:
            return True
        if not form.startswith(alias_base):
            continue
        remainder = form[len(alias_base):]
        # 中国银行股份有限公司上海市分行 retains the parent legal suffix
        # in the middle, unlike a standalone 中国银行股份有限公司 notice.
        for suffix in _CHINESE_LEGAL_SUFFIXES:
            if remainder.startswith(suffix):
                remainder = remainder[len(suffix):]
                break
        if re.fullmatch(
            rf"{_BRANCH_LOCATION_PATTERN}(?:{_BRANCH_UNIT_PATTERN})?", remainder
        ):
            return True
        # For unlisted cities, require an explicit organizational-unit suffix
        # and a sufficiently specific parent name. Never use broad contains()
        # matching (中国电子 is not 中国电子科技集团; EY is not Kearney).
        if len(alias_base) >= 3 and re.fullmatch(
            rf"[\u4e00-\u9fff0-9]{{0,24}}{_BRANCH_UNIT_PATTERN}", remainder
        ):
            return True
    return False


def _company_matches_target(company: str, target: EmployerSearchTarget) -> bool:
    company_raw = re.sub(r"\s+", " ", str(company)).strip().casefold()
    company_key = _employer_alias_key(company_raw)
    if not company_key:
        return False
    for alias in target.aliases:
        alias_raw = re.sub(r"\s+", " ", alias).strip().casefold()
        alias_key = _employer_alias_key(alias_raw)
        if not alias_key:
            continue
        if company_key == alias_key:
            return True
        if re.search(r"[\u4e00-\u9fff]", alias_raw):
            if _matches_chinese_company_or_branch(company_key, alias_key):
                return True
            continue
        # Short Latin aliases such as EY must be complete tokens; naive
        # substring matching would classify Kearney as EY.
        escaped = re.escape(alias_raw).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", company_raw):
            return True
    return False


def _target_for_company(
    company: str,
    targets: tuple[EmployerSearchTarget, ...],
) -> EmployerSearchTarget | None:
    matches = [target for target in targets if _company_matches_target(company, target)]
    if not matches:
        return None
    # Prefer the most specific configured alias if two legal names overlap.
    return max(
        matches,
        key=lambda target: max(
            (len(_employer_alias_key(alias)) for alias in target.aliases),
            default=0,
        ),
    )


def _needs_management_trainee_review(company: str, title: str) -> bool:
    normalized_company = re.sub(r"\s+", "", company).casefold()
    normalized_title = re.sub(r"\s+", "", title).casefold()
    return (
        ("管培" in normalized_title or "管理培训生" in normalized_title)
        and any(
            employer in normalized_company
            for employer in MANAGEMENT_TRAINEE_REVIEW_EMPLOYERS
        )
    )


def _safe_official_url(value: str) -> str | None:
    try:
        display_url, _ = normalize_public_https_urls(value, resolve_dns=False)
    except WatchFetchError:
        return None
    hostname = (urllib.parse.urlsplit(display_url).hostname or "").lower()
    if not hostname:
        return None
    if hostname in EXPLICIT_RECRUITMENT_HOSTS:
        return display_url
    if any(hostname == blocked or hostname.endswith(f".{blocked}") for blocked in BLOCKED_DISCOVERY_HOSTS):
        return None
    return display_url


def _evidence_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


_CLOSED_PAGE_PATTERN = re.compile(
    r"(?:已截止|申请已结束|报名已结束|网申已结束|投递已结束|职位已关闭|岗位已关闭|"
    r"申请通道已关闭|不再接受申请|职位已下线|job\s+no\s+longer\s+available|"
    r"\bclosed\b|\bexpired\b|no\s+longer\s+accepting)",
    re.IGNORECASE,
)

_UNRESOLVED_ATS_FIELD_PATTERN = re.compile(
    r"\{\{[^{}\r\n]{0,100}(?:job|position|requisition|error)[^{}\r\n]{0,100}\}\}",
    re.IGNORECASE,
)

_OPEN_PAGE_PATTERN = re.compile(
    r"(?:立即(?:申请|投递|报名)|现在申请|申请职位|投递简历|我要申请|"
    r"申请入口|投递入口|网申入口|报名入口|开放(?:申请|投递|报名)|"
    r"(?:校园招聘|校招).{0,16}(?:正式)?(?:启动|开启|开放)|"
    r"(?:启动|开启|开放).{0,16}(?:校园招聘|校招)|"
    r"\bapply\s+now\b|\bapply\s+for\b|\bsubmit\s+(?:an?\s+)?application\b|"
    r"\bopen\s+for\s+applications?\b|\baccepting\s+applications?\b)",
    re.IGNORECASE,
)

_CAMPUS_PAGE_PATTERN = re.compile(
    r"(?:校园招聘|校招|应届(?:生|毕业生)?|毕业生|管培生|提前批|"
    r"(?:20\d{2}|\d{2})\s*届|"
    r"\bcampus\b|\bgraduate(?:s|\s+program(?:me)?)?\b)",
    re.IGNORECASE,
)

_OPENING_DATE_LABEL = (
    r"(?:开放|启动|开始|起始|申请开始|投递开始|网申开始|"
    r"报名开始|开放申请|开放投递|开放报名|"
    r"applications?\s+open(?:ing)?|opening\s+date|starts?\s+on)"
)
_CLOSING_DATE_LABEL = (
    r"(?:申请截止|投递截止|网申截止|报名截止|截止日期|截止时间|"
    r"截止|截至|application\s+deadline|closing\s+date|applications?\s+close|deadline)"
)


def _date_pattern(value: date) -> str:
    year = str(value.year)
    month = str(value.month)
    day = str(value.day)
    return (
        rf"(?:{year}\s*[-/.]\s*0?{month}\s*[-/.]\s*0?{day}"
        rf"|{year}\s*年\s*0?{month}\s*月\s*0?{day}\s*日?)"
    )


def _semantic_date_appears_in_page(
    page_text: str,
    iso_date: str | None,
    *,
    semantic: str,
) -> bool:
    """Match an application date only next to a label with the same meaning.

    Publication dates, footer dates and unrelated event dates are deliberately
    ignored.  ``semantic='application'`` is a conservative compatibility mode
    for callers that do not know whether a value is an opening or closing date.
    """
    if not iso_date:
        return False
    try:
        value = date.fromisoformat(iso_date)
    except ValueError:
        return False
    labels_by_semantic = {
        "opening": _OPENING_DATE_LABEL,
        "closing": _CLOSING_DATE_LABEL,
        "application": rf"(?:{_OPENING_DATE_LABEL}|{_CLOSING_DATE_LABEL})",
    }
    labels = labels_by_semantic.get(semantic)
    if labels is None:
        raise ValueError("semantic must be 'opening', 'closing', or 'application'.")
    normalized = re.sub(r"\s+", " ", str(page_text or "")).casefold()
    date_expression = _date_pattern(value)
    return bool(
        re.search(rf"{labels}.{{0,32}}?{date_expression}", normalized, re.IGNORECASE)
        or re.search(rf"{date_expression}.{{0,24}}?{labels}", normalized, re.IGNORECASE)
    )


def _date_appears_in_page(page_text: str, iso_date: str | None) -> bool:
    return _semantic_date_appears_in_page(
        page_text, iso_date, semantic="application"
    )


def _targets_current_graduate_cohort(value: str) -> bool:
    today = date.today()
    target_year = today.year + 1 if today.month >= 6 else today.year
    short_year = str(target_year)[-2:]
    normalized = re.sub(r"\s+", "", value.casefold())
    return str(target_year) in normalized or f"{short_year}届" in normalized


def _hostname_matches(hostname: str, allowed: str) -> bool:
    normalized_host = hostname.strip(".").casefold()
    normalized_allowed = allowed.strip(".").casefold()
    return (
        normalized_host == normalized_allowed
        or normalized_host.endswith(f".{normalized_allowed}")
    )


def _company_evidence_aliases(company: str) -> set[str]:
    """Return conservative employer names suitable for page-body evidence."""
    raw = re.sub(r"\s+", " ", str(company or "")).strip()
    if not raw:
        return set()
    candidates = {raw}
    candidates.update(re.findall(r"[A-Za-z][A-Za-z0-9.&+\-]{2,}|[一-鿿]{2,}", raw))
    for suffix in (
        "集团股份有限公司", "股份有限公司", "集团有限公司",
        "有限责任公司", "有限公司", "集团", "公司",
    ):
        if raw.endswith(suffix):
            candidates.add(raw[: -len(suffix)])
    aliases: set[str] = set()
    for candidate in candidates:
        key = _evidence_key(candidate)
        if len(key) >= 2 and key not in {"集团", "公司", "bank", "group"}:
            aliases.add(key)
    return aliases


def _official_domain_confirmed(
    company: str,
    url: str,
    aliases: tuple[str, ...] = (),
) -> bool:
    hostname = (urllib.parse.urlsplit(url).hostname or "").casefold()
    if not hostname:
        return False
    if any(_hostname_matches(hostname, domain) for domain in KNOWN_AUTHORIZED_ATS_DOMAINS):
        return True
    company_keys = {
        key for key in (_evidence_key(value) for value in (company, *aliases)) if key
    }
    for employer, domains in OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER.items():
        employer_key = _evidence_key(employer)
        if not employer_key or not any(
            employer_key in company_key or company_key in employer_key
            for company_key in company_keys
        ):
            continue
        if any(_hostname_matches(hostname, domain) for domain in domains):
            return True
    return False


def _is_campaign_title(title: str, company: str) -> bool:
    """Recognize a campaign row without treating a role as a campaign."""
    residual = _evidence_key(title)
    for alias in _company_evidence_aliases(company):
        residual = residual.replace(alias, "")
    residual = re.sub(r"20\d{2}|年|届", "", residual)
    for marker in (
        "全球校园招聘", "校园招聘", "秋季招聘", "春季招聘", "校招",
        "应届生招聘", "graduateprogram", "campusrecruitment",
        "招聘公告", "招聘启事", "正式启动", "启动", "开启", "计划",
    ):
        residual = residual.replace(_evidence_key(marker), "")
    return not residual


def _evaluate_official_candidate_page(
    job: dict[str, Any], page_text: str, final_url: str
) -> CandidatePageEvidence:
    """Evaluate already-fetched official-page evidence with one shared gate."""
    page_text = str(page_text or "")
    page_key = _evidence_key(page_text)
    final_url = str(final_url or job.get("url", ""))
    company = str(job.get("company", ""))
    configured_aliases = tuple(
        str(value) for value in job.get("_employer_aliases", []) if value
    )
    company_aliases = _company_evidence_aliases(company)
    for alias in configured_aliases:
        company_aliases.update(_company_evidence_aliases(alias))
    employer_confirmed = bool(
        page_key and any(alias in page_key for alias in company_aliases)
    )
    domain_confirmed = _official_domain_confirmed(
        company, final_url, configured_aliases
    )
    cohort_confirmed = bool(
        _CAMPUS_PAGE_PATTERN.search(page_text)
        and _targets_current_graduate_cohort(page_text)
    )
    title_key = _evidence_key(str(job.get("title", "")))
    exact_title_confirmed = bool(title_key and title_key in page_key)
    if not exact_title_confirmed and _UNRESOLVED_ATS_FIELD_PATTERN.search(page_text):
        # Angular/ATS shells contain both the job view and an unrendered error
        # branch (for example {{jobsHeading}} / {{ErrorMessageJobTitle}} plus
        # "posting expired"). Without a resolved candidate title, that branch
        # is not evidence that this job is closed, open, or even loaded.
        return CandidatePageEvidence(
            readable=False,
            title_confirmed=False,
            page_text=page_text,
            employer_confirmed=employer_confirmed,
            domain_confirmed=domain_confirmed,
            final_url=final_url,
        )
    campaign_confirmed = bool(
        _is_campaign_title(str(job.get("title", "")), company)
        and _CAMPUS_PAGE_PATTERN.search(page_text)
    )
    identity_confirmed = exact_title_confirmed or campaign_confirmed
    closed = bool(_CLOSED_PAGE_PATTERN.search(page_text))
    future_closing_confirmed = bool(
        job.get("closing_date")
        and str(job["closing_date"]) > date.today().isoformat()
        and _semantic_date_appears_in_page(
            page_text, str(job["closing_date"]), semantic="closing"
        )
    )
    open_confirmed = bool(
        not closed
        and (_OPEN_PAGE_PATTERN.search(page_text) or future_closing_confirmed)
    )
    verified = bool(
        page_key
        and domain_confirmed
        and employer_confirmed
        and cohort_confirmed
        and open_confirmed
        and identity_confirmed
    )
    return CandidatePageEvidence(
        readable=bool(page_key),
        title_confirmed=verified,
        closed=closed,
        page_text=page_text,
        employer_confirmed=employer_confirmed,
        domain_confirmed=domain_confirmed,
        cohort_confirmed=cohort_confirmed,
        open_confirmed=open_confirmed,
        identity_confirmed=identity_confirmed,
        final_url=final_url,
    )


def _inspect_official_candidate_page(job: dict[str, Any]) -> CandidatePageEvidence:
    """Fetch and attest one candidate without trusting model assertions.

    Reachability alone preserves a discovery candidate.  Promotion requires a
    known employer/ATS domain, employer identity, current graduate cohort,
    exact role (or an explicitly generic campaign identity), and positive
    evidence that applications are open.
    """
    try:
        result = fetch_watch_page(
            job["url"],
            (),
            timeout_seconds=6,
            max_bytes=500_000,
        )
    except (OSError, ValueError, WatchFetchError):
        return CandidatePageEvidence(readable=False, title_confirmed=False)
    return _evaluate_official_candidate_page(
        job,
        str(result.text or ""),
        str(getattr(result, "final_url", "") or job.get("url", "")),
    )


def _candidate_cohort_is_unconfirmed(text: str) -> bool:
    """Do not mistake an instruction or an uncertain new-cohort claim for evidence."""
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    cohort = rf"(?:{target_year}|{str(target_year)[-2:]})\s*届?"
    normalized = re.sub(r"\s+", "", text.casefold())
    return bool(re.search(
        rf"(?:{cohort}.{{0,14}}(?:是否(?:接收|接受|招收|面向)|尚不(?:确定|明确)|未(?:确认|明确)|待(?:核对|核实|确认))"
        rf"|(?:未(?:明确|确认)|不(?:接受|接收|面向)|不含|排除|是否(?:接收|接受|招收|面向)).{{0,12}}{cohort}"
        rf"|(?:若|如果|仅当|只有).{{0,24}}{cohort}.{{0,20}}(?:方可|才能|才可|时)"
        rf"|原文为.{cohort}.{{0,30}}(?:方可|才可|才能))",
        normalized,
        re.IGNORECASE,
    ))


def _normalize_job_with_reason(
    item: dict[str, Any],
    pool: dict[str, Any] | None = None,
    target: EmployerSearchTarget | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Normalize public search fields and return a stable, non-sensitive decision."""
    if not isinstance(item, dict):
        return None, "invalid_candidate"
    company = str(item.get("company", "")).strip()[:120]
    if not company:
        return None, "company_missing"
    if target is not None and not _company_matches_target(company, target):
        return None, "company_alias_mismatch"
    if pool is None:
        pool = next(
            (
                candidate
                for candidate in PERSONAL_MONITOR_POOLS
                if _priority_employer(
                    company,
                    {str(value).casefold() for value in candidate.get("employers", [])},
                )
            ),
            None,
        )
    if pool is None:
        return None, "employer_outside_scope"
    if target is None:
        pool_employers = {str(value).casefold() for value in pool.get("employers", [])}
        employer_key = _priority_employer(company, pool_employers)
        if not employer_key:
            return None, "employer_outside_scope"
    title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()[:240]
    needs_management_review = _needs_management_trainee_review(company, title)
    campus_text = f"{title} {item.get('requirements', '')}".lower()
    if not title:
        return None, "title_missing"
    if re.search(r"实习|\bintern(?:ship)?\b", title, re.IGNORECASE):
        return None, "explicit_internship"
    if re.search(
        r"(?:(?:仅限|只招|仅招|面向|限定)非应届(?:生|毕业生)?|仅限社会招聘|社招岗位|"
        r"(?:本|该)(?:岗位|职位).{0,8}(?:社会招聘|社招)|"
        r"不(?:接收|接受|招收|招录|面向)(?:本届|当前)?(?:应届|毕业生)|"
        r"experienced\s+(?:hires?|professionals?)\s+only)",
        campus_text,
        re.IGNORECASE,
    ) or re.search(r"(?:社会招聘|社招)", title):
        return None, "explicit_non_campus"
    if not _CAMPUS_PAGE_PATTERN.search(campus_text):
        return None, "not_campus"
    if not _targets_current_graduate_cohort(campus_text):
        return None, "wrong_or_missing_cohort"
    if _candidate_cohort_is_unconfirmed(campus_text):
        return None, "cohort_unconfirmed"
    raw_url = str(item.get("official_url", "")).strip()
    if not raw_url:
        return None, "official_url_missing"
    if not raw_url.casefold().startswith("https://"):
        return None, "official_url_not_https"
    official_url = _safe_official_url(raw_url)
    if not official_url:
        return None, "official_url_unsafe_or_discovery_host"
    closing_date = _date_or_none(item.get("closing_date"))
    opening_date = _date_or_none(item.get("opening_date"))
    today = date.today().isoformat()
    if closing_date and closing_date <= today:
        return None, "deadline_passed"
    if opening_date and opening_date > today:
        return None, "not_open_yet"
    category = EMPLOYER_TYPE_BY_POOL[pool["id"]]
    primary_category = str(pool.get("primary_category") or pool["id"])
    observed_at = datetime.now(timezone.utc).isoformat()
    # Campaign pages commonly contain many roles.  URL-only identity collapsed
    # every role on such a page into one record, making a successful scan look
    # almost empty.  Include the stable role identity while keeping retries
    # idempotent.
    identity_company = target.canonical_name if target is not None else company
    identity = "\0".join((identity_company.casefold(), title.casefold(),
                           str(item.get("city", "")).strip().casefold(), official_url))
    job_id = f"web-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    requirements = re.sub(r"\s+", " ", str(item.get("requirements", ""))).strip()[:1200]
    tags = [
        "校园招聘", "动态监控", "AI网页搜索", "待打开核对",
        category, primary_category,
    ]
    if needs_management_review:
        tags.append("待官方核验")
    return {
        "id": job_id,
        "company": company,
        "employer_type": category,
        "title": title,
        "city": str(item.get("city", "")).strip()[:120] or "地点待公告确认",
        "industry": str(item.get("industry", "")).strip()[:80],
        "url": official_url,
        "source": WEB_SEARCH_SOURCE,
        "opening_date": opening_date,
        "closing_date": closing_date,
        "requirements": requirements or "AI 网页搜索发现；请打开企业官方原文核对申请条件。",
        "tags": tags,
        "historical_applicants": None,
        "historical_offers": None,
        "last_verified_at": observed_at,
        "status": "open",
    }, "normalized"


def _normalize_job(
    item: dict[str, Any],
    pool: dict[str, Any] | None = None,
    target: EmployerSearchTarget | None = None,
) -> dict[str, Any] | None:
    return _normalize_job_with_reason(item, pool, target)[0]


def _usage_value(response: Any, name: str) -> int:
    usage = getattr(response, "usage", None)
    return max(0, int(getattr(usage, name, 0) or 0))


def _response_value(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _completed_web_search_sources(response: Any) -> tuple[set[str], int]:
    """Return sanitized citations from completed hosted web-search calls."""
    source_urls: set[str] = set()
    completed_calls = 0
    for output in getattr(response, "output", []) or []:
        if _response_value(output, "type") != "web_search_call":
            continue
        if str(_response_value(output, "status", "")).casefold() != "completed":
            continue
        completed_calls += 1
        action = _response_value(output, "action", {}) or {}
        for source in _response_value(action, "sources", []) or []:
            candidate = _safe_official_url(str(_response_value(source, "url", "")))
            if candidate:
                source_urls.add(candidate)
    if completed_calls == 0:
        raise RuntimeError("Web search returned no completed web_search_call.")
    return source_urls, completed_calls


def _candidate_was_cited(candidate_url: str, source_urls: set[str]) -> bool:
    """Require an exact cited URL or a citation on the same HTTPS host."""
    candidate = _safe_official_url(candidate_url)
    if not candidate:
        return False
    candidate_parts = urllib.parse.urlsplit(candidate)
    candidate_fetch_url = urllib.parse.urlunsplit((
        candidate_parts.scheme,
        candidate_parts.netloc,
        candidate_parts.path or "/",
        candidate_parts.query,
        "",
    ))
    candidate_host = (candidate_parts.hostname or "").casefold()
    for source_url in source_urls:
        source_parts = urllib.parse.urlsplit(source_url)
        source_fetch_url = urllib.parse.urlunsplit((
            source_parts.scheme,
            source_parts.netloc,
            source_parts.path or "/",
            source_parts.query,
            "",
        ))
        if candidate_fetch_url == source_fetch_url:
            return True
        if candidate_host and candidate_host == (source_parts.hostname or "").casefold():
            return True
    return False


def _inspect_normalized_search_candidate(
    job: dict[str, Any], target: EmployerSearchTarget, *, cited: bool,
    page_evidence: CandidatePageEvidence | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Shared deterministic verification for fresh searches and checkpoint replay."""
    if cited:
        if page_evidence is None:
            job["_employer_aliases"] = list(target.aliases)
            evidence = _inspect_official_candidate_page(job)
            job.pop("_employer_aliases", None)
        else:
            evidence = page_evidence
    else:
        # A university notice may lead to an ATS on a different host. This is
        # still a discovery lead, not an authenticated official-page assertion.
        job["tags"].append("搜索引用待确认")
        evidence = CandidatePageEvidence(readable=False, title_confirmed=False)
    if evidence.closed:
        return None, "official_page_closed"
    for field, semantic in (("opening_date", "opening"), ("closing_date", "closing")):
        if not evidence.readable or not _semantic_date_appears_in_page(
            evidence.page_text, job[field], semantic=semantic
        ):
            job[field] = None
    if evidence.title_confirmed:
        job["tags"].extend(["链接已验证", "标题已验证"])
        job["tags"] = [tag for tag in job["tags"] if tag not in {"待官方核验", "待打开核对"}]
        reason = "official_verified"
    else:
        job["tags"].append("链接可访问" if evidence.readable else "官方页暂不可读")
        if "待官方核验" not in job["tags"]:
            job["tags"].append("待官方核验")
        reason = "citation_unconfirmed" if not cited else "official_pending"
    return job, reason


def _discover_company_official_jobs(
    batch: EmployerSearchBatch, cited_urls: set[str], existing: list[dict[str, Any]],
    *, cancellation_check: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Give each sidebar employer the same non-AI official-link crawl budget.

    Known employer/ATS citations can seed discovery even when the model emitted
    no jobs. An explicitly cited candidate on an unknown host may be followed,
    but the shared evidence gate still keeps its jobs pending. Never guess URLs
    from company names, consume other companies' quotas, or make more AI calls.
    """
    target = batch.targets[0]
    urls = list(dict.fromkeys([
        *sorted(url for url in cited_urls if _safe_official_url(url) and _official_domain_confirmed(
            target.canonical_name, url, target.aliases,
        )),
        *(job["url"] for job in existing if _candidate_was_cited(job["url"], cited_urls)),
    ]))
    discovered = discover_official_job_pages(
        urls, company=target.canonical_name, fetcher=fetch_watch_page,
        cancellation_check=cancellation_check,
    )
    jobs: list[dict[str, Any]] = []
    decisions: dict[str, int] = {}
    for candidate in discovered.candidates:
        if candidate.job.get("posting_expired"):
            decisions["official_posting_expired"] = decisions.get("official_posting_expired", 0) + 1
            continue
        job, reason = _normalize_job_with_reason(candidate.job, batch.pool, target)
        if job is not None:
            job["_employer_aliases"] = list(target.aliases)
            evidence = _evaluate_official_candidate_page(job, candidate.page_text, candidate.final_url)
            job.pop("_employer_aliases", None)
            job, reason = _inspect_normalized_search_candidate(
                job, target, cited=True, page_evidence=evidence,
            )
        decisions[reason] = decisions.get(reason, 0) + 1
        if job is not None:
            job["tags"].append("官网列表逐页发现")
            jobs.append(job)
    coverage = {**discovered.coverage, "accepted_count": len(jobs), "candidate_decisions": decisions}
    return jobs, coverage


def _search_batch(
    api_client: OpenAI,
    batch: EmployerSearchBatch,
    *, cancellation_check: Callable[[], None] | None = None,
) -> WebRecruitmentSearchResult:
    check_discovery_cancellation(cancellation_check)
    if len(batch.targets) != 1:
        raise RuntimeError(
            "Employer search requires exactly one target per request."
        )
    response = api_client.responses.create(
        model=settings.recruitment_web_search_model,
        tools=[
            {
                "type": "web_search",
                "search_context_size": "medium",
                "user_location": {
                    "type": "approximate",
                    "country": "CN",
                    "timezone": "Asia/Shanghai",
                },
            }
        ],
        input=_search_prompt(batch),
        text={
            "format": {
                "type": "json_schema",
                "name": "future_radar_jobs",
                "strict": True,
                "schema": _search_result_schema(batch),
            }
        },
        include=["web_search_call.action.sources"],
        tool_choice="required",
        parallel_tool_calls=True,
        # This is a ceiling for the one employer's search/page visits, not a
        # promise that the model made this many calls. Actual completed tool
        # calls below are mandatory before the employer counts as searched.
        max_tool_calls=max(1, settings.recruitment_web_search_max_tool_calls),
        max_output_tokens=SEARCH_MAX_OUTPUT_TOKENS,
        store=False,
    )
    response_status = str(_response_value(response, "status", "completed"))
    check_discovery_cancellation(cancellation_check)
    if response_status != "completed" or _response_value(response, "incomplete_details"):
        raise RuntimeError(
            f"Employer search batch {batch.id} returned an incomplete response."
        )
    cited_source_urls, tool_calls = _completed_web_search_sources(response)
    payload = json.loads(response.output_text)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise RuntimeError(
            f"Employer search batch {batch.id} returned an invalid result."
        )
    expected_target_ids = {target.id for target in batch.targets}
    checked_target_ids = [
        str(item.get("target_id", ""))
        for item in payload.get("checked_employers", [])
        if isinstance(item, dict)
    ]
    if (
        len(checked_target_ids) != len(expected_target_ids)
        or len(checked_target_ids) != len(set(checked_target_ids))
        or set(checked_target_ids) != expected_target_ids
    ):
        raise RuntimeError(
            f"Employer search batch {batch.id} returned incomplete coverage."
        )

    targets_by_id = {target.id: target for target in batch.targets}
    normalized: list[dict[str, Any]] = []
    seen_jobs: set[tuple[str, str, str, str]] = set()
    employers_with_candidates: set[str] = set()
    for item in payload["jobs"]:
        check_discovery_cancellation(cancellation_check)
        if not isinstance(item, dict):
            continue
        target = targets_by_id.get(str(item.get("target_id", "")))
        if target is None:
            target = _target_for_company(str(item.get("company", "")), batch.targets)
        if target is None:
            continue
        job = _normalize_job(item, batch.pool, target)
        if not job:
            continue
        cited = _candidate_was_cited(job["url"], cited_source_urls)
        job_key = (
            job["company"].casefold(), job["title"].casefold(),
            job["city"].casefold(), job["url"],
        )
        if job_key in seen_jobs:
            continue
        job, _ = _inspect_normalized_search_candidate(job, target, cited=cited)
        if job is None:
            continue
        seen_jobs.add(job_key)
        normalized.append(job)
        employers_with_candidates.add(target.canonical_name)
    try:
        discovered, official_coverage = _discover_company_official_jobs(
            batch, cited_source_urls, normalized, cancellation_check=cancellation_check,
        )
        existing_by_key = {
            (job["company"].casefold(), job["title"].casefold(), job["city"].casefold(), job["url"]): job
            for job in normalized
        }
        for job in discovered:
            key = (job["company"].casefold(), job["title"].casefold(), job["city"].casefold(), job["url"])
            if key not in seen_jobs:
                seen_jobs.add(key)
                normalized.append(job)
                existing_by_key[key] = job
                employers_with_candidates.add(batch.targets[0].canonical_name)
            elif "标题已验证" in job.get("tags", []):
                # A previously pending model row can be attested by this
                # deterministic follow-up; don't discard that fresh evidence.
                existing_by_key[key].update(job)
    except OfficialDiscoveryCancelled:
        raise
    except Exception:
        # Preserve completed search results on an independent HTTP/parser
        # failure. The source is partial, not a falsely successful empty scan.
        official_coverage = {
            "employer": batch.targets[0].canonical_name, "status": "failed",
            "pagination_complete": False, "snapshot_complete": False,
            "completion_reason": "official_discovery_failed", "accepted_count": 0,
        }
        logger.warning("Official-list discovery failed for search batch %s", batch.id)
    target_names = tuple(target.canonical_name for target in batch.targets)
    return WebRecruitmentSearchResult(
        jobs=normalized,
        input_tokens=_usage_value(response, "input_tokens"),
        output_tokens=_usage_value(response, "output_tokens"),
        total_tokens=_usage_value(response, "total_tokens"),
        tool_calls=tool_calls,
        model=str(getattr(response, "model", settings.recruitment_web_search_model)),
        target_employers=target_names,
        searched_employers=target_names,
        employers_with_candidates=tuple(sorted(employers_with_candidates)),
        search_batches=1,
        official_discovery=(official_coverage,),
    )


def _search_pool(api_client: OpenAI, pool: dict[str, Any]) -> WebRecruitmentSearchResult:
    """Compatibility wrapper that fully covers one pool through small batches."""
    batches = build_employer_search_batches([pool])
    results = [_search_batch(api_client, batch) for batch in batches]
    jobs: list[dict[str, Any]] = []
    seen_job_ids: set[str] = set()
    for result in results:
        for job in result.jobs:
            if job["id"] in seen_job_ids:
                continue
            seen_job_ids.add(job["id"])
            jobs.append(job)
    return WebRecruitmentSearchResult(
        jobs=jobs,
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        total_tokens=sum(result.total_tokens for result in results),
        tool_calls=sum(result.tool_calls for result in results),
        model=results[0].model if results else settings.recruitment_web_search_model,
        target_employers=tuple(
            name for result in results for name in result.target_employers
        ),
        searched_employers=tuple(
            name for result in results for name in result.searched_employers
        ),
        employers_with_candidates=tuple(sorted({
            name for result in results for name in result.employers_with_candidates
        })),
        search_batches=len(results),
        official_discovery=tuple(item for result in results for item in result.official_discovery),
    )


def search_current_recruitment_jobs(
    client: OpenAI | None = None, *, cancellation_check: Callable[[], None] | None = None,
) -> WebRecruitmentSearchResult:
    check_discovery_cancellation(cancellation_check)
    api_client = client or OpenAI(api_key=settings.openai_api_key)
    pools = list(PERSONAL_MONITOR_POOLS)
    targets = build_employer_search_targets(pools)
    batches = build_employer_search_batches(pools)
    results: list[WebRecruitmentSearchResult] = []
    failed_pools: set[str] = set()
    failed_batches: list[str] = []
    failed_employer_names: set[str] = set()
    if not batches:
        return WebRecruitmentSearchResult(
            jobs=[], input_tokens=0, output_tokens=0, total_tokens=0,
            tool_calls=0, model=settings.recruitment_web_search_model,
        )
    max_workers = min(8, len(batches))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _search_batch, api_client, batch,
                **({"cancellation_check": cancellation_check} if cancellation_check is not None else {}),
            ): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                results.append(future.result())
            except OfficialDiscoveryCancelled:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception:
                failed_pools.add(str(batch.pool["id"]))
                failed_batches.append(batch.id)
                failed_employer_names.update(
                    target.canonical_name for target in batch.targets
                )
                logger.exception("Recruitment web search batch failed: %s", batch.id)

    if not results:
        raise RuntimeError("All recruitment web-search pools failed.")

    jobs: list[dict[str, Any]] = []
    jobs_by_identity: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for result in results:
        for job in result.jobs:
            key = (
                job["company"].casefold(), job["title"].casefold(),
                job["city"].casefold(), job["url"],
            )
            existing = jobs_by_identity.get(key)
            if existing:
                existing["tags"] = list(dict.fromkeys([
                    *existing.get("tags", []), *job.get("tags", []),
                ]))
                continue
            jobs_by_identity[key] = job
            jobs.append(job)
    searched_names = {
        name for result in results for name in result.searched_employers
    }
    searched_employers = tuple(
        target.canonical_name
        for target in targets
        if target.canonical_name in searched_names
    )
    employers_with_candidates = tuple(sorted({
        name for result in results for name in result.employers_with_candidates
    }))
    final_result = WebRecruitmentSearchResult(
        # Do not reintroduce a global result cap: every accepted update from
        # every company-level batch must reach the Future Radar candidate pool.
        jobs=jobs,
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        total_tokens=sum(result.total_tokens for result in results),
        tool_calls=sum(result.tool_calls for result in results),
        model=results[0].model if results else settings.recruitment_web_search_model,
        failed_pools=tuple(sorted(failed_pools)),
        target_employers=tuple(target.canonical_name for target in targets),
        searched_employers=searched_employers,
        employers_with_candidates=employers_with_candidates,
        failed_employers=tuple(
            target.canonical_name
            for target in targets
            if target.canonical_name in failed_employer_names
        ),
        search_batches=len(batches),
        failed_batches=tuple(sorted(failed_batches)),
        official_discovery=tuple(item for result in results for item in result.official_discovery),
    )
    logger.info(
        "Recruitment company coverage targets=%d searched=%d candidates=%d "
        "batches=%d failed_batches=%d coverage=%.2f%%",
        len(final_result.target_employers),
        len(final_result.searched_employers),
        len(final_result.employers_with_candidates),
        final_result.search_batches,
        len(final_result.failed_batches),
        final_result.coverage_percent,
    )
    return final_result
