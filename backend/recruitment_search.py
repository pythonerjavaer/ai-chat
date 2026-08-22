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
MAX_SEARCH_JOBS = 64
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
    "policy_and_major_banks": "银行/金融",
    "securities_funds_asset": "券商/基金",
    "insurance_fintech": "保险/综合金融",
    "internet_tech_scale": "互联网企业",
    "consumer_global_consulting": "外企/咨询",
    "quant_private_capital": "量化私募",
}

EMPLOYER_TYPE_BY_NAME = {
    employer.lower(): EMPLOYER_TYPE_BY_POOL[pool["id"]]
    for pool in PERSONAL_MONITOR_POOLS
    for employer in pool["employers"]
}

# These institutions publish campus and affiliated-unit recruitment under
# specific official notices.  A generic "management trainee" label for either
# institution is not an official job family and has repeatedly produced false
# positives from search snippets.  Keep this narrowly scoped: it does not
# suppress recruitment by their separately named affiliates.
UNSUPPORTED_MANAGEMENT_TRAINEE_EMPLOYERS = {
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
6. 中国人民银行和中国农业发展银行只能使用其官方公告中的实际岗位名称；不得把笼统校园招聘或所属单位招聘改写成“管培生”。
7. 最多返回 {MAX_JOBS_PER_CATEGORY} 条，优先最新和截止日期较近的岗位。
""".strip()


def _date_or_none(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _priority_employer(company: str) -> str | None:
    normalized = company.strip().lower()
    matches = [
        employer
        for employer in PRIORITY_EMPLOYERS
        if employer in normalized or normalized in employer
    ]
    return max(matches, key=len) if matches else None


def _is_unsupported_management_trainee_claim(company: str, title: str) -> bool:
    normalized_company = re.sub(r"\s+", "", company).casefold()
    normalized_title = re.sub(r"\s+", "", title).casefold()
    return (
        ("管培" in normalized_title or "管理培训生" in normalized_title)
        and any(
            employer in normalized_company
            for employer in UNSUPPORTED_MANAGEMENT_TRAINEE_EMPLOYERS
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


def _official_candidate_link_is_readable(url: str) -> bool:
    """Keep a web-search candidate only when its supplied HTTPS page opens.

    This is deliberately deterministic and sends no page contents to an AI
    provider. It rejects dead links, redirects to unsafe addresses, and pages
    that do not return readable HTML/text before a candidate reaches users.
    """
    try:
        result = fetch_watch_page(
            url,
            (),
            timeout_seconds=6,
            max_bytes=500_000,
        )
    except (OSError, ValueError, WatchFetchError):
        return False
    return bool(result.text)


def _normalize_job(item: dict[str, Any]) -> dict[str, Any] | None:
    company = str(item.get("company", "")).strip()[:120]
    employer_key = _priority_employer(company)
    if not employer_key:
        return None
    title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()[:240]
    if _is_unsupported_management_trainee_claim(company, title):
        return None
    campus_text = f"{title} {item.get('requirements', '')}".lower()
    if not title or not any(
        marker in campus_text
        for marker in ("校园", "校招", "应届", "毕业生", "graduate", "campus", "管培", "提前批")
    ):
        return None
    official_url = _safe_official_url(str(item.get("official_url", "")).strip())
    if not official_url:
        return None
    closing_date = _date_or_none(item.get("closing_date"))
    if closing_date and closing_date < date.today().isoformat():
        return None
    category = EMPLOYER_TYPE_BY_NAME.get(employer_key)
    if not category:
        return None
    observed_at = datetime.now(timezone.utc).isoformat()
    job_id = f"web-{hashlib.sha256(official_url.encode()).hexdigest()[:24]}"
    requirements = re.sub(r"\s+", " ", str(item.get("requirements", ""))).strip()[:1200]
    return {
        "id": job_id,
        "company": company,
        "employer_type": category,
        "title": title,
        "city": str(item.get("city", "")).strip()[:120] or "地点待公告确认",
        "industry": str(item.get("industry", "")).strip()[:80],
        "url": official_url,
        "source": WEB_SEARCH_SOURCE,
        "opening_date": _date_or_none(item.get("opening_date")),
        "closing_date": closing_date,
        "requirements": requirements or "AI 网页搜索发现；请打开企业官方原文核对申请条件。",
        "tags": ["校园招聘", "动态监控", "AI网页搜索", "待打开核对", category],
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
        job = _normalize_job(item)
        if (
            not job
            or job["url"] in seen_urls
            or not _official_candidate_link_is_readable(job["url"])
        ):
            continue
        job["tags"].append("链接已验证")
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
    seen_urls: set[str] = set()
    for result in results:
        for job in result.jobs:
            if job["url"] in seen_urls:
                continue
            seen_urls.add(job["url"])
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
