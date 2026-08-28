import hashlib
import json
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from openai import OpenAI

from .config import settings
from .live_sources import PERSONAL_MONITOR_POOLS, PRIORITY_EMPLOYERS
from .recruitment_watch import (
    WatchFetchError,
    fetch_watch_page,
    normalize_public_https_urls,
)


logger = logging.getLogger(__name__)
WEB_SEARCH_SOURCE = "OpenAI 网页搜索"
WEB_SEARCH_STATE_KEY = "recruitment_web_search"
MAX_SEARCH_JOBS = 100
MAX_JOBS_PER_CATEGORY = 10
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

SEARCH_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "maxItems": MAX_JOBS_PER_CATEGORY,
            "items": {
                "type": "object",
                "properties": {
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
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class WebRecruitmentSearchResult:
    jobs: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int
    model: str
    failed_pools: tuple[str, ...] = ()


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


def _search_prompt(pool: dict[str, Any]) -> str:
    today = date.today().isoformat()
    employers = "、".join(pool["employers"])
    category = EMPLOYER_TYPE_BY_POOL[pool["id"]]
    return f"""
今天是 {today}。搜索“{pool['name']}”这类重点雇主当前仍可申请的校园招聘、应届生、Graduate、管培生、提前批或留学生招聘岗位。

目标雇主：{employers}
category 固定填写：{category}

要求：
1. 覆盖尽可能多的目标雇主，不要只返回一家单位或一个汇总页面。
2. 只返回当前开放且能直接投递或查看原公告的岗位，排除社招、实习、城市招聘导航页、转载汇总页和已过期岗位。
3. official_url 必须是企业招聘官网或企业授权 ATS 的直接 HTTPS 链接，不得填搜索结果页、公众号转载、社交媒体或臆造链接。
4. opening_date / closing_date 只有原文明确写明时才填写 YYYY-MM-DD，否则为 null；不得把发布日期当截止日期。
5. city 未公告时写“地点待公告确认”。requirements 简洁记录毕业年份、学历、专业、语言或笔试门槛；无法确认时明确写“待官方原文核对”。
6. 中国人民银行和中国农业发展银行只能使用官方原文中的实际岗位名称；不得自行把笼统校园招聘或所属单位招聘改写成“管培生”。如果原文确实使用该称谓，保留原称并标记“待官方核验”。
7. 最多返回 {MAX_JOBS_PER_CATEGORY} 条，优先最新和截止日期较近的岗位。
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


def _official_domain_confirmed(company: str, url: str) -> bool:
    hostname = (urllib.parse.urlsplit(url).hostname or "").casefold()
    if not hostname:
        return False
    if any(_hostname_matches(hostname, domain) for domain in KNOWN_AUTHORIZED_ATS_DOMAINS):
        return True
    company_key = _evidence_key(company)
    for employer, domains in OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER.items():
        employer_key = _evidence_key(employer)
        if not employer_key or not (
            employer_key in company_key or company_key in employer_key
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
    company_aliases = _company_evidence_aliases(company)
    employer_confirmed = bool(
        page_key and any(alias in page_key for alias in company_aliases)
    )
    domain_confirmed = _official_domain_confirmed(company, final_url)
    cohort_confirmed = bool(
        _CAMPUS_PAGE_PATTERN.search(page_text)
        and _targets_current_graduate_cohort(page_text)
    )
    title_key = _evidence_key(str(job.get("title", "")))
    exact_title_confirmed = bool(title_key and title_key in page_key)
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


def _normalize_job(
    item: dict[str, Any], pool: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    company = str(item.get("company", "")).strip()[:120]
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
        return None
    pool_employers = {str(value).casefold() for value in pool.get("employers", [])}
    employer_key = _priority_employer(company, pool_employers)
    if not employer_key:
        return None
    title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()[:240]
    needs_management_review = _needs_management_trainee_review(company, title)
    campus_text = f"{title} {item.get('requirements', '')}".lower()
    if not title or not any(
        marker in campus_text
        for marker in ("校园", "校招", "应届", "毕业生", "graduate", "campus", "管培", "提前批")
    ):
        return None
    if not _targets_current_graduate_cohort(campus_text):
        return None
    official_url = _safe_official_url(str(item.get("official_url", "")).strip())
    if not official_url:
        return None
    closing_date = _date_or_none(item.get("closing_date"))
    opening_date = _date_or_none(item.get("opening_date"))
    today = date.today().isoformat()
    if closing_date and closing_date <= today:
        return None
    if opening_date and opening_date > today:
        return None
    category = EMPLOYER_TYPE_BY_POOL[pool["id"]]
    primary_category = str(pool.get("primary_category") or pool["id"])
    observed_at = datetime.now(timezone.utc).isoformat()
    # Campaign pages commonly contain many roles.  URL-only identity collapsed
    # every role on such a page into one record, making a successful scan look
    # almost empty.  Include the stable role identity while keeping retries
    # idempotent.
    identity = "\0".join((company.casefold(), title.casefold(),
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
    }


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


def _search_pool(api_client: OpenAI, pool: dict[str, Any]) -> WebRecruitmentSearchResult:
    response = api_client.responses.create(
        model=settings.recruitment_web_search_model,
        tools=[
            {
                "type": "web_search",
                "search_context_size": "low",
                "user_location": {
                    "type": "approximate",
                    "country": "CN",
                    "timezone": "Asia/Shanghai",
                },
            }
        ],
        input=_search_prompt(pool),
        text={
            "format": {
                "type": "json_schema",
                "name": "future_radar_jobs",
                "strict": True,
                "schema": SEARCH_RESULT_SCHEMA,
            }
        },
        include=["web_search_call.action.sources"],
        tool_choice="required",
        max_tool_calls=1,
        max_output_tokens=1_600,
        store=False,
    )
    cited_source_urls, tool_calls = _completed_web_search_sources(response)
    payload = json.loads(response.output_text)
    normalized: list[dict[str, Any]] = []
    seen_jobs: set[tuple[str, str, str, str]] = set()
    for item in payload.get("jobs", [])[:MAX_JOBS_PER_CATEGORY]:
        if not isinstance(item, dict):
            continue
        job = _normalize_job(item, pool)
        if not job:
            continue
        if not _candidate_was_cited(job["url"], cited_source_urls):
            continue
        job_key = (
            job["company"].casefold(), job["title"].casefold(),
            job["city"].casefold(), job["url"],
        )
        if job_key in seen_jobs:
            continue
        evidence = _inspect_official_candidate_page(job)
        if evidence.closed:
            continue
        job["opening_date"] = (
            job["opening_date"]
            if evidence.readable and _semantic_date_appears_in_page(
                evidence.page_text, job["opening_date"], semantic="opening"
            )
            else None
        )
        job["closing_date"] = (
            job["closing_date"]
            if evidence.readable and _semantic_date_appears_in_page(
                evidence.page_text, job["closing_date"], semantic="closing"
            )
            else None
        )
        if evidence.title_confirmed:
            job["tags"].append("链接已验证")
            job["tags"].append("标题已验证")
            job["tags"] = [
                tag for tag in job["tags"]
                if tag not in {"待官方核验", "待打开核对"}
            ]
        else:
            job["tags"].append(
                "链接可访问" if evidence.readable else "官方页暂不可读"
            )
            if "待官方核验" not in job["tags"]:
                job["tags"].append("待官方核验")
        seen_jobs.add(job_key)
        normalized.append(job)
    return WebRecruitmentSearchResult(
        jobs=normalized,
        input_tokens=_usage_value(response, "input_tokens"),
        output_tokens=_usage_value(response, "output_tokens"),
        total_tokens=_usage_value(response, "total_tokens"),
        tool_calls=tool_calls,
        model=str(getattr(response, "model", settings.recruitment_web_search_model)),
    )


def search_current_recruitment_jobs(client: OpenAI | None = None) -> WebRecruitmentSearchResult:
    api_client = client or OpenAI(api_key=settings.openai_api_key)
    pools = PERSONAL_MONITOR_POOLS[: settings.recruitment_web_search_max_tool_calls]
    results: list[WebRecruitmentSearchResult] = []
    failed_pools: list[str] = []
    max_workers = min(4, len(pools))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_search_pool, api_client, pool): pool["id"]
            for pool in pools
        }
        for future in as_completed(futures):
            pool_id = futures[future]
            try:
                results.append(future.result())
            except Exception:
                failed_pools.append(pool_id)
                logger.exception("Recruitment web search pool failed: %s", pool_id)

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
    return WebRecruitmentSearchResult(
        jobs=jobs[:MAX_SEARCH_JOBS],
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        total_tokens=sum(result.total_tokens for result in results),
        tool_calls=sum(result.tool_calls for result in results),
        model=results[0].model if results else settings.recruitment_web_search_model,
        failed_pools=tuple(sorted(failed_pools)),
    )
