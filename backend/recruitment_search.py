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
    title_confirmed: bool
    closed: bool = False
    page_text: str = ""


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


def _date_appears_in_page(page_text: str, iso_date: str | None) -> bool:
    if not iso_date:
        return False
    try:
        value = date.fromisoformat(iso_date)
    except ValueError:
        return False
    compact = re.sub(r"\s+", "", page_text.casefold())
    variants = {
        value.isoformat(),
        f"{value.year}/{value.month:02d}/{value.day:02d}",
        f"{value.year}.{value.month:02d}.{value.day:02d}",
        f"{value.year}年{value.month}月{value.day}日",
        f"{value.year}年{value.month:02d}月{value.day:02d}日",
    }
    return any(variant.casefold() in compact for variant in variants)


def _targets_current_graduate_cohort(value: str) -> bool:
    today = date.today()
    target_year = today.year + 1 if today.month >= 6 else today.year
    short_year = str(target_year)[-2:]
    normalized = re.sub(r"\s+", "", value.casefold())
    return str(target_year) in normalized or f"{short_year}届" in normalized


def _inspect_official_candidate_page(job: dict[str, Any]) -> CandidatePageEvidence:
    """Open the supplied page and check whether its body supports the title.

    This is deliberately deterministic and sends no page contents to an AI
    provider. A reachable page is preserved as a candidate, but it is only
    promoted to verified when the claimed title is present in the page body.
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
    page_text = str(result.text or "")
    page_key = _evidence_key(page_text)
    title_key = _evidence_key(str(job.get("title", "")))
    return CandidatePageEvidence(
        readable=bool(page_key),
        title_confirmed=bool(title_key and title_key in page_key),
        closed=bool(_CLOSED_PAGE_PATTERN.search(page_text)),
        page_text=page_text,
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
    job_id = f"web-{hashlib.sha256(official_url.encode()).hexdigest()[:24]}"
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
        max_tool_calls=1,
        max_output_tokens=1_600,
        store=False,
    )
    payload = json.loads(response.output_text)
    normalized: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in payload.get("jobs", [])[:MAX_JOBS_PER_CATEGORY]:
        if not isinstance(item, dict):
            continue
        job = _normalize_job(item, pool)
        if not job or job["url"] in seen_urls:
            continue
        evidence = _inspect_official_candidate_page(job)
        if not evidence.readable or evidence.closed:
            continue
        job["opening_date"] = (
            job["opening_date"]
            if _date_appears_in_page(evidence.page_text, job["opening_date"])
            else None
        )
        job["closing_date"] = (
            job["closing_date"]
            if _date_appears_in_page(evidence.page_text, job["closing_date"])
            else None
        )
        job["tags"].append("链接已验证")
        if evidence.title_confirmed:
            job["tags"].append("标题已验证")
            job["tags"] = [
                tag for tag in job["tags"]
                if tag not in {"待官方核验", "待打开核对"}
            ]
        elif "待官方核验" not in job["tags"]:
            job["tags"].append("待官方核验")
        seen_urls.add(job["url"])
        normalized.append(job)
    tool_calls = sum(
        getattr(output, "type", "") == "web_search_call"
        for output in getattr(response, "output", [])
    )
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
    jobs_by_url: dict[str, dict[str, Any]] = {}
    for result in results:
        for job in result.jobs:
            existing = jobs_by_url.get(job["url"])
            if existing:
                existing["tags"] = list(dict.fromkeys([
                    *existing.get("tags", []), *job.get("tags", []),
                ]))
                continue
            jobs_by_url[job["url"]] = job
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
