"""Pluggable source adapters used by Future Radar scans."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from openai import OpenAI

from .. import database
from ..chatgpt_sources import KNOWN_CHATGPT_SOURCE_IDS
from ..recruitment import primary_employer_category
from ..recruitment_rating import merge_source_ratings, normalize_source_rating
from ..recruitment_limits import MAX_MONITOR_BATCH_ITEMS
from ..recruitment_search import (
    EMPLOYER_ALIAS_GROUPS,
    EmployerSearchTarget,
    WEB_SEARCH_SOURCE,
    _company_matches_target,
    _evaluate_official_candidate_page,
    _inspect_official_candidate_page,
    _normalize_job as normalize_web_search_job,
    _semantic_date_appears_in_page,
    search_current_recruitment_jobs,
)
from ..recruitment_watch import WatchFetchError, fetch_watch_page
from ..recruitment_watch import normalize_html_text, validate_public_https_url
from .ai import extract_recruitment_content
from .normalization import (
    PRIMARY_CATEGORY_CODES,
    canonical_telecom_operator,
    canonicalize_url,
    clean_text,
    normalize_date,
    normalize_taxonomy_value,
    stable_digest,
    stable_program_external_id,
    telecom_primary_category,
)
from .public_discovery import (
    check_discovery_cancellation,
    discover_official_job_pages,
    discover_bank_recruitment_articles,
    discover_sasac_recruitment_articles,
)
from .repository import RadarRepository


logger = logging.getLogger(__name__)


_PUBLIC_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_PUBLIC_PHONES = (
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)"),
    re.compile(r"(?<!\d)\+\d{8,15}(?!\d)"),
)
_PUBLIC_SECRETS = (
    re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:authorization|cookie|set-cookie|api[_ -]?key|access[_ -]?token|"
        r"refresh[_ -]?token)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{10,}\b"),
)
_PUBLIC_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie",
    "key", "password", "refresh_token", "secret", "sig", "signature", "token",
}


def _redact_public_text(value: Any, *, limit: int) -> str:
    """Keep a useful public excerpt without persisting contacts or credentials."""
    # Redact before applying the output limit; truncating first could preserve
    # the prefix of a credential that crosses the excerpt boundary.
    text = clean_text(value, limit=1_500_000)
    text = _PUBLIC_EMAIL.sub("[redacted-email]", text)
    for pattern in _PUBLIC_PHONES:
        text = pattern.sub("[redacted-phone]", text)
    for pattern in _PUBLIC_SECRETS:
        text = pattern.sub("[redacted-secret]", text)
    text = _PUBLIC_UUID.sub("[redacted-uuid]", text)
    return clean_text(text, limit=limit)


def _public_reference_url(value: Any) -> str | None:
    """Return a canonical public reference, excluding chats and credential URLs."""
    candidate = clean_text(value, limit=2_000)
    decoded = urllib.parse.unquote(candidate)
    try:
        decoded_parts = urllib.parse.urlsplit(decoded)
    except ValueError:
        return None
    phone_check_path = decoded_parts.path
    reference_host = (decoded_parts.hostname or "").casefold()
    # Public ATS identifiers can happen to contain a phone-shaped digit run.
    # Exempt only the identifier in known public recruitment routes, never a
    # contact query, another host, or a number elsewhere in the job title.
    if reference_host == "xiaoyuan.zhaopin.com":
        phone_check_path = re.sub(
            r"^/company/KA\d{6,16}D\d{6,16}/?$", "/company/public-ats-id", phone_check_path,
        )
        phone_check_path = re.sub(
            r"^/job/CC\d{6,16}J\d{6,16}/?$", "/job/public-ats-id", phone_check_path,
        )
    elif reference_host.endswith(".myworkdayjobs.com") and "/job/" in phone_check_path:
        phone_check_path = re.sub(
            r"_(?:JR|REQ|R)[-_]?\d{4,16}/?$", "_public-ats-id", phone_check_path,
            flags=re.IGNORECASE,
        )
    phone_check_url = decoded_parts._replace(path=phone_check_path).geturl()
    if (
        _PUBLIC_EMAIL.search(decoded)
        or any(pattern.search(phone_check_url) for pattern in _PUBLIC_PHONES)
        or any(pattern.search(decoded) for pattern in _PUBLIC_SECRETS)
    ):
        return None
    try:
        raw_query = urllib.parse.urlsplit(candidate).query
    except ValueError:
        return None
    if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _ in urllib.parse.parse_qsl(raw_query)):
        return None
    try:
        canonical = canonicalize_url(candidate, allow_empty=False)
    except ValueError:
        return None
    parsed = urllib.parse.urlsplit(canonical)
    # ChatGPT share pages are import transports, never recruitment evidence;
    # private /c UUIDs and public share IDs must not enter Radar provenance.
    if parsed.hostname == "chatgpt.com":
        return None
    return canonical


@dataclass
class AdapterResult:
    programs: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    articles: list[dict[str, Any]] = field(default_factory=list)
    content_hash: str | None = None
    normalized_content: str = ""
    snapshot_complete: bool = True
    status: str = "healthy"
    message: str = ""
    ai_calls: int = 0
    model_tokens_used: int = 0
    # Deterministic orchestration counts, never model-declared search coverage.
    coverage: dict[str, Any] = field(default_factory=dict)
    # Discovery adapters may deterministically open an official HTTPS page
    # after finding a candidate.  Keep that per-item attestation separate from
    # the source's broad trust level: a web-search source is still discovery,
    # while an exact row whose official page contains the claimed title can be
    # promoted by the service.
    verified_job_external_ids: set[str] = field(default_factory=set)
    # Explicit source withdrawals, not jobs merely absent from a partial page.
    # Scoped legacy ingest uses these to retire only its own provenance link.
    retired_job_external_ids: set[str] = field(default_factory=set)


class SourceAdapter(Protocol):
    def scan(self, source: dict[str, Any]) -> AdapterResult: ...


class DiscoveryLimitedError(RuntimeError):
    pass


class DomainRateLimiter:
    """Small process-local politeness layer; persisted source intervals remain authoritative."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._last_request: dict[str, float] = {}

    def wait(self, domain: str, minimum_interval_seconds: float) -> None:
        if not domain or minimum_interval_seconds <= 0:
            return
        with self._guard:
            now = time.monotonic()
            remaining = minimum_interval_seconds - (now - self._last_request.get(domain, 0))
            if remaining > 0:
                time.sleep(min(remaining, 5.0))
            self._last_request[domain] = time.monotonic()


DOMAIN_LIMITER = DomainRateLimiter()


class DiscoveryLimitedAdapter:
    def scan(self, source: dict[str, Any]) -> AdapterResult:
        raise DiscoveryLimitedError(
            "Public article discovery is not configured for this source; no success was fabricated."
        )


class WechatSourceAdapter:
    """Use only an administrator-configured, publicly accessible article URL.

    It deliberately has no login, Cookie, CAPTCHA, account-search or anti-bot
    behavior.  A source without a verified public URL remains visibly limited.
    """

    def __init__(self, *, repository: RadarRepository, api_key: str, ai_model: str):
        self.html = OfficialHtmlAdapter(
            repository=repository, api_key=api_key, ai_model=ai_model
        )

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        if not source.get("url"):
            raise DiscoveryLimitedError(
                "No verified public article URL is configured; WeChat access was not bypassed."
            )
        config = source.get("adapter_config", {})
        title = clean_text(config.get("article_title"), limit=300)
        try:
            result = self.html.scan(source)
        except RuntimeError as exc:
            # A public title/URL remains useful discovery evidence even when
            # the body is temporarily inaccessible. It is not treated as an
            # official verification and cannot close existing entities.
            if not title:
                raise
            metadata = {
                "publisher": clean_text(
                    source.get("account_name") or source.get("name"), limit=160
                ),
                "article_title": title,
                "article_url": source["url"],
                "publish_time": config.get("publish_time"),
                "raw_excerpt": clean_text(config.get("search_excerpt"), limit=1_500),
                "is_recruitment": bool(config.get("is_recruitment", True)),
                "recruitment_year": config.get("recruitment_year"),
                "classification": "recruitment_signal",
            }
            digest = hashlib.sha256(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            return AdapterResult(
                articles=[metadata],
                content_hash=digest,
                snapshot_complete=False,
                status="discovery_only",
                message=f"Article metadata preserved; body fetch unavailable ({type(exc).__name__}).",
            )
        if title:
            result.articles.append({
                "publisher": clean_text(source.get("account_name") or source.get("name"), limit=160),
                "article_title": title,
                "article_url": source["url"],
                "publish_time": config.get("publish_time"),
                "raw_excerpt": clean_text(result.normalized_content, limit=1_500),
                "is_recruitment": bool(result.programs or result.jobs),
                "recruitment_year": config.get("recruitment_year"),
                "classification": "recruitment" if result.programs or result.jobs else "unknown",
            })
        return result


WECHAT_DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "publish_date": {"type": ["string", "null"]},
                    "excerpt": {"type": "string"},
                },
                "required": ["title", "url", "publish_date", "excerpt"],
                "additionalProperties": False,
            },
        },
        "jobs": {
            "type": "array",
            "maxItems": 10,
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
                    "company", "title", "city", "industry", "official_url",
                    "opening_date", "closing_date", "requirements", "category",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["articles", "jobs"],
    "additionalProperties": False,
}


def _response_field(value: Any, name: str, default: Any = None) -> Any:
    """Read one Responses API field from either SDK objects or test dictionaries."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _completed_web_search_calls(response: Any) -> list[Any]:
    """Return only tool calls whose provider status explicitly completed."""
    output = _response_field(response, "output", [])
    if not isinstance(output, (list, tuple)):
        return []
    return [
        item
        for item in output
        if clean_text(_response_field(item, "type"), limit=80).casefold()
        == "web_search_call"
        and clean_text(_response_field(item, "status"), limit=80).casefold()
        == "completed"
    ]


def _web_search_source_urls(calls: list[Any]) -> set[str]:
    """Extract allowlisted public citations from completed web-search calls."""
    result: set[str] = set()
    for call in calls:
        action = _response_field(call, "action")
        sources = _response_field(action, "sources", [])
        if not isinstance(sources, (list, tuple)):
            continue
        for source in sources:
            url = _public_reference_url(_response_field(source, "url"))
            if url:
                result.add(url)
    return result


def _article_url_supported_by_search_sources(
    article_url: str, source_urls: set[str]
) -> bool:
    """Require an exact citation or the same public hostname as one citation."""
    if article_url in source_urls:
        return True
    hostname = (urllib.parse.urlsplit(article_url).hostname or "").casefold()
    if not hostname:
        return False
    return any(
        (urllib.parse.urlsplit(source_url).hostname or "").casefold() == hostname
        for source_url in source_urls
    )


class WechatWebSearchAdapter:
    """Discover public article signals without automating WeChat login.

    Search results are candidates only. Every job is fetched again from its
    official HTTPS page and attested per item; articles never verify jobs.
    """

    def __init__(self, *, api_key: str, ai_model: str):
        self.api_key = api_key
        self.ai_model = ai_model

    @staticmethod
    def _prompt(source: dict[str, Any]) -> str:
        today = date.today()
        target_year = today.year + (1 if today.month >= 6 else 0)
        account = clean_text(source.get("account_name") or source.get("name"), limit=160)
        return f"""
今天是 {today.isoformat()}。在公开网页中搜索与“{account}”相关的最新校园招聘内容，
重点寻找 {target_year} 届校招、应届生、Graduate、管培生和提前批机会。

要求：
1. articles 只能返回真实、可打开的公开 HTTPS 文章或公开招聘栏目链接；不得臆造 URL。
2. jobs 只返回当前仍开放的具体岗位；排除社招、实习、城市导航页、转载汇总页和已截止岗位。
3. jobs.official_url 必须是企业招聘官网或企业授权 ATS 的直接 HTTPS 页面，不能是公众号、搜索页或媒体转载。
4. 日期只有原文明确写明才填 YYYY-MM-DD，否则为 null；不得把发布日期当截止日期。
5. 找不到可靠内容时返回空数组，不要用常识补写。
""".strip()

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        if not self.api_key:
            raise RuntimeError("OpenAI API is not configured for public discovery.")
        response = OpenAI(api_key=self.api_key).responses.create(
            model=self.ai_model,
            tools=[{
                "type": "web_search",
                "search_context_size": "medium",
                "user_location": {
                    "type": "approximate",
                    "country": "CN",
                    "timezone": "Asia/Shanghai",
                },
            }],
            input=self._prompt(source),
            # Only one tool is supplied.  Requiring a tool call prevents the
            # model from returning plausible-looking JSON from parametric
            # memory while silently skipping public web discovery.
            tool_choice="required",
            text={"format": {
                "type": "json_schema",
                "name": "wechat_public_recruitment_discovery",
                "strict": True,
                "schema": WECHAT_DISCOVERY_SCHEMA,
            }},
            include=["web_search_call.action.sources"],
            max_tool_calls=2,
            max_output_tokens=2_400,
            store=False,
        )
        completed_search_calls = _completed_web_search_calls(response)
        if not completed_search_calls:
            # Do not parse or trust output_text unless the provider confirms a
            # completed web-search call.  Raising lets the service mark only
            # this source failed instead of recording a healthy fake scan.
            raise RuntimeError("OpenAI public web search did not complete.")
        search_source_urls = _web_search_source_urls(completed_search_calls)
        try:
            payload = json.loads(response.output_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenAI public web search returned invalid structured data.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI public web search returned invalid structured data.")
        publisher = _redact_public_text(
            source.get("account_name") or source.get("name"), limit=160
        )
        articles: list[dict[str, Any]] = []
        seen_article_urls: set[str] = set()
        for raw in payload.get("articles", [])[:8]:
            if not isinstance(raw, dict):
                continue
            url = _public_reference_url(raw.get("url"))
            title = _redact_public_text(raw.get("title"), limit=300)
            if (
                not url
                or not title
                or url in seen_article_urls
                or not _article_url_supported_by_search_sources(url, search_source_urls)
            ):
                continue
            excerpt = _redact_public_text(raw.get("excerpt"), limit=1_500)
            signal = f"{title} {excerpt}".casefold()
            is_recruitment = any(marker in signal for marker in (
                "校招", "校园招聘", "应届", "毕业生", "graduate", "campus", "管培", "提前批",
            ))
            year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", signal)
            articles.append({
                "publisher": publisher,
                "article_title": title,
                "article_url": url,
                "publish_time": normalize_date(raw.get("publish_date")),
                "raw_excerpt": excerpt,
                "is_recruitment": is_recruitment,
                "recruitment_year": int(year_match.group(1)) if year_match else None,
                "classification": "recruitment_signal" if is_recruitment else "other",
            })
            seen_article_urls.add(url)

        jobs: list[dict[str, Any]] = []
        verified_ids: set[str] = set()
        seen_job_ids: set[str] = set()
        for raw in payload.get("jobs", [])[:10]:
            if not isinstance(raw, dict):
                continue
            discovered = normalize_web_search_job(raw)
            if not discovered or discovered["id"] in seen_job_ids:
                continue
            evidence = _inspect_official_candidate_page(discovered)
            # A deterministic closed-page signal is terminal.  Temporary
            # fetch/readability failures are not: preserving the safe URL as a
            # pending candidate prevents transient ATS failures from silently
            # erasing a legitimate discovery signal.
            if evidence.closed:
                continue
            if evidence.readable:
                discovered["opening_date"] = (
                    discovered["opening_date"]
                    if _semantic_date_appears_in_page(
                        evidence.page_text,
                        discovered["opening_date"],
                        semantic="opening",
                    )
                    else None
                )
                discovered["closing_date"] = (
                    discovered["closing_date"]
                    if _semantic_date_appears_in_page(
                        evidence.page_text,
                        discovered["closing_date"],
                        semantic="closing",
                    )
                    else None
                )
            else:
                # Dates asserted by discovery output are never retained when
                # the official page could not be read and semantically checked.
                discovered["opening_date"] = None
                discovered["closing_date"] = None

            discovered["tags"] = [
                tag for tag in discovered["tags"]
                if tag not in {
                    "待官方核验", "待打开核对", "链接已验证", "标题已验证",
                    "链接可访问", "官方页暂不可读", "公众号公开发现",
                }
            ]
            if evidence.title_confirmed:
                discovered["tags"].extend(["链接已验证", "标题已验证", "公众号公开发现"])
                verified_ids.add(discovered["id"])
            elif evidence.readable:
                discovered["tags"].extend(["链接可访问", "公众号公开发现", "待官方核验"])
            else:
                discovered["tags"].extend([
                    "官方页暂不可读", "公众号公开发现", "待官方核验",
                ])
            jobs.append({
                "external_id": discovered["id"],
                "company": discovered["company"],
                "title": discovered["title"],
                "city": discovered.get("city", ""),
                "region": discovered.get("city", ""),
                "employer_type": discovered.get("employer_type", ""),
                "industry": discovered.get("industry", ""),
                "official_url": discovered["url"],
                "application_url": discovered["url"],
                "opening_date": discovered.get("opening_date"),
                "closing_date": discovered.get("closing_date"),
                "status": "open",
                "verification_status": "verified" if discovered["id"] in verified_ids else "pending",
                "confidence_score": 0.9 if discovered["id"] in verified_ids else 0.6,
                "requirements": discovered.get("requirements", ""),
                "tags": list(dict.fromkeys(discovered.get("tags", []))),
            })
            seen_job_ids.add(discovered["id"])

        digest = hashlib.sha256(json.dumps(
            {"articles": articles, "jobs": jobs}, ensure_ascii=False,
            sort_keys=True, default=str,
        ).encode("utf-8")).hexdigest()
        usage = getattr(response, "usage", None)
        return AdapterResult(
            jobs=jobs,
            articles=articles,
            content_hash=digest,
            snapshot_complete=False,
            status="healthy" if articles or jobs else "discovery_only",
            message=(
                "Public article discovery completed."
                if articles or jobs else "No new public recruitment signal was found."
            ),
            ai_calls=len(completed_search_calls),
            model_tokens_used=max(0, int(getattr(usage, "total_tokens", 0) or 0)),
            verified_job_external_ids=verified_ids,
        )


class ManualAdapter:
    def scan(self, source: dict[str, Any]) -> AdapterResult:
        del source
        return AdapterResult(status="idle", snapshot_complete=False, message="Push-only source.")


class PublicRecruitmentIndexAdapter:
    """Parse known public listings into article signals without AI claims."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        kind = clean_text(source.get("adapter_config", {}).get("discovery_kind"), limit=40)
        if kind == "sasac":
            batch = discover_sasac_recruitment_articles()
        elif kind == "bank":
            batch = discover_bank_recruitment_articles()
        else:
            raise ValueError("public_recruitment_index requires a supported discovery_kind")
        return AdapterResult(
            articles=batch.radar_articles(),
            content_hash=batch.content_hash,
            normalized_content=" ".join(
                article["article_title"] for article in batch.radar_articles()
            )[:20_000],
            snapshot_complete=False,
            status="healthy",
            message=f"Discovered {len(batch.articles)} public recruitment article signals.",
        )


def _legacy_primary_category(item: dict[str, Any], tags: list[Any]) -> str:
    """Use exact directory identities and metadata, never role/JD prose."""
    operator_category = telecom_primary_category(item.get("company"))
    if operator_category:
        return operator_category
    # The identity-aware helper also repairs old labels inferred from e.g.
    # '消费金融' in a bank's sector string. It never reads the role or JD.
    explicit = normalize_taxonomy_value(item.get("primary_category"))
    return primary_employer_category({
        "company": item.get("company"),
        "primary_category": explicit if explicit in PRIMARY_CATEGORY_CODES else "",
        "employer_type": item.get("employer_type"),
        "industry": item.get("industry"),
        "organization_category": item.get("organization_category"),
        "industry_tags": item.get("industry_tags"),
        "tags": tags,
    }) or ""


class LegacyDatabaseAdapter:
    """Moves verified legacy jobs into Radar without deleting the old API."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        if source.get("adapter_config", {}).get("discovery_only"):
            return LegacyDiscoveryDatabaseAdapter().scan(source)
        del source
        jobs: list[dict[str, Any]] = []
        for item in database.list_recruitment_jobs():
            tags = list(item.get("tags") or [])
            tag_values = {str(tag).strip() for tag in tags}
            pending = bool(
                {"待官方核验", "待打开核对"}.intersection(tag_values)
                or not {"链接已验证", "标题已验证"}.issubset(tag_values)
            )
            primary_category = _legacy_primary_category(item, tags)
            jobs.append({
                "external_id": item["id"],
                "company": item["company"],
                "title": item["title"],
                "city": item.get("city", ""),
                "region": item.get("city", ""),
                "employer_type": item.get("employer_type", ""),
                "industry": item.get("industry", ""),
                "primary_category": primary_category,
                "organization_category": item.get("organization_category", ""),
                "industry_tags": item.get("industry_tags") or [],
                "role_tags": item.get("role_tags") or [],
                "official_url": item.get("url") or None,
                "application_url": item.get("url") or None,
                "opening_date": item.get("opening_date"),
                "closing_date": item.get("closing_date"),
                "status": item.get("status", "open"),
                "verification_status": "pending" if pending else "verified",
                "confidence_score": 0.55 if pending else 0.95,
                "description": item.get("description", ""),
                "responsibilities": item.get("responsibilities", ""),
                "requirements": item.get("requirements", ""),
                "source_ratings": merge_source_ratings(item.get("source_rating")),
                "tags": [*tags, "legacy-compatible"],
            })
        content_hash = hashlib.sha256(json.dumps(
            jobs, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        return AdapterResult(jobs=jobs, content_hash=content_hash)


class LegacyDiscoveryDatabaseAdapter:
    """Expose old search/ingest observations without upgrading their trust.

    This is a deterministic projection of two local tables, not another web
    search.  It deliberately never selects conversation identifiers, evidence,
    source labels, payloads or verification diagnostics from the ingest table.
    Explicit source ratings use a separate bounded, validated provenance field.
    Stable identities align with the official legacy bridge so a later
    promotion adds provenance to one job instead of making a duplicate.
    """

    @staticmethod
    def _tags(value: Any) -> list[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = []
        if not isinstance(value, list):
            return []
        return [
            _redact_public_text(tag, limit=80)
            for tag in value
            if isinstance(tag, str)
        ][:30]

    @staticmethod
    def _safe_external_id(value: Any) -> str:
        value = str(value or "")
        if re.fullmatch(r"(?:web|monitor|candidate)-[0-9a-f]{24,64}", value):
            return value
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,179}", value)
            and not _PUBLIC_UUID.search(value)
            and not any(pattern.search(value) for pattern in _PUBLIC_SECRETS)
            and not any(pattern.search(value) for pattern in _PUBLIC_PHONES)
        ):
            return value
        return stable_digest(value, prefix="legacy-discovery", length=32)

    @classmethod
    def _ingest_external_id(cls, item: dict[str, Any]) -> str:
        if item.get("promoted_job_id"):
            return cls._safe_external_id(item["promoted_job_id"])
        # Match the legacy ingest endpoint's monitor identity before it is
        # promoted.  Raw external identifiers are used only as hash material.
        if item.get("controlled_chatgpt") and item.get("external_id"):
            company_key = re.sub(
                r"[^0-9a-z\u4e00-\u9fff]+", "", str(item["company"]).casefold()
            )
            identity = f"external:{company_key}:{str(item['external_id']).casefold()}"
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        else:
            digest = str(item.get("dedupe_key") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                digest = hashlib.sha256(str(item.get("id") or "").encode("utf-8")).hexdigest()
        return f"monitor-{digest[:24]}"

    @classmethod
    def _job(cls, item: dict[str, Any], *, external_id: str) -> dict[str, Any] | None:
        company = _redact_public_text(item.get("company"), limit=160)
        title = _redact_public_text(item.get("title"), limit=280)
        if not company or not title:
            return None
        closing_date = normalize_date(item.get("closing_date"))
        if (
            item.get("status") == "closed"
            or item.get("verification_status") == "closed"
            or (closing_date and closing_date <= date.today().isoformat())
        ):
            return None
        tags = cls._tags(item.get("tags"))
        # Source-provided "verified" tags must not contradict the candidate's
        # explicitly pending state in the new pool.
        tags = [tag for tag in tags if tag not in {"链接已验证", "标题已验证"}]
        verification = str(item.get("verification_status") or "pending")
        if verification not in {"rejected", "conflicted"}:
            verification = "pending"
        url = _public_reference_url(item.get("canonical_url") or item.get("url"))
        url = url or _public_reference_url(item.get("official_url"))
        rating = normalize_source_rating(item.get("source_rating"))
        if rating:
            rating["observed_at"] = item.get("last_seen_at") or item.get("last_verified_at")
        return {
            "external_id": external_id,
            "company": company,
            "title": title,
            "city": _redact_public_text(item.get("city"), limit=160),
            "region": _redact_public_text(item.get("city"), limit=160),
            "employer_type": _redact_public_text(item.get("employer_type"), limit=80),
            "industry": _redact_public_text(item.get("industry"), limit=120),
            "primary_category": _legacy_primary_category(item, tags),
            "official_url": url,
            "application_url": url,
            "opening_date": normalize_date(item.get("opening_date")),
            "closing_date": closing_date,
            "status": "unknown" if item.get("status") == "unknown" else "open",
            "verification_status": verification,
            "confidence_score": 0.5,
            "requirements": _redact_public_text(item.get("requirements"), limit=1_200),
            "source_ratings": merge_source_ratings(rating),
            "tags": list(dict.fromkeys([*tags, "历史搜索发现"])),
        }

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        config = source.get("adapter_config") or {}
        scoped = "candidate_ids" in config
        candidate_ids: list[str] = []
        if scoped:
            raw_ids = config["candidate_ids"]
            # Internal transport hint, never a query supplied by a source.
            # An empty/invalid scope must not silently become a full-pool scan.
            if (
                not isinstance(raw_ids, list)
                or len(raw_ids) > MAX_MONITOR_BATCH_ITEMS
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"candidate-[0-9a-f]{32}", value)
                    for value in raw_ids
                )
            ):
                raise ValueError(f"candidate_ids must contain at most {MAX_MONITOR_BATCH_ITEMS} internal candidate IDs.")
            candidate_ids = list(dict.fromkeys(raw_ids))
        legacy_rows = []
        ingest_rows = []
        if not scoped or candidate_ids:
            with database.connect() as connection:
                if not scoped:
                    legacy_rows = connection.execute(
                        """
                        SELECT id, company, employer_type, title, city, industry, url,
                               opening_date, closing_date, requirements, tags, source_rating, status,
                               last_verified_at
                        FROM recruitment_jobs
                        WHERE source=? OR tags LIKE '%AI网页搜索%'
                        ORDER BY id
                        """,
                        (WEB_SEARCH_SOURCE,),
                    ).fetchall()
                source_slots = ",".join("?" for _ in KNOWN_CHATGPT_SOURCE_IDS)
                ingest_query = f"""
                SELECT id, dedupe_key, external_id, promoted_job_id,
                       CASE WHEN source_id IN ({source_slots}) THEN 1 ELSE 0 END AS controlled_chatgpt,
                       company, employer_type, title, city, industry,
                       official_url, canonical_url, opening_date, closing_date,
                       requirements, tags, source_rating, incoming_status AS status,
                       verification_status, source_updated_at, last_seen_at
                FROM recruitment_ingest_candidates
                """
                if scoped:
                    placeholders = ",".join("?" for _ in candidate_ids)
                    ingest_query += f" WHERE id IN ({placeholders})"
                ingest_query += " ORDER BY id"
                ingest_rows = connection.execute(
                    ingest_query, (*sorted(KNOWN_CHATGPT_SOURCE_IDS), *candidate_ids),
                ).fetchall()

        jobs_by_id: dict[str, dict[str, Any]] = {}
        retired_job_external_ids: set[str] = set()
        cursors: list[str] = []
        for rows, is_ingest in ((legacy_rows, False), (ingest_rows, True)):
            for row in rows:
                item = dict(row)
                external_id = (
                    self._ingest_external_id(item)
                    if is_ingest else self._safe_external_id(item["id"])
                )
                deadline = normalize_date(item.get("closing_date"))
                explicitly_retired = scoped and is_ingest and (
                    item.get("status") == "closed"
                    or item.get("verification_status") in {"closed", "rejected"}
                    or bool(deadline and deadline <= date.today().isoformat())
                )
                job = None if explicitly_retired else self._job(item, external_id=external_id)
                if job:
                    job["source_ratings"] = merge_source_ratings(
                        jobs_by_id.get(external_id, {}).get("source_ratings"), job.get("source_ratings"),
                    )
                    jobs_by_id[external_id] = job
                    retired_job_external_ids.discard(external_id)
                elif is_ingest:
                    jobs_by_id.pop(external_id, None)
                    if explicitly_retired:
                        retired_job_external_ids.add(external_id)
                for field in ("source_updated_at", "last_seen_at", "last_verified_at"):
                    try:
                        timestamp = datetime.fromisoformat(str(item.get(field) or "").replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    cursors.append(timestamp.astimezone(timezone.utc).isoformat())
        jobs = [jobs_by_id[key] for key in sorted(jobs_by_id)]
        digest = hashlib.sha256(json.dumps(
            jobs, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        return AdapterResult(
            jobs=jobs,
            content_hash=digest,
            normalized_content=json.dumps({
                "kind": "legacy_search_discovery",
                "candidate_count": len(jobs),
                "observed_cursor": max(cursors, default=None),
            }, ensure_ascii=False, sort_keys=True),
            # Only a normal, unscoped Quick Scan reads the complete local pool.
            # An ingest bridge processes this batch only: missing older rows
            # are not evidence of closure and must retain their source links.
            snapshot_complete=not scoped,
            retired_job_external_ids=retired_job_external_ids,
        )


class OfficialHtmlAdapter:
    """Deterministically fingerprints a public page and can emit a program signal."""

    CAMPUS_MARKERS = ("校园招聘", "秋季招聘", "秋招", "校招", "应届", "graduate", "campus")

    def __init__(self, *, repository: RadarRepository, api_key: str, ai_model: str):
        self.repository = repository
        self.api_key = api_key
        self.ai_model = ai_model

    @staticmethod
    def _configured_markers(config: dict[str, Any], key: str) -> list[str]:
        value = config.get(key)
        if value in (None, []):
            return []
        if not isinstance(value, list):
            raise ValueError(f"official_html {key} must be an array of strings.")
        return [
            marker
            for item in value[:10]
            if (marker := clean_text(item, limit=280))
        ]

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        if not source.get("url"):
            raise DiscoveryLimitedError("Source URL is not configured.")
        domain = source.get("domain") or ""
        minimum = float(source.get("adapter_config", {}).get("domain_delay_seconds", 1.0))
        cancellation_check = source.get("adapter_config", {}).get("_cancellation_check")
        last_error: Exception | None = None
        page = None
        for attempt in range(3):
            try:
                check_discovery_cancellation(cancellation_check)
                DOMAIN_LIMITER.wait(domain, minimum)
                page = fetch_watch_page(
                    source["url"],
                    self.CAMPUS_MARKERS,
                    timeout_seconds=float(source.get("adapter_config", {}).get("timeout_seconds", 10)),
                    **({"allow_structured_body": True}
                       if source.get("adapter_config", {}).get("discover_job_links") else {}),
                )
                break
            except WatchFetchError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        if page is None:
            raise RuntimeError(str(last_error or "Public source fetch failed."))

        programs: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        config = source.get("adapter_config", {})
        company = clean_text(source.get("company"), limit=160)
        campus = bool(page.keyword_hits)
        year = int(config.get("recruitment_year", date.today().year + 1))
        required_markers = self._configured_markers(config, "required_markers")
        # Configured markers are normalized by clean_text().  Normalize the
        # fetched page the same way so literal official titles containing
        # full-width punctuation (for example Chinese parentheses) still match.
        folded_page = clean_text(
            page.text,
            # Do not let the presentation-oriented default truncate a long
            # official vacancy list before a configured marker near its end.
            limit=max(4_000, len(page.text) * 4),
        ).casefold()
        required_markers_present = all(
            marker.casefold() in folded_page for marker in required_markers
        )
        reference_url = canonicalize_url(source["url"], allow_empty=False)

        opening_date = normalize_date(config.get("opening_date"))
        closing_date = normalize_date(config.get("closing_date"))
        closed_markers = self._configured_markers(config, "closed_markers")
        today = date.today().isoformat()
        if opening_date and opening_date > today:
            listing_status = "unknown"
        elif (
            (closing_date and closing_date <= today)
            or any(marker.casefold() in folded_page for marker in closed_markers)
        ):
            listing_status = "closed"
        else:
            listing_status = "open"

        if campus and company and required_markers_present:
            programs.append({
                "company": company,
                "program_name": clean_text(config.get("program_name") or f"{year} 校园招聘"),
                "recruitment_year": year,
                "recruitment_type": config.get("recruitment_type", "campus"),
                "region": config.get("region", "中国"),
                "opening_date": opening_date,
                "closing_date": closing_date,
                "official_url": reference_url,
                "status": listing_status,
                "verification_status": "verified",
                "confidence_score": 0.9,
                "evidence": [
                    f"官方页面包含确定性标记：{required_markers[-1]}"
                ] if required_markers else [],
            })

        # Opt-in only: an actual current-year employer campaign is a useful
        # unscored opportunity, not an invented individual vacancy.  The main
        # pool already derives recruitment_program from these factual fields.
        if (
            programs and config.get("emit_program_listing")
            and _current_campus_campaign(page.text, year)
        ):
            jobs.append(_program_listing(programs[0], config))

        configured_jobs = config.get("configured_jobs")
        if configured_jobs not in (None, []) and not isinstance(configured_jobs, list):
            raise ValueError("official_html configured_jobs must be an array of objects.")
        if isinstance(configured_jobs, list):
            job_definitions = [item for item in configured_jobs[:50] if isinstance(item, dict)]
        elif config.get("job_title") or config.get("job_marker"):
            job_definitions = [config]
        else:
            job_definitions = []
        for definition in job_definitions:
            job_config = {**config, **definition}
            job_marker = clean_text(job_config.get("job_marker"), limit=280)
            job_title = clean_text(job_config.get("job_title"), limit=280)
            opening_date = normalize_date(job_config.get("opening_date"))
            job_closing_date = normalize_date(job_config.get("closing_date"))
            if not (
                company
                and required_markers_present
                and job_title
                and job_marker
                and job_marker.casefold() in folded_page
                and (not opening_date or opening_date <= today)
            ):
                continue
            application_url = reference_url
            if job_config.get("application_url"):
                application_url = _public_reference_url(job_config["application_url"])
                if not application_url:
                    logger.warning(
                        "Official source %s skipped a configured job with an unsafe application URL",
                        source["id"],
                    )
                    continue
            job_status = "closed" if (
                (job_closing_date and job_closing_date <= today)
                or any(marker.casefold() in folded_page for marker in self._configured_markers(
                    job_config, "closed_markers"
                ))
            ) else listing_status
            jobs.append({
                "external_id": clean_text(job_config.get("external_id"), limit=180) or None,
                "company": company,
                "title": job_title,
                "city": clean_text(job_config.get("city"), limit=160),
                "region": clean_text(job_config.get("region") or "中国", limit=160),
                "employer_type": clean_text(job_config.get("employer_type"), limit=80),
                "industry": clean_text(job_config.get("industry"), limit=120),
                "primary_category": clean_text(job_config.get("primary_category"), limit=80),
                "organization_category": clean_text(
                    job_config.get("organization_category"), limit=80
                ),
                "industry_tags": list(job_config.get("industry_tags") or [])[:30],
                "role_tags": list(job_config.get("role_tags") or [])[:30],
                "official_url": reference_url,
                "application_url": application_url,
                "opening_date": opening_date,
                "closing_date": job_closing_date,
                "status": job_status,
                "verification_status": "verified",
                "confidence_score": 0.95,
                "requirements": clean_text(job_config.get("requirements"), limit=8_000),
                "tags": ["校园招聘", "官方网页", "确定性解析"],
                "evidence": [f"官方页面逐字包含岗位名称：{job_marker}"],
            })

        ai_calls = 0
        model_tokens = 0
        if bool(config.get("ai_extract")):
            try:
                extracted = extract_recruitment_content(
                    repository=self.repository,
                    content=page.text,
                    content_hash=page.fingerprint,
                    source_url=page.final_url,
                    model=self.ai_model,
                    api_key=self.api_key,
                    force_refresh=bool(config.get("_force_refresh")),
                )
                programs.extend(extracted.get("programs", []))
                jobs.extend(extracted.get("jobs", []))
                ai_calls = 0 if extracted.get("cache_hit") else 1
                model_tokens = int(extracted.get("model_tokens_used", 0))
            except RuntimeError:
                logger.warning("AI extraction degraded for source %s", source["id"])

        coverage: dict[str, Any] = {}
        content_hash = page.fingerprint
        normalized_content = page.text[:20_000]
        snapshot_complete = True
        status = "healthy"
        if config.get("discover_job_links"):
            # A linked-list crawl is not an authoritative company snapshot;
            # missing pages must never retire previously collected vacancies.
            snapshot_complete = False
            aliases = {company}
            aliases.update(self._configured_markers(config, "employer_aliases"))
            for canonical, values in EMPLOYER_ALIAS_GROUPS.items():
                if company in (canonical, *values):
                    aliases.update((canonical, *values))
            target = EmployerSearchTarget(
                id=source["id"], canonical_name=company, aliases=tuple(sorted(aliases)),
                pool_id="", primary_category=config.get("primary_category", ""), pool_name="", focus="",
            )
            discovered = discover_official_job_pages(
                [reference_url], company=company, fetcher=fetch_watch_page,
                initial_pages={reference_url: page},
                max_listing_pages=int(config.get("max_listing_pages", 24)),
                max_detail_pages=int(config.get("max_detail_pages", 120)),
                max_seconds=float(config.get("max_scan_seconds", 90)),
                timeout_seconds=float(config.get("timeout_seconds", 10)),
                before_fetch=lambda host: DOMAIN_LIMITER.wait(host, minimum),
                cancellation_check=cancellation_check,
            )
            decisions: dict[str, int] = {}
            existing_keys = {(job.get("title"), job.get("official_url")) for job in jobs}
            for candidate in discovered.candidates:
                item = candidate.job
                if not _company_matches_target(item["company"], target):
                    reason = "employer_mismatch"
                elif item.get("posting_expired"):
                    reason = "official_posting_expired"
                elif not _operator_role_is_current(item["title"], candidate.page_text, year):
                    reason = "not_current_campus"
                else:
                    evidence = _evaluate_official_candidate_page(
                        {**item, "url": candidate.final_url, "_employer_aliases": list(aliases)},
                        candidate.page_text, candidate.final_url,
                    )
                    if evidence.closed:
                        reason = "official_page_closed"
                    elif not evidence.cohort_confirmed:
                        reason = "cohort_unconfirmed"
                    elif (item["title"], candidate.final_url) in existing_keys:
                        reason = "already_configured"
                    else:
                        reason = "official_verified" if evidence.title_confirmed else "official_pending"
                        jobs.append({
                            "external_id": stable_digest(source["id"], item["title"], item.get("city", ""), candidate.final_url, prefix="job"),
                            "company": item["company"], "title": item["title"], "city": item.get("city", ""),
                            "region": config.get("region", "中国"),
                            "employer_type": config.get("employer_type", ""), "industry": config.get("industry", ""),
                            "primary_category": config.get("primary_category", ""),
                            "official_url": candidate.final_url, "application_url": candidate.final_url,
                            "opening_date": None, "closing_date": None,
                            "status": "open" if evidence.open_confirmed else "unknown",
                            "verification_status": "verified" if evidence.title_confirmed else "pending",
                            "confidence_score": 0.95 if evidence.title_confirmed else 0.6,
                            "requirements": _redact_public_text(item.get("requirements", ""), limit=8_000),
                            "tags": ["校园招聘", "官方网页", "官网列表逐页发现"],
                            "evidence": ["来自实际公开职位链接；按公司、标题、届次与开放状态核验。"],
                        })
                        existing_keys.add((item["title"], candidate.final_url))
                decisions[reason] = decisions.get(reason, 0) + 1
            coverage = {**discovered.coverage, "candidate_decisions": decisions}
            status = "healthy" if discovered.coverage["status"] == "healthy" else "partial"
            content_hash = hashlib.sha256(f"{page.fingerprint}:{discovered.content_hash}".encode()).hexdigest()
            normalized_content = json.dumps({"kind": "official_link_discovery", "coverage": coverage}, ensure_ascii=False, sort_keys=True)

        return AdapterResult(
            programs=programs,
            jobs=jobs,
            content_hash=content_hash,
            normalized_content=normalized_content,
            snapshot_complete=snapshot_complete,
            status=status,
            coverage=coverage,
            ai_calls=ai_calls,
            model_tokens_used=model_tokens,
        )


def _current_campus_campaign(text: str, year: int) -> bool:
    return bool(re.search(
        rf"{year}\s*(?:届|年度|年)?\s*(?:全球|秋季|春季)?\s*(?:校园招聘|校招)",
        text,
    ))


def _program_listing(program: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    program_id = stable_program_external_id(program)
    operator_category = telecom_primary_category(program["company"])
    return {
        "external_id": stable_digest(program_id, "campaign", prefix="job"),
        "program_external_id": program_id,
        "company": program["company"],
        "title": clean_text(config.get("program_listing_title") or program["program_name"], limit=280),
        "region": program.get("region") or "中国",
        "city": "",
        "employer_type": config.get("employer_type") or ("央企科技通信" if operator_category else ""),
        "industry": config.get("industry") or ("通信运营" if operator_category else ""),
        "primary_category": config.get("primary_category") or operator_category,
        "official_url": program["official_url"],
        "application_url": program["official_url"],
        "opening_date": program.get("opening_date"),
        "closing_date": program.get("closing_date"),
        "status": program["status"],
        "verification_status": program["verification_status"],
        "confidence_score": program.get("confidence_score", 0.9),
        "requirements": _redact_public_text(
            f"{config.get('requirements') or ''} 这是企业整体校园招聘项目，不是一个具体岗位；具体单位、职位、名额与申请条件以官方岗位列表为准。",
            limit=8_000,
        ),
        "tags": ["校园招聘", str(program["recruitment_year"]), "招聘项目", "官方网页", "recruitment_program"],
        "evidence": program.get("evidence", []),
    }


def _operator_role_is_current(title: str, details: str, year: int) -> bool:
    # Experience requirements may mention past internships. Only explicit
    # internship/social recruitment titles are rejected on that basis.
    if re.search(r"实习|社会招聘|社招|博士后|\bintern(?:ship)?\b|postdoc", title, re.I):
        return False
    cohorts = re.findall(r"(20\d{2})\s*(?:届|年度(?:秋季|春季)?校园招聘)", f"{title} {details}")
    if cohorts and str(year) not in cohorts:
        return False
    # Several official ATS lists retain old rows even after changing their
    # campaign banner. A precise old graduation window must not be relabelled
    # with the new group-wide year. Do not confuse publication/founding dates
    # or "internship experience preferred" with eligibility.
    windows = [part for part in re.split(r"[。；;\n]", details) if re.search(
        r"(?:毕业时间|毕业于|毕业日期).*20\d{2}年|20\d{2}年.{0,70}毕业", part
    )]
    explicit_current_cohort = bool(re.search(rf"{year}\s*届", details))
    for window in windows:
        years = [int(value) for value in re.findall(r"(20\d{2})年", window)]
        if years and not min(years) <= year <= max(years):
            if explicit_current_cohort and re.search(r"未就业|择业期|也可|亦可|也接受", window):
                continue
            return False
    return True


def _operator_result(
    programs: list[dict[str, Any]], jobs: list[dict[str, Any]], *,
    pages: int, observed: int, skipped: int, complete: bool, reason: str = "", total: int | None = None,
    parse_failures: int = 0,
) -> AdapterResult:
    # Persist only public recruitment fields, never the ATS staff/recruiter
    # profile, anonymous session HTML, scripts or dynamic signing parameters.
    summary = {"pages": pages, "observed": observed, "jobs": len(jobs), "skipped": skipped,
               "reported_total": total, "complete": complete, "reason": reason,
               "parse_failures": parse_failures}
    digest = hashlib.sha256(json.dumps(
        {"programs": programs, "jobs": jobs}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()
    return AdapterResult(
        programs=programs, jobs=jobs, content_hash=digest,
        normalized_content=json.dumps(summary, ensure_ascii=False, sort_keys=True),
        snapshot_complete=complete, status="healthy" if complete else "partial",
        message=f"Official campus list: {observed} rows, {len(jobs)} opportunities, {pages} pages. {reason}",
    )


class _NoPublicApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WatchFetchError("Public ATS API redirected; no request was forwarded.")


_HOTJOB_SUITE_KEY = re.compile(r"SU[0-9a-f]{24}", re.IGNORECASE)
_HOTJOB_POST_ID = re.compile(r"[0-9a-f]{24}", re.IGNORECASE)


def _hotjob_public_request(
    suite_key: str,
    route: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    referer: str,
) -> dict[str, Any]:
    """Call one fixed, read-only Hotjob public recruitment route.

    The adapter never forwards browser state, cookies or authorization.  Both
    the tenant key and route are locally constrained before the URL is built,
    and redirects are rejected so an ATS configuration change cannot turn a
    scan into a request to an unrelated host.
    """
    if not _HOTJOB_SUITE_KEY.fullmatch(suite_key):
        raise WatchFetchError("Hotjob suite key is invalid.")
    if route not in {"config/get", "positionInfo/listPosition", "positionInfo/listPositionDetail"}:
        raise WatchFetchError("Hotjob public route is not allowed.")
    endpoint = validate_public_https_url(
        f"https://wecruit.hotjob.cn/wecruit/{route}/{suite_key}"
        "?iSaJAx=isAjax&request_locale=zh_CN"
    )
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "User-Agent": "FrostFire-Recruitment-Watch/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Referer": referer,
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoPublicApiRedirect()).open(
            request, timeout=timeout,
        ) as response:
            final = validate_public_https_url(response.geturl())
            if final != endpoint or response.headers.get_content_type() != "application/json":
                raise WatchFetchError("Unexpected Hotjob public ATS response.")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise WatchFetchError("Hotjob public ATS response exceeded the size limit.")
            result = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise WatchFetchError(f"Hotjob public ATS returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WatchFetchError("Hotjob public ATS response is unavailable or invalid.") from exc
    if not isinstance(result, dict) or str(result.get("state")) != "200" or not isinstance(result.get("data"), dict):
        raise WatchFetchError("Hotjob public ATS did not confirm a successful response.")
    return result["data"]


def _hotjob_public_date(value: Any) -> str | None:
    raw = clean_text(value, limit=40)
    # Public Hotjob payloads commonly include a time component even though the
    # product only exposes a calendar deadline.  Parse the leading public date
    # without interpreting it as an opening timestamp or local-time assertion.
    match = re.match(r"^(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw)
    candidate = normalize_date("-".join(match.groups())) if match else normalize_date(raw)
    if not candidate:
        return None
    # Hotjob uses 3000-01-01 as an internal no-deadline sentinel.  It is not a
    # factual application deadline and must never appear in the public pool.
    if int(candidate[:4]) > 2100:
        return None
    return candidate


def _hotjob_hiring_entity(root_company: str, unit: Any) -> str:
    name = _redact_public_text(unit, limit=160)
    if not name:
        return root_company
    # A legal subsidiary remains the actual employer.  An internal department,
    # branch or committee is qualified by the root brand so downstream hierarchy
    # scoring can distinguish headquarters, provincial branches and outlets.
    if re.search(r"(?:股份有限公司|有限责任公司|有限公司)$", name):
        return name
    if name.startswith(root_company):
        return name
    return clean_text(f"{root_company}{name}", limit=160)


def _hotjob_target_cohort(item: dict[str, Any], year: int) -> bool:
    text = " ".join(clean_text(item.get(key), limit=2_000) for key in (
        "postName", "projectName", "subject", "workContent",
    ))
    return bool(
        re.search(rf"(?<!\d){year}\s*届(?!\d)", text)
        or re.search(
            rf"(?<!\d){year}\s*年(?:度)?\s*(?:校园招聘|校招|应届(?:生|毕业生)?)(?!\d)",
            text,
        )
    )


_HOTJOB_PROGRAM_ONLY = re.compile(
    r"挑战赛|竞赛|大赛|训练营|开放日|宣讲会|"
    r"\b(?:challenge|competition|contest|open\s+day)\b",
    re.IGNORECASE,
)


def _hotjob_public_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    raw = clean_text(value, limit=20).casefold()
    if raw in {"true", "yes", "1", "open", "enabled"}:
        return True
    if raw in {"false", "no", "0", "closed", "disabled"}:
        return False
    return None


def _hotjob_delivery_status(item: dict[str, Any], closing_date: str | None) -> str:
    signals = [
        _hotjob_public_bool(item.get("canDelivery")),
        _hotjob_public_bool(item.get("showDeliverButton")),
    ]
    if False in signals or (closing_date and closing_date < date.today().isoformat()):
        return "closed"
    if True in signals or (closing_date and closing_date >= date.today().isoformat()):
        return "open"
    return "unknown"


class HotjobCampusAdapter:
    """Paginate a configured employer's public Hotjob campus ATS.

    Only rows that explicitly name the configured graduate cohort are emitted.
    A campus channel or a recent publish timestamp alone is not cohort evidence.
    Each retained row is re-read from the public detail API and linked to its
    independent, browser-openable detail page.
    """

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        config = source.get("adapter_config", {})
        suite_key = clean_text(config.get("suite_key"), limit=80)
        source_url = _public_reference_url(source.get("url"))
        if not source_url or not _HOTJOB_SUITE_KEY.fullmatch(suite_key):
            raise DiscoveryLimitedError("A valid public Hotjob campus source is not configured.")
        parsed = urllib.parse.urlsplit(source_url)
        if parsed.hostname != "wecruit.hotjob.cn" or not parsed.path.startswith(f"/{suite_key}/"):
            raise WatchFetchError("Hotjob source URL and configured tenant do not match.")

        company = clean_text(source.get("company"), limit=160)
        if not company:
            raise WatchFetchError("Hotjob source company is missing.")
        aliases = tuple(dict.fromkeys((
            company,
            *[
                clean_text(value, limit=160)
                for value in config.get("employer_aliases", [])[:20]
                if clean_text(value, limit=160)
            ],
        )))
        category = normalize_taxonomy_value(
            config.get("primary_category")
            or "securities_public_funds_asset_management"
        )
        if category not in PRIMARY_CATEGORY_CODES:
            raise WatchFetchError("Hotjob source category is invalid.")
        target = EmployerSearchTarget(
            id=source["id"], canonical_name=company, aliases=aliases,
            pool_id=clean_text(config.get("pool_id") or "hotjob_public", limit=80),
            primary_category=category,
            pool_name=clean_text(config.get("pool_name") or source.get("name"), limit=160),
            focus="",
        )
        timeout = min(25.0, max(2.0, float(config.get("timeout_seconds", 12))))
        delay = max(0.25, float(config.get("domain_delay_seconds", 1)))
        cancellation_check = config.get("_cancellation_check")

        DOMAIN_LIMITER.wait("wecruit.hotjob.cn", delay)
        tenant = _hotjob_public_request(
            suite_key, "config/get", {}, timeout=timeout, referer=source_url,
        )
        tenant_config = tenant.get("config") if isinstance(tenant.get("config"), dict) else {}
        tenant_company = clean_text(tenant_config.get("companyName"), limit=160)
        if not tenant_company or not _company_matches_target(tenant_company, target):
            raise WatchFetchError("Hotjob tenant identity does not match the configured employer.")

        year = int(config.get("recruitment_year", date.today().year + 1))
        max_pages = min(100, max(1, int(config.get("max_pages", 30))))
        max_details = min(300, max(1, int(config.get("max_details", 120))))
        deadline = time.monotonic() + min(
            600.0, max(20.0, float(config.get("max_scan_seconds", 180)))
        )
        page_number = 1
        pages_scanned = 0
        expected_pages: int | None = None
        expected_total: int | None = None
        observed = 0
        matched_rows: list[dict[str, Any]] = []
        seen_post_ids: set[str] = set()
        complete = True
        reason = ""

        while page_number <= max_pages:
            check_discovery_cancellation(cancellation_check)
            if time.monotonic() >= deadline:
                complete, reason = False, "Hotjob scan reached its source time budget."
                break
            DOMAIN_LIMITER.wait("wecruit.hotjob.cn", delay)
            data = _hotjob_public_request(
                suite_key,
                "positionInfo/listPosition",
                {"isFrompb": "true", "recruitType": 1, "pageSize": 15, "currentPage": page_number},
                timeout=min(timeout, max(2.0, deadline - time.monotonic())),
                referer=source_url,
            )
            page_form = data.get("pageForm")
            if not isinstance(page_form, dict) or not isinstance(page_form.get("pageData"), list):
                raise WatchFetchError("Hotjob campus list schema changed.")
            try:
                total_pages = int(page_form.get("totalPage", 0))
                current_page = int(page_form.get("currentPage", 0))
                data_count = int(page_form.get("dataCount", 0))
            except (TypeError, ValueError) as exc:
                raise WatchFetchError("Hotjob campus pagination is invalid.") from exc
            if current_page != page_number:
                complete, reason = False, "Hotjob campus returned an unexpected page number."
                break
            if total_pages < 0 or data_count < 0:
                complete, reason = False, "Hotjob campus pagination totals are invalid."
                break
            if (
                (expected_pages is not None and expected_pages != total_pages)
                or (expected_total is not None and expected_total != data_count)
            ):
                complete, reason = False, "Hotjob campus list changed during pagination."
            expected_pages = total_pages
            expected_total = data_count
            rows = page_form["pageData"]
            pages_scanned += 1
            observed += len(rows)
            for raw in rows:
                if not isinstance(raw, dict) or str(raw.get("recruitType")) != "1":
                    complete = False
                    reason = reason or "Hotjob campus returned an invalid campus row."
                    continue
                post_id = clean_text(raw.get("postId"), limit=80)
                if not _HOTJOB_POST_ID.fullmatch(post_id) or post_id in seen_post_ids:
                    complete = False
                    reason = reason or "Hotjob campus returned an invalid or duplicate position id."
                    continue
                seen_post_ids.add(post_id)
                if not _hotjob_target_cohort(raw, year):
                    continue
                matched_rows.append(raw)
            if total_pages == 0 or page_number >= total_pages:
                break
            if not rows:
                complete, reason = False, "Hotjob pagination stopped before the final page."
                break
            page_number += 1
        else:
            if expected_pages and page_number <= expected_pages:
                complete, reason = False, "Hotjob scan reached its page budget."

        if expected_total is not None and (
            observed != expected_total or len(seen_post_ids) != expected_total
        ):
            complete = False
            reason = reason or "Hotjob campus row count did not match its pagination total."

        jobs: list[dict[str, Any]] = []
        detail_failures = 0
        verified_details: list[dict[str, Any]] = []
        for raw in matched_rows[:max_details]:
            check_discovery_cancellation(cancellation_check)
            post_id = clean_text(raw.get("postId"), limit=80)
            title = _redact_public_text(raw.get("postName"), limit=280)
            if not _HOTJOB_POST_ID.fullmatch(post_id) or not title:
                detail_failures += 1
                continue
            if time.monotonic() >= deadline:
                complete, reason = False, "Hotjob scan reached its detail time budget."
                break
            try:
                DOMAIN_LIMITER.wait("wecruit.hotjob.cn", delay)
                detail = _hotjob_public_request(
                    suite_key,
                    "positionInfo/listPositionDetail",
                    {"postId": post_id},
                    timeout=min(timeout, max(2.0, deadline - time.monotonic())),
                    referer=source_url,
                )
            except WatchFetchError:
                detail_failures += 1
                continue
            detail_title = _redact_public_text(detail.get("postName"), limit=280)
            if (
                clean_text(detail.get("postId"), limit=80) != post_id
                or str(detail.get("recruitType")) != "1"
                or detail_title != title
                or not _hotjob_target_cohort(detail, year)
            ):
                detail_failures += 1
                continue

            closing_date = _hotjob_public_date(detail.get("endDate"))
            status = _hotjob_delivery_status(detail, closing_date)
            detail_url = canonicalize_url(
                f"https://wecruit.hotjob.cn/{suite_key}/pb/posDetail.html?"
                + urllib.parse.urlencode({"postId": post_id}),
                allow_empty=False,
            )
            department = _redact_public_text(detail.get("department"), limit=160)
            education = _redact_public_text(detail.get("education"), limit=160)
            subject = _redact_public_text(detail.get("subject"), limit=2_000)
            requirements = "；".join(value for value in (education, subject) if value)
            if department:
                requirements = clean_text(f"招聘部门：{department}。{requirements}", limit=8_000)
            semantic_detail = {
                "external_id": f"hotjob-{suite_key.casefold()}-{post_id.casefold()}",
                "company": _hotjob_hiring_entity(company, detail.get("company")),
                "title": title,
                "city": _redact_public_text(detail.get("workPlaceStr"), limit=160),
                "region": "中国",
                "employer_type": clean_text(
                    config.get("employer_type") or "券商/公募/资管", limit=80,
                ),
                "industry": clean_text(config.get("industry") or "证券", limit=120),
                "primary_category": category,
                "official_url": detail_url,
                "application_url": detail_url,
                # publishDate is publication metadata, not an asserted opening date.
                "opening_date": None,
                "closing_date": closing_date,
                "status": status,
                "verification_status": "verified",
                "confidence_score": 0.98,
                "description": _redact_public_text(detail.get("serviceCondition"), limit=8_000),
                "responsibilities": _redact_public_text(detail.get("workContent"), limit=8_000),
                "requirements": requirements,
                "tags": ["校园招聘", str(year), "官方 ATS", "确定性解析"],
                "evidence": [f"官方 ATS 当前校招列表与详情均明确标注 {year} 届及该岗位名称。"],
            }
            verified_details.append(semantic_detail)
            # Recruitment contests and open days are valid campaign signals,
            # but they are not employment vacancies and must not manufacture
            # high-tier jobs in the public opportunity pool.
            if not _HOTJOB_PROGRAM_ONLY.search(
                f"{detail_title} {clean_text(detail.get('projectName'), limit=300)}"
            ):
                jobs.append(semantic_detail)

        if len(matched_rows) > max_details:
            complete, reason = False, "Hotjob scan reached its detail-item budget."
        if detail_failures:
            complete = False
            reason = reason or f"{detail_failures} Hotjob detail rows could not be verified."

        programs = []
        if verified_details:
            detail_statuses = {item["status"] for item in verified_details}
            complete_detail_snapshot = bool(
                complete
                and not detail_failures
                and len(verified_details) == len(matched_rows)
            )
            program_status = (
                "open" if "open" in detail_statuses
                else "closed" if complete_detail_snapshot and detail_statuses == {"closed"}
                else "unknown"
            )
            detail_deadlines = [
                item["closing_date"] for item in verified_details if item.get("closing_date")
            ]
            program_deadline = (
                max(detail_deadlines)
                if complete_detail_snapshot and len(detail_deadlines) == len(verified_details)
                else None
            )
            programs.append({
                "external_id": f"hotjob-{suite_key.casefold()}-{year}-campus",
                "company": company,
                "program_name": clean_text(config.get("program_name") or f"{year} 届校园招聘"),
                "recruitment_year": year,
                "recruitment_type": "campus",
                "region": "中国",
                "opening_date": None,
                "closing_date": program_deadline,
                "official_url": source_url,
                "status": program_status,
                "verification_status": "verified",
                "confidence_score": 0.98,
                "evidence": [f"官方 ATS 校招列表存在明确标注 {year} 届的岗位。"],
            })
            program_external_id = programs[0]["external_id"]
            for job in jobs:
                job["program_external_id"] = program_external_id

        normalized = json.dumps({
            "tenant": tenant_company,
            "year": year,
            # Only validated, already-redacted public fields enter snapshots.
            # Include detail semantics so a responsibility/location/delivery
            # change invalidates the source hash even when the list is stable.
            "programs": programs,
            "jobs": jobs,
            "verified_detail_ids": [item["external_id"] for item in verified_details],
        }, ensure_ascii=False, sort_keys=True)
        return AdapterResult(
            programs=programs,
            jobs=jobs,
            content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            normalized_content=normalized,
            snapshot_complete=complete,
            status="healthy" if complete else "partial",
            message=reason,
            coverage={
                "pages_scanned": pages_scanned,
                "rows_observed": observed,
                "target_year_rows": len(matched_rows),
                "verified_jobs": len(jobs),
                "program_only_rows": len(verified_details) - len(jobs),
                "detail_failures": detail_failures,
                "reported_total": expected_total,
            },
        )


_CITICS_API_ENDPOINT = "https://global-kong.citics.com/api/v1/recruit/getPositionList"
_CITICS_PUBLIC_PAGE = "https://careers.citics.com/campus/headquarters/"
_CITICS_POSITION_ID = re.compile(r"\d{1,12}")


def _citics_public_positions(page_number: int, page_size: int, timeout: float) -> dict[str, Any]:
    """Read one fixed page of CITIC Securities' public campus API.

    The request is deliberately independent of browser state: it contains no
    Cookie, authorization header or configurable destination.  Redirects are
    rejected before any request can be forwarded to another host.
    """
    if page_number < 1 or page_number > 4 or page_size != 50:
        raise WatchFetchError("CITIC Securities pagination is outside its safety bounds.")
    endpoint = validate_public_https_url(_CITICS_API_ENDPOINT)
    payload = {
        "sysNo": "CSE001", "recruitType": "08", "deptype": "Headquarter",
        "batchId": "63", "practice": "0", "pageSize": str(page_size),
        "pageNo": str(page_number),
    }
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "User-Agent": "FrostFire-Recruitment-Watch/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Referer": _CITICS_PUBLIC_PAGE,
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoPublicApiRedirect()).open(
            request, timeout=timeout,
        ) as response:
            final = validate_public_https_url(response.geturl())
            if final != endpoint or response.headers.get_content_type() != "application/json":
                raise WatchFetchError("Unexpected CITIC Securities public API response.")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise WatchFetchError("CITIC Securities response exceeded the size limit.")
            result = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise WatchFetchError(
            f"CITIC Securities public API returned HTTP {exc.code}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WatchFetchError("CITIC Securities public API is unavailable or invalid.") from exc
    if (
        not isinstance(result, dict)
        or result.get("errorCode") != 0
        or not isinstance(result.get("positionList"), list)
    ):
        raise WatchFetchError("CITIC Securities public API did not confirm success.")
    return result


def _citics_heading_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", clean_text(value, limit=160).casefold())


def _citics_selected_responsibilities(position_name: str, value: Any) -> str:
    """Keep only the named direction from a compound department description."""
    raw = _redact_public_text(value, limit=30_000)
    headings = list(re.finditer(
        r"<b>\s*(?:\d+\s*[.、．]\s*)?([^<]{1,100}?)\s*</b>", raw,
        flags=re.IGNORECASE,
    ))
    if headings:
        target = _citics_heading_key(position_name)
        for index, match in enumerate(headings):
            if _citics_heading_key(match.group(1)) != target:
                continue
            end = headings[index + 1].start() if index + 1 < len(headings) else len(raw)
            selected = re.sub(r"<[^>]{1,80}>", " ", raw[match.end():end])
            selected = re.sub(r"^[\s:：;；、.-]+", "", selected)
            return clean_text(f"{position_name}：{selected}", limit=8_000)
        # A structured compound description without the requested heading is
        # ambiguous.  Do not leak the other directions into this job's score.
        return ""
    plain = clean_text(re.sub(r"<[^>]{1,80}>", " ", raw), limit=8_000)
    lines = plain.splitlines()
    if len(lines) > 1 and re.search(r"岗位.{0,20}方向", lines[0]):
        plain = clean_text(" ".join(lines[1:]), limit=8_000)
    return plain


def _citics_hiring_entity(responsibilities: str) -> str:
    """Use an explicitly named CITIC legal subsidiary as the real employer."""
    match = re.search(
        r"(中信[0-9A-Za-z\u4e00-\u9fff()（）]{1,45}(?:股份有限公司|有限责任公司|有限公司))岗位",
        responsibilities,
    )
    return clean_text(match.group(1), limit=160) if match else "中信证券总部"


class CiticsHeadquartersCampusAdapter:
    """Deterministically import the official 2027 headquarters vacancy list."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        config = source.get("adapter_config", {})
        if clean_text(source.get("company"), limit=160) != "中信证券":
            raise WatchFetchError("CITIC Securities source employer is invalid.")
        if _public_reference_url(source.get("url")) != canonicalize_url(
            _CITICS_PUBLIC_PAGE, allow_empty=False,
        ):
            raise WatchFetchError("CITIC Securities source page is invalid.")
        if int(config.get("recruitment_year", 0)) != 2027:
            raise WatchFetchError("CITIC Securities recruitment cohort is invalid.")
        timeout = min(25.0, max(2.0, float(config.get("timeout_seconds", 12))))
        page_size = 50
        expected_total: int | None = None
        pages = 0
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for page_number in range(1, 5):
            check_discovery_cancellation(config.get("_cancellation_check"))
            DOMAIN_LIMITER.wait("global-kong.citics.com", max(
                0.25, float(config.get("domain_delay_seconds", 0.5)),
            ))
            response = _citics_public_positions(page_number, page_size, timeout)
            try:
                total = int(response.get("count"))
            except (TypeError, ValueError) as exc:
                raise WatchFetchError("CITIC Securities result count is invalid.") from exc
            if total < 0 or total > 200 or (expected_total is not None and total != expected_total):
                raise WatchFetchError("CITIC Securities result count changed or exceeded bounds.")
            expected_total = total
            page_rows = response["positionList"]
            pages += 1
            for item in page_rows:
                if not isinstance(item, dict):
                    raise WatchFetchError("CITIC Securities returned an invalid vacancy row.")
                position_id = clean_text(item.get("positionNo"), limit=32)
                title = _redact_public_text(item.get("positionName"), limit=160)
                department = _redact_public_text(item.get("deptName"), limit=160)
                if (
                    not _CITICS_POSITION_ID.fullmatch(position_id)
                    or position_id in seen_ids
                    or item.get("batchId") != 63
                    or not title
                    or not department
                ):
                    raise WatchFetchError("CITIC Securities vacancy identity is invalid.")
                seen_ids.add(position_id)
                rows.append(item)
            if len(rows) >= total:
                break
            if not page_rows:
                raise WatchFetchError("CITIC Securities pagination ended early.")
        if expected_total is None or len(rows) != expected_total:
            raise WatchFetchError("CITIC Securities vacancy count did not match pagination.")

        program_id = "citics-headquarters-campus-2027"
        jobs = []
        for item in rows:
            position_id = clean_text(item["positionNo"], limit=32)
            department = _redact_public_text(item["deptName"], limit=160)
            position_name = _redact_public_text(item["positionName"], limit=160)
            responsibilities = _citics_selected_responsibilities(
                position_name, item.get("positionDesc"),
            )
            jobs.append({
                "external_id": f"citics-headquarters-2027-{position_id}",
                "program_external_id": program_id,
                "company": _citics_hiring_entity(responsibilities),
                "title": f"{department}｜{position_name}",
                "city": _redact_public_text(item.get("workplace"), limit=160),
                "region": "中国",
                "employer_type": "券商/公募/资管",
                "industry": "证券",
                "primary_category": "securities_public_funds_asset_management",
                "official_url": _CITICS_PUBLIC_PAGE,
                "application_url": _CITICS_PUBLIC_PAGE,
                "opening_date": None,
                "closing_date": None,
                "status": "open",
                "verification_status": "verified",
                "confidence_score": 0.99,
                "responsibilities": responsibilities,
                "requirements": _redact_public_text(item.get("qualification"), limit=8_000),
                "tags": ["校园招聘", "2027", "总部", "官方 API", "确定性解析"],
                "evidence": ["中信证券官方 2027 总部校园招聘接口当前列出该部门与岗位方向。"],
            })
        programs = [{
            "external_id": program_id,
            "company": "中信证券总部",
            "program_name": "中信证券 2027 总部校园招聘",
            "recruitment_year": 2027,
            "recruitment_type": "campus",
            "region": "中国",
            "opening_date": None,
            "closing_date": None,
            "official_url": _CITICS_PUBLIC_PAGE,
            "status": "open",
            "verification_status": "verified",
            "confidence_score": 0.99,
            "evidence": ["中信证券官方招聘网站当前公开 2027 总部岗位列表。"],
        }]
        normalized = json.dumps(
            {"programs": programs, "jobs": jobs}, ensure_ascii=False, sort_keys=True,
        )
        return AdapterResult(
            programs=programs,
            jobs=jobs,
            content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            normalized_content=normalized,
            snapshot_complete=True,
            status="healthy",
            coverage={"pages_scanned": pages, "rows_observed": len(rows), "verified_jobs": len(jobs)},
        )


def _unicom_public_jobs_page(page_number: int, page_size: int, timeout: float) -> dict[str, Any]:
    """The read-only list request used by zglt.zhaopin.com/scjobs/index.html.

    Fixed public route and fixed employer org, no Cookie, login or AI. TLS,
    DNS/private-network validation, response bounds and website limits apply.
    """
    endpoint = validate_public_https_url("https://fe.zhaopin.com/grace/api/dsc/search-job-list")
    payload = {
        "pageIndex": page_number, "pageSize": page_size, "orgNumbers": ["105347"],
        "jobSource": 2, "orgDepartmentIds": [], "workRegionIds": "", "jobTypes": "",
        "priorityMajors": "", "customTags": "", "campusParentDepartmentIds": "",
    }
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={
        "User-Agent": "FrostFire-Recruitment-Watch/1.0", "Content-Type": "application/json",
        "Accept": "application/json", "Referer": "https://zglt.zhaopin.com/",
    }, method="POST")
    try:
        with urllib.request.build_opener(_NoPublicApiRedirect()).open(request, timeout=timeout) as response:
            final = validate_public_https_url(response.geturl())
            if final != endpoint or response.headers.get_content_type() != "application/json":
                raise WatchFetchError("Unexpected public ATS response.")
            raw = response.read(1_500_001)
            if len(raw) > 1_500_000:
                raise WatchFetchError("Public ATS response exceeded the page size limit.")
            result = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise WatchFetchError(f"Public ATS returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WatchFetchError("Public ATS response is unavailable or invalid.") from exc
    if not isinstance(result, dict) or result.get("code") != 200 or not isinstance(result.get("data"), dict):
        raise WatchFetchError("Public ATS did not confirm a successful list response.")
    return result["data"]


class ChinaUnicomCampusAdapter:
    """Paginate the real public Zhaopin campus list, restricted to China Unicom."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        config = source.get("adapter_config", {})
        year = int(config.get("recruitment_year", date.today().year + 1))
        campus_url = "https://zglt.zhaopin.com/home/index.html"
        page = fetch_watch_page(campus_url, OfficialHtmlAdapter.CAMPUS_MARKERS)
        if not _current_campus_campaign(page.text, year) or "中国联合网络通信" not in page.text:
            raise WatchFetchError("China Unicom campaign page no longer confirms the configured campus year.")
        program = {
            "company": source["company"], "program_name": f"{year} 校园招聘",
            "recruitment_year": year, "recruitment_type": "campus", "region": "中国",
            "opening_date": None, "closing_date": None, "official_url": campus_url,
            "status": "open", "verification_status": "verified", "confidence_score": 0.95,
            "evidence": [f"中国联通官方招聘站标题明确为{year}校园招聘；岗位来自该站公开校招列表。"],
        }
        jobs = [_program_listing(program, config)]
        seen: set[str] = set()
        observed = skipped = pages = 0
        total = None
        complete = False
        reason = ""
        deadline = time.monotonic() + min(1_200.0, max(30.0, float(config.get("max_scan_seconds", 600))))
        max_pages = min(500, max(1, int(config.get("max_pages", 200))))
        for number in range(1, max_pages + 1):
            if time.monotonic() >= deadline:
                reason = "Source time budget reached; snapshot remains incomplete."
                break
            DOMAIN_LIMITER.wait("fe.zhaopin.com", max(1.0, float(config.get("domain_delay_seconds", 1))))
            try:
                data = _unicom_public_jobs_page(number, 50, min(20.0, max(1.0, deadline - time.monotonic())))
                rows = data.get("jobList")
                info = data.get("pageInfo") or {}
                if not isinstance(rows, list) or not isinstance(info, dict):
                    raise WatchFetchError("Public ATS list/page metadata is invalid.")
                page_total = int(info["totalNum"])
                total_pages = int(info["totalPage"])
                if page_total < 0 or total_pages < 0 or int(info.get("pageIndex", number)) != number:
                    raise WatchFetchError("Public ATS pagination metadata is inconsistent.")
            except (WatchFetchError, ValueError, KeyError, TypeError) as exc:
                reason = f"Page {number} failed ({type(exc).__name__}); retained successful pages."
                break
            pages += 1
            if total is not None and total != page_total:
                reason = "The public list changed during pagination; closure checks are disabled."
            total = page_total
            page_new = 0
            for row in rows:
                observed += 1
                if not isinstance(row, dict) or not isinstance(row.get("job"), dict) or not isinstance(row.get("company"), dict):
                    skipped += 1
                    reason = reason or "Some public list rows could not be parsed."
                    continue
                job, company = row["job"], row["company"]
                identifier = clean_text(job.get("jobNumber"), limit=100)
                title = _redact_public_text(job.get("title"), limit=280)
                url = _public_reference_url(job.get("url"))
                if (
                    not re.fullmatch(r"CC\d{6,16}J\d{6,16}", identifier)
                    or not url or urllib.parse.urlsplit(url).hostname != "xiaoyuan.zhaopin.com"
                    or urllib.parse.urlsplit(url).path != f"/job/{identifier}"
                    or not title or str(company.get("companyId")) != "105347"
                    or str(job.get("positionSourceType")) != "2"
                ):
                    skipped += 1
                    reason = reason or "Some public list identities/URLs were invalid."
                    continue
                if identifier in seen:
                    reason = reason or "Duplicate rows appeared while paginating; closure checks are disabled."
                    continue
                seen.add(identifier)
                page_new += 1
                details = _redact_public_text(normalize_html_text(str(job.get("detail") or "")), limit=8_000)
                if not _operator_role_is_current(title, details, year):
                    skipped += 1
                    continue
                employer = _redact_public_text(company.get("campusOrgName") or source["company"], limit=160)
                jobs.append({
                    "external_id": f"unicom-{identifier}", "program_external_id": stable_program_external_id(program),
                    "company": employer, "title": title, "city": _redact_public_text(job.get("cityName"), limit=160),
                    "region": "中国", "employer_type": "央企科技通信", "industry": "通信运营",
                    "primary_category": "state_tech_telecom", "official_url": url, "application_url": url,
                    # modifiedTime/publishTime in this API are NOT application dates.
                    "opening_date": None, "closing_date": None, "status": "open",
                    "verification_status": "verified", "confidence_score": 0.95,
                    "requirements": _redact_public_text(f"中国联通{year}校园招聘项目公开岗位。{details}", limit=8_000),
                    "tags": ["校园招聘", str(year), "官方ATS", "确定性解析"],
                    "evidence": [f"官方{year}校园招聘列表逐字提供此岗位名称及独立公开详情链接。"],
                })
            if not rows and total != 0 or rows and not page_new:
                reason = reason or "Pagination stopped making progress; snapshot remains incomplete."
                break
            if total == 0 or number >= total_pages:
                complete = not reason and observed == total
                if not complete and not reason:
                    reason = "Observed row count differs from public total; snapshot remains incomplete."
                break
        if not complete and not reason:
            reason = "Page budget reached; snapshot remains incomplete."
        return _operator_result([program], jobs, pages=pages, observed=observed, skipped=skipped,
                                complete=complete, reason=reason, total=total)


class ChinaMobileNoticeAdapter:
    """Read the actual notice feed used by job.10086.cn, without TLS fallback."""

    def __init__(self, html: OfficialHtmlAdapter):
        self.html = html

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        config = source.get("adapter_config", {})
        year = int(config.get("recruitment_year", date.today().year + 1))
        page = fetch_watch_page(source["url"], ())
        try:
            rows = json.loads(page.raw_text)["cData"]["list"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WatchFetchError("China Mobile public notice feed schema changed.") from exc
        if not isinstance(rows, list):
            raise WatchFetchError("China Mobile public notice feed did not contain a list.")
        programs: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        failed = 0
        for row in rows:
            title = clean_text(row.get("text3"), limit=280) if isinstance(row, dict) else ""
            if not _current_campus_campaign(title, year) or not _operator_role_is_current(title, "", year):
                continue
            prefix = re.split(r"20\d{2}", title, maxsplit=1)[0].strip()
            if canonical_telecom_operator(prefix) != "china_mobile":
                continue
            url = _public_reference_url(urllib.parse.urljoin(source["url"], row.get("detail_href") or row.get("jump_link") or ""))
            if not url or urllib.parse.urlsplit(url).hostname != "job.10086.cn":
                failed += 1
                continue
            try:
                result = self.html.scan({**source, "url": url, "company": prefix, "adapter_config": {
                    **config, "adapter": "official_html", "ai_extract": False,
                    "required_markers": [title], "program_name": title,
                    "program_listing_title": f"{year}校园招聘", "emit_program_listing": True,
                    # text4 is publication metadata, never an application start.
                    "opening_date": None, "closing_date": None,
                }})
                programs.extend(result.programs)
                jobs.extend(result.jobs)
            except (WatchFetchError, RuntimeError):
                failed += 1
        result = _operator_result(programs, jobs, pages=1, observed=len(rows), skipped=len(rows)-len(programs),
                                  complete=False, reason=(f"{failed} current notices failed." if failed else "Notice discovery is not a complete vacancy inventory."))
        if not failed:
            result.status = "healthy"
        return result


class _TelecomPageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        attributes = dict(attrs)
        key = attributes.get("name") or attributes.get("id")
        if key in {"currentPage", "lastPage"}:
            self.values[key] = attributes.get("value") or ""


def _telecom_campus_rows(html: str, year: int, source_company: str) -> tuple[list[dict[str, Any]], int, int, int]:
    """Parse only actual job cards, not the page's filter/organization directory."""
    blocks = re.findall(
        r'<li\b[^>]*class=["\'][^"\']*\bposition_list-list-demo\b[^"\']*["\'][^>]*>(.*?)</li>',
        html, re.S | re.I,
    )
    jobs = []
    skipped = 0
    parse_failures = 0
    for block in blocks:
        identity = re.search(r"toDetailPostUrl\(\s*(\d{1,10})\s*,\s*1\s*,", block)
        title_match = re.search(r'class=["\']position_list-list-demo-title["\'][^>]*>(.*?)</div>', block, re.S)
        details = [_redact_public_text(normalize_html_text(value), limit=8_000) for value in re.findall(
            r'class=["\']detailedInformation["\'][^>]*>(.*?)</div>', block, re.S,
        )]
        project = next((part for part in details if part.startswith("招聘项目")), "")
        title = _redact_public_text(normalize_html_text(title_match[1]), limit=280) if title_match else ""
        if not identity or not title or not project:
            parse_failures += 1
            continue
        if not _current_campus_campaign(project, year):
            if re.search(r"20\d{2}.*(?:校园招聘|校招)", project):
                skipped += 1
            else:
                parse_failures += 1
            continue
        if not _operator_role_is_current(title, " ".join(details), year):
            skipped += 1
            continue
        first_row = re.search(r'class=["\']position_list-first-row["\'][^>]*>(.*?)</div>', block, re.S)
        columns = [_redact_public_text(normalize_html_text(value), limit=160) for value in re.findall(
            r'<span\b[^>]*>(.*?)</span>', first_row[1] if first_row else "", re.S,
        )]
        department = columns[0] if columns else ""
        company = department if canonical_telecom_operator(department) == "china_telecom" else source_company
        url = "https://wejob.chinatelecom.com.cn/wt/TELE/mobweb/v8/position/detail?" + urllib.parse.urlencode({
            "recruitType": 1, "postIdsAry": identity[1], "brandCode": 1,
        })
        # This public route is used by the site's toDetailPostUrl handler.
        # Only the public job ID/type/brand are needed; omit openid, operational,
        # webUserIdToken and anonymous JS signatures entirely.
        jobs.append({
            "external_id": f"telecom-{identity[1]}", "company": company, "title": title,
            "city": columns[1] if len(columns) > 1 else "", "region": "中国",
            "employer_type": "央企科技通信", "industry": "通信运营", "primary_category": "state_tech_telecom",
            "official_url": url, "application_url": url, "opening_date": None, "closing_date": None,
            "status": "closed" if re.search(r"(?:该职位|本岗位)(?:已关闭|已结束)|停止申请", normalize_html_text(block)) else "open",
            "verification_status": "verified", "confidence_score": 0.95,
            "requirements": _redact_public_text(
                f"招聘部门：{department or '以详情页为准'}；具体用人单位以岗位详情为准。 " + " ".join(details), limit=8_000,
            ),
            "tags": ["校园招聘", str(year), "官方ATS", "确定性解析"],
            "evidence": [f"中国电信官方岗位列表逐条标明{year}年度秋季校园招聘、职位名称及公开岗位编号。"],
        })
    return jobs, len(blocks), skipped, parse_failures


class ChinaTelecomCampusAdapter:
    """Read China Telecom's public paginated HTML list, without a user session."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        config = source.get("adapter_config", {})
        year = int(config.get("recruitment_year", date.today().year + 1))
        base = "https://wejob.chinatelecom.com.cn/wt/TELE/mobweb/v8/position/list"
        max_pages = min(500, max(1, int(config.get("max_pages", 300))))
        deadline = time.monotonic() + min(1_200.0, max(30.0, float(config.get("max_scan_seconds", 600))))
        jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = observed = skipped = parse_failures = 0
        reason = ""
        complete = False
        for number in range(1, max_pages + 1):
            if time.monotonic() >= deadline:
                reason = "Source time budget reached; snapshot remains incomplete."
                break
            DOMAIN_LIMITER.wait("wejob.chinatelecom.com.cn", max(1.0, float(config.get("domain_delay_seconds", 1))))
            url = base + "?" + urllib.parse.urlencode({
                "recruitType": 1, "brandCode": 1, "ajaxMini": "true", "pc.currentPage": number,
            })
            try:
                page = fetch_watch_page(url, (), timeout_seconds=min(20.0, max(1.0, deadline-time.monotonic())))
                metadata = _TelecomPageMetadata()
                metadata.feed(page.raw_text)
                if int(metadata.values.get("currentPage", "0")) != number or metadata.values.get("lastPage") not in {"true", "false"}:
                    raise WatchFetchError("China Telecom pagination metadata is invalid.")
                rows, raw_count, dropped, invalid = _telecom_campus_rows(page.raw_text, year, source["company"])
            except (WatchFetchError, ValueError) as exc:
                reason = f"Page {number} failed ({type(exc).__name__}); retained successful pages."
                break
            pages += 1
            observed += raw_count
            skipped += dropped
            parse_failures += invalid
            if invalid:
                reason = "Some public job cards could not be parsed; closure checks are disabled."
            duplicate = False
            for row in rows:
                identifier = row["external_id"]
                # Add IDs one by one so duplicates within this page are also
                # detected. Keep the first observation and every distinct row
                # already parsed, but never use an unstable list for closure.
                if identifier in seen:
                    duplicate = True
                    skipped += 1
                    continue
                seen.add(identifier)
                jobs.append(row)
            if duplicate:
                reason = "Duplicate rows appeared while paginating; snapshot remains incomplete."
                break
            if raw_count == 0 and not re.search(r"暂无(?:招聘)?(?:职位|岗位)|没有符合.*(?:职位|岗位)|无匹配.*(?:职位|岗位)", page.text):
                reason = "No recognizable job cards or explicit empty-list marker; snapshot remains incomplete."
                break
            if metadata.values["lastPage"] == "true":
                complete = not reason
                break
            if raw_count == 0:
                reason = "Empty non-final page; snapshot remains incomplete."
                break
        if not complete and not reason:
            reason = "Page budget reached; snapshot remains incomplete."
        if not pages:
            raise WatchFetchError(reason or "China Telecom public list is unavailable.")
        return _operator_result([], jobs, pages=pages, observed=observed, skipped=skipped,
                                complete=complete, reason=reason, parse_failures=parse_failures)


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


class OfficialJsonApiAdapter:
    """Generic public JSON adapter configured through Source Registry mappings."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        if not source.get("url"):
            raise DiscoveryLimitedError("Source API URL is not configured.")
        config = source.get("adapter_config", {})
        field_map = config.get("field_map") or {}
        if not isinstance(field_map, dict) or not field_map.get("title"):
            raise ValueError("official_api adapter requires adapter_config.field_map.title")
        DOMAIN_LIMITER.wait(
            source.get("domain") or "",
            float(config.get("domain_delay_seconds", 1.0)),
        )
        page = fetch_watch_page(
            source["url"], (), timeout_seconds=float(config.get("timeout_seconds", 10))
        )
        try:
            payload = json.loads(page.text)
        except json.JSONDecodeError as exc:
            raise ValueError("Public API did not return valid JSON.") from exc
        rows = _path_value(payload, str(config.get("items_path", "")))
        if not isinstance(rows, list):
            raise ValueError("Configured items_path did not resolve to an array.")
        jobs: list[dict[str, Any]] = []
        for row in rows[:500]:
            if not isinstance(row, dict):
                continue
            item = {
                target: _path_value(row, path)
                for target, path in field_map.items()
                if isinstance(path, str)
            }
            item.setdefault("company", source.get("company") or config.get("company") or "")
            item.setdefault("region", config.get("region", ""))
            item.setdefault("verification_status", "verified")
            item.setdefault("confidence_score", 0.95)
            item.setdefault("status", "open")
            if item.get("title") and item.get("company"):
                jobs.append(item)
        return AdapterResult(
            jobs=jobs,
            content_hash=page.fingerprint,
            normalized_content=page.text[:20_000],
            snapshot_complete=bool(config.get("snapshot_complete", True)),
        )


def _xml_local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _xml_child_text(node: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for child in node:
        if _xml_local_name(child.tag) in wanted:
            return clean_text("".join(child.itertext()), limit=8_000)
    return ""


def _feed_entry_link(node: ET.Element) -> str | None:
    for child in node:
        if _xml_local_name(child.tag) != "link":
            continue
        href = clean_text(child.attrib.get("href"), limit=2_000)
        relation = clean_text(child.attrib.get("rel") or "alternate", limit=40).casefold()
        candidate = href or clean_text(child.text, limit=2_000)
        if candidate and relation in {"alternate", ""}:
            canonical = _public_reference_url(candidate)
            if canonical:
                return canonical
    return None


def _feed_publish_time(value: str) -> str | None:
    raw = clean_text(value, limit=160)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _bounded_feed_number(
    value: Any, *, default: float, minimum: float, maximum: float, name: str
) -> float:
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"public_feed {name} must be numeric.") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"public_feed {name} must be between {minimum:g} and {maximum:g}.")
    return number


class PublicFeedAdapter:
    """Read a public RSS/Atom feed as discovery-only article metadata.

    Feed entries never become verified jobs by themselves.  They are minimal
    discovery signals whose linked official recruitment page must still pass
    the existing verification path before a job is published.
    """

    CAMPUS_MARKERS = OfficialHtmlAdapter.CAMPUS_MARKERS

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        if not source.get("url"):
            raise DiscoveryLimitedError("Public RSS/Atom URL is not configured.")
        if not _public_reference_url(source["url"]):
            raise DiscoveryLimitedError(
                "Public RSS/Atom URL cannot contain credentials or ChatGPT conversation references."
            )
        config = source.get("adapter_config", {})
        timeout = _bounded_feed_number(
            config.get("timeout_seconds"), default=10, minimum=1, maximum=60,
            name="timeout_seconds",
        )
        domain_delay = _bounded_feed_number(
            config.get("domain_delay_seconds"), default=1, minimum=0, maximum=10,
            name="domain_delay_seconds",
        )
        DOMAIN_LIMITER.wait(
            source.get("domain") or "",
            domain_delay,
        )
        page = fetch_watch_page(
            source["url"], (), timeout_seconds=timeout
        )
        raw = page.raw_text.lstrip()
        if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            raise ValueError("RSS/Atom feed must not contain DTD or entity declarations.")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError("Public feed did not return valid RSS/Atom XML.") from exc
        root_name = _xml_local_name(root.tag)
        if root_name == "rss":
            entries = [node for node in root.iter() if _xml_local_name(node.tag) == "item"]
        elif root_name == "feed":
            entries = [node for node in root if _xml_local_name(node.tag) == "entry"]
        else:
            raise ValueError("Public XML is neither an RSS nor an Atom feed.")

        try:
            max_entries = int(config.get("max_entries", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("public_feed max_entries must be an integer.") from exc
        if not 1 <= max_entries <= 10_000:
            raise ValueError("public_feed max_entries must be between 1 and 10000.")
        publisher = _redact_public_text(
            source.get("account_name") or source.get("name"), limit=160
        )
        articles: list[dict[str, Any]] = []
        for entry in entries[:max_entries]:
            title = _redact_public_text(_xml_child_text(entry, "title"), limit=300)
            link = _feed_entry_link(entry)
            if not title or not link:
                continue
            summary_html = _xml_child_text(
                entry, "description", "summary", "content", "encoded"
            )
            try:
                excerpt = normalize_html_text(summary_html) if "<" in summary_html else summary_html
            except WatchFetchError:
                excerpt = clean_text(summary_html, limit=1_500)
            excerpt = _redact_public_text(excerpt, limit=1_500)
            signal_text = f"{title} {excerpt}".casefold()
            is_recruitment = any(marker.casefold() in signal_text for marker in self.CAMPUS_MARKERS)
            year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", signal_text)
            articles.append({
                "publisher": publisher,
                "article_title": title,
                "article_url": link,
                "publish_time": _feed_publish_time(
                    _xml_child_text(entry, "pubdate", "published", "updated")
                ),
                "raw_excerpt": excerpt,
                "is_recruitment": is_recruitment,
                "recruitment_year": int(year_match.group(1)) if year_match else None,
                "classification": "recruitment_signal" if is_recruitment else "other",
            })
        return AdapterResult(
            articles=articles,
            content_hash=page.fingerprint,
            normalized_content=_redact_public_text(page.text, limit=20_000),
            status="partial" if len(entries) > max_entries else "healthy",
            message=(
                f"Feed has {len(entries)} entries; {len(entries) - max_entries} require another input page."
                if len(entries) > max_entries else ""
            ),
            coverage={
                "entries_total": len(entries), "entries_processed": min(len(entries), max_entries),
                "entries_remaining": max(0, len(entries) - max_entries),
                "continuation_required": len(entries) > max_entries,
            },
            # A feed is a rolling window, not a complete snapshot of all jobs.
            snapshot_complete=False,
        )


class OpenAIWebSearchAdapter:
    def scan(self, source: dict[str, Any]) -> AdapterResult:
        cancellation_check = source.get("adapter_config", {}).get("_cancellation_check")
        result = search_current_recruitment_jobs(
            **({"cancellation_check": cancellation_check} if cancellation_check is not None else {}),
        )
        jobs = [
            {
                "external_id": item["id"],
                "company": item["company"],
                "title": item["title"],
                "city": item.get("city", ""),
                "region": item.get("city", ""),
                "employer_type": item.get("employer_type", ""),
                "industry": item.get("industry", ""),
                "official_url": item.get("url"),
                "application_url": item.get("url"),
                "opening_date": item.get("opening_date"),
                "closing_date": item.get("closing_date"),
                "status": "open",
                "verification_status": (
                    "verified" if "标题已验证" in item.get("tags", []) else "pending"
                ),
                "confidence_score": 0.9 if "标题已验证" in item.get("tags", []) else 0.6,
                "requirements": item.get("requirements", ""),
                "tags": item.get("tags", []),
            }
            for item in result.jobs
        ]
        verified_job_external_ids = {
            str(item["id"])
            for item in result.jobs
            if "标题已验证" in item.get("tags", [])
        }
        digest = hashlib.sha256(json.dumps(
            jobs, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        coverage = {}
        if getattr(result, "target_count", 0):
            coverage = {
                "target_count": result.target_count,
                "searched_count": result.searched_count,
                "failed_count": result.failed_count,
                "employers_with_candidates_count": len(result.employers_with_candidates),
                "batch_count": result.batch_count,
                "failed_batch_count": len(result.failed_batches),
                "coverage_percent": result.coverage_percent,
                "failed_employers": list(result.failed_employers),
            }
        official_discovery = list(getattr(result, "official_discovery", ()))
        if official_discovery:
            coverage.update({
                "official_discovery": official_discovery,
                "official_pagination_complete_count": sum(bool(item.get("pagination_complete")) for item in official_discovery),
                "official_partial_or_failed_count": sum(item.get("status") != "healthy" for item in official_discovery),
            })
        return AdapterResult(
            jobs=jobs, content_hash=digest, snapshot_complete=False,
            ai_calls=result.tool_calls, model_tokens_used=result.total_tokens,
            coverage=coverage,
            status="partial" if coverage.get("failed_count") or coverage.get("official_partial_or_failed_count") else "healthy",
            verified_job_external_ids=verified_job_external_ids,
        )


class MockRadarAdapter:
    """Five deterministic rounds covering the complete entity lifecycle."""

    COMPANIES = ("星河科技", "北辰银行", "远海能源", "霁云咨询", "曙光消费")

    @classmethod
    def jobs_for_round(cls, round_number: int) -> list[dict[str, Any]]:
        today = date.today()
        jobs: list[dict[str, Any]] = []
        count = 10 if round_number <= 1 else 12
        for index in range(1, count + 1):
            company = cls.COMPANIES[(index - 1) % len(cls.COMPANIES)]
            closing = today + timedelta(days=30 + index)
            if round_number >= 3 and index == 1:
                closing += timedelta(days=14)
            status = "closed" if round_number == 4 and index == 2 else "open"
            jobs.append({
                "external_id": f"mock-2027-job-{index:02d}",
                "program_external_id": f"mock-program-{company}",
                "company": company,
                "title": f"2027 校园招聘 · 智能业务岗位 {index:02d}",
                "city": ("北京", "上海", "深圳", "广州")[index % 4],
                "region": "中国",
                "employer_type": "测试重点雇主",
                "industry": "招聘情报测试",
                "official_url": f"https://example.com/campus/2027/jobs/{index}",
                "application_url": f"https://example.com/campus/2027/jobs/{index}/apply",
                "opening_date": today.isoformat(),
                "closing_date": closing.isoformat(),
                "status": status,
                "verification_status": "verified",
                "confidence_score": 1.0,
                "requirements": "用于 Future Radar 本地生命周期测试的确定性岗位。",
                "tags": ["校园招聘", "Mock Radar", "T2"],
            })
        return jobs

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        round_number = max(1, min(5, int(source.get("adapter_config", {}).get("round", 1))))
        programs = [
            {
                "external_id": f"mock-program-{company}",
                "company": company,
                "program_name": "2027 校园招聘",
                "recruitment_year": 2027,
                "recruitment_type": "campus",
                "region": "中国",
                "status": "open",
                "verification_status": "verified",
                "confidence_score": 1.0,
                "official_url": f"https://example.com/campus/2027/{stable_digest(company, length=10)}",
            }
            for company in self.COMPANIES
        ]
        jobs = self.jobs_for_round(round_number)
        digest = hashlib.sha256(json.dumps(
            {"round": round_number, "programs": programs, "jobs": jobs},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        return AdapterResult(programs=programs, jobs=jobs, content_hash=digest)


def adapter_for_source(
    source: dict[str, Any], *, repository: RadarRepository,
    openai_api_key: str, ai_model: str,
) -> SourceAdapter:
    adapter_name = source.get("adapter_config", {}).get("adapter") or source["source_type"]
    if adapter_name == "mock":
        return MockRadarAdapter()
    if adapter_name == "legacy_database":
        return LegacyDatabaseAdapter()
    if adapter_name == "official_api":
        provider = source.get("adapter_config", {}).get("provider")
        if provider == "china_unicom_campus":
            return ChinaUnicomCampusAdapter()
        if provider == "china_telecom_campus":
            return ChinaTelecomCampusAdapter()
        if provider == "china_mobile_notices":
            return ChinaMobileNoticeAdapter(OfficialHtmlAdapter(
                repository=repository, api_key=openai_api_key, ai_model=ai_model
            ))
        if provider == "hotjob_campus":
            return HotjobCampusAdapter()
        if provider == "citics_headquarters_campus":
            return CiticsHeadquartersCampusAdapter()
        return OfficialJsonApiAdapter()
    if adapter_name == "public_feed":
        return PublicFeedAdapter()
    if adapter_name == "public_recruitment_index":
        return PublicRecruitmentIndexAdapter()
    if adapter_name in {"official_html", "ats", "other_public_source"}:
        return OfficialHtmlAdapter(repository=repository, api_key=openai_api_key, ai_model=ai_model)
    if adapter_name == "openai_web_search":
        return OpenAIWebSearchAdapter()
    if adapter_name == "wechat_public":
        return WechatSourceAdapter(
            repository=repository, api_key=openai_api_key, ai_model=ai_model
        )
    if adapter_name == "wechat_web_search":
        return WechatWebSearchAdapter(api_key=openai_api_key, ai_model=ai_model)
    if adapter_name == "discovery_limited":
        return DiscoveryLimitedAdapter()
    return ManualAdapter()
