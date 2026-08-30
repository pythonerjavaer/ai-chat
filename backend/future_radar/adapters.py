"""Pluggable source adapters used by Future Radar scans."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from openai import OpenAI

from .. import database
from ..recruitment import primary_employer_category
from ..recruitment_search import (
    WEB_SEARCH_SOURCE,
    _inspect_official_candidate_page,
    _normalize_job as normalize_web_search_job,
    _semantic_date_appears_in_page,
    search_current_recruitment_jobs,
)
from ..recruitment_watch import WatchFetchError, fetch_watch_page
from ..recruitment_watch import normalize_html_text
from .ai import extract_recruitment_content
from .normalization import (
    PRIMARY_CATEGORY_CODES,
    canonicalize_url,
    clean_text,
    normalize_date,
    normalize_taxonomy_value,
    stable_digest,
)
from .public_discovery import (
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
    """Map legacy metadata to one starfield without inspecting prose or names."""
    explicit = normalize_taxonomy_value(item.get("primary_category"))
    if explicit in PRIMARY_CATEGORY_CODES:
        return explicit

    # Only organization metadata participates.  Company, title, requirements
    # and other prose are deliberately excluded so classification cannot drift
    # because a role happens to mention another sector.
    return primary_employer_category({
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
            "tags": list(dict.fromkeys([*tags, "历史搜索发现"])),
        }

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        del source
        with database.connect() as connection:
            legacy_rows = connection.execute(
                """
                SELECT id, company, employer_type, title, city, industry, url,
                       opening_date, closing_date, requirements, tags, status,
                       last_verified_at
                FROM recruitment_jobs
                WHERE source=? OR tags LIKE '%AI网页搜索%'
                ORDER BY id
                """,
                (WEB_SEARCH_SOURCE,),
            ).fetchall()
            ingest_rows = connection.execute(
                """
                SELECT id, dedupe_key, external_id, promoted_job_id,
                       CASE WHEN source_id IN (
                           'chatgpt-radar-01', 'chatgpt-radar-02', 'chatgpt-radar-03',
                           'chatgpt-radar-04', 'chatgpt-radar-05'
                       ) THEN 1 ELSE 0 END AS controlled_chatgpt,
                       company, employer_type, title, city, industry,
                       official_url, canonical_url, opening_date, closing_date,
                       requirements, tags, incoming_status AS status,
                       verification_status, source_updated_at, last_seen_at
                FROM recruitment_ingest_candidates ORDER BY id
                """
            ).fetchall()

        jobs_by_id: dict[str, dict[str, Any]] = {}
        cursors: list[str] = []
        for rows, is_ingest in ((legacy_rows, False), (ingest_rows, True)):
            for row in rows:
                item = dict(row)
                external_id = (
                    self._ingest_external_id(item)
                    if is_ingest else self._safe_external_id(item["id"])
                )
                job = self._job(item, external_id=external_id)
                if job:
                    jobs_by_id[external_id] = job
                elif is_ingest:
                    jobs_by_id.pop(external_id, None)
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
            # Unlike a rolling web response, this is a complete read of the
            # persisted local pool.  Removed/closed rows can retire this source
            # link; another active official source still protects its job.
            snapshot_complete=True,
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
        last_error: Exception | None = None
        page = None
        for attempt in range(3):
            try:
                DOMAIN_LIMITER.wait(domain, minimum)
                page = fetch_watch_page(
                    source["url"],
                    self.CAMPUS_MARKERS,
                    timeout_seconds=float(source.get("adapter_config", {}).get("timeout_seconds", 10)),
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
        folded_page = page.text.casefold()
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

        return AdapterResult(
            programs=programs,
            jobs=jobs,
            content_hash=page.fingerprint,
            normalized_content=page.text[:20_000],
            snapshot_complete=True,
            ai_calls=ai_calls,
            model_tokens_used=model_tokens,
        )


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
        if not 1 <= max_entries <= 100:
            raise ValueError("public_feed max_entries must be between 1 and 100.")
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
            # A feed is a rolling window, not a complete snapshot of all jobs.
            snapshot_complete=False,
        )


class OpenAIWebSearchAdapter:
    def scan(self, source: dict[str, Any]) -> AdapterResult:
        del source
        result = search_current_recruitment_jobs()
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
        return AdapterResult(
            jobs=jobs, content_hash=digest, snapshot_complete=False,
            ai_calls=result.tool_calls, model_tokens_used=result.total_tokens,
            coverage=coverage,
            status="partial" if coverage.get("failed_count") else "healthy",
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
