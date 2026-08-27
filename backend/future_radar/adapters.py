"""Pluggable source adapters used by Future Radar scans."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from .. import database
from ..recruitment_search import search_current_recruitment_jobs
from ..recruitment_watch import WatchFetchError, fetch_watch_page
from .ai import extract_recruitment_content
from .normalization import clean_text, stable_digest
from .repository import RadarRepository


logger = logging.getLogger(__name__)


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


class ManualAdapter:
    def scan(self, source: dict[str, Any]) -> AdapterResult:
        del source
        return AdapterResult(status="idle", snapshot_complete=False, message="Push-only source.")


class LegacyDatabaseAdapter:
    """Moves verified legacy jobs into Radar without deleting the old API."""

    def scan(self, source: dict[str, Any]) -> AdapterResult:
        del source
        jobs: list[dict[str, Any]] = []
        for item in database.list_recruitment_jobs():
            tags = list(item.get("tags") or [])
            pending = bool({"待官方核验", "待打开核对"}.intersection(tags))
            jobs.append({
                "external_id": item["id"],
                "company": item["company"],
                "title": item["title"],
                "city": item.get("city", ""),
                "region": item.get("city", ""),
                "employer_type": item.get("employer_type", ""),
                "industry": item.get("industry", ""),
                "official_url": item.get("url") or None,
                "application_url": item.get("url") or None,
                "opening_date": item.get("opening_date"),
                "closing_date": item.get("closing_date"),
                "status": item.get("status", "open"),
                "verification_status": "pending" if pending else "verified",
                "confidence_score": 0.55 if pending else 0.95,
                "requirements": item.get("requirements", ""),
                "tags": [*tags, "legacy-compatible"],
            })
        content_hash = hashlib.sha256(json.dumps(
            jobs, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        return AdapterResult(jobs=jobs, content_hash=content_hash)


class OfficialHtmlAdapter:
    """Deterministically fingerprints a public page and can emit a program signal."""

    CAMPUS_MARKERS = ("校园招聘", "秋季招聘", "秋招", "校招", "应届", "graduate", "campus")

    def __init__(self, *, repository: RadarRepository, api_key: str, ai_model: str):
        self.repository = repository
        self.api_key = api_key
        self.ai_model = ai_model

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
        if campus and company:
            programs.append({
                "company": company,
                "program_name": clean_text(config.get("program_name") or f"{year} 校园招聘"),
                "recruitment_year": year,
                "recruitment_type": config.get("recruitment_type", "campus"),
                "region": config.get("region", "中国"),
                "official_url": page.final_url,
                "status": "open",
                "verification_status": "verified",
                "confidence_score": 0.9,
            })

        job_marker = clean_text(config.get("job_marker"), limit=280)
        job_title = clean_text(config.get("job_title"), limit=280)
        if company and job_title and job_marker and job_marker.casefold() in page.text.casefold():
            jobs.append({
                "company": company,
                "title": job_title,
                "city": clean_text(config.get("city"), limit=160),
                "region": clean_text(config.get("region") or "中国", limit=160),
                "employer_type": clean_text(config.get("employer_type"), limit=80),
                "industry": clean_text(config.get("industry"), limit=120),
                "official_url": page.final_url,
                "application_url": page.final_url,
                "opening_date": config.get("opening_date"),
                "closing_date": config.get("closing_date"),
                "status": "open",
                "verification_status": "verified",
                "confidence_score": 0.95,
                "requirements": clean_text(config.get("requirements"), limit=8_000),
                "tags": ["校园招聘", "官方网页", "确定性解析"],
            })

        ai_calls = 0
        model_tokens = 0
        if bool(config.get("ai_extract")) and page.fingerprint != source.get("last_content_hash"):
            try:
                extracted = extract_recruitment_content(
                    repository=self.repository,
                    content=page.text,
                    content_hash=page.fingerprint,
                    source_url=page.final_url,
                    model=self.ai_model,
                    api_key=self.api_key,
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
        digest = hashlib.sha256(json.dumps(
            jobs, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        return AdapterResult(
            jobs=jobs, content_hash=digest, snapshot_complete=True,
            ai_calls=result.tool_calls, model_tokens_used=result.total_tokens,
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
    if adapter_name in {"official_html", "ats", "other_public_source"}:
        return OfficialHtmlAdapter(repository=repository, api_key=openai_api_key, ai_model=ai_model)
    if adapter_name == "openai_web_search":
        return OpenAIWebSearchAdapter()
    if adapter_name == "wechat_public":
        return WechatSourceAdapter(
            repository=repository, api_key=openai_api_key, ai_model=ai_model
        )
    if adapter_name == "discovery_limited":
        return DiscoveryLimitedAdapter()
    return ManualAdapter()
