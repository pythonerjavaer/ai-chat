"""Deterministic discovery for public recruitment article listings.

This module deliberately stops at *article signals*.  It does not turn a
listing headline into a verified job, does not log in to any service, and does
not attempt to bypass a CAPTCHA or an anti-bot challenge.  A later verification
stage must follow an official employer/ATS URL before a vacancy can enter the
public job pool.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

from ..recruitment_watch import WatchFetchError, WatchFetchResult, fetch_watch_page
from .normalization import canonicalize_url, clean_text, normalize_date


SASAC_RECRUITMENT_URLS: tuple[str, ...] = (
    "https://www.sasac.gov.cn/n2588035/n2588325/n2588350/index.html",
    "https://wap.sasac.gov.cn/n2588035/n2588325/n2588350/index.html",
)
BANK_RECRUITMENT_URLS: tuple[str, ...] = ("https://www.yhks.cn/",)

_RECRUITMENT_MARKERS = (
    "招聘", "校招", "校园招聘", "秋招", "春招", "应届", "管培生", "实习生",
    "graduate", "campus", "internship",
)
_CAMPUS_MARKERS = (
    "校招", "校园招聘", "秋招", "春招", "应届", "管培生", "毕业生",
    "graduate", "campus",
)
_SOCIAL_MARKERS = ("社会招聘", "社招")
_VACANCY_MARKERS = (
    "招聘公告", "招聘简章", "招聘启事", "招聘信息", "招聘岗位", "招聘计划",
    "校园招聘", "校招", "秋招", "春招", "应届", "管培生", "实习生",
)
_COMMERCIAL_ONLY_MARKERS = (
    "培训课程", "辅导课程", "考试题库", "历年真题", "备考资料", "备考技巧",
    "笔试技巧", "面试技巧", "教材购买",
)
_GENERIC_NAV_TITLES = {
    "招聘", "招聘信息", "招聘公告", "人才招聘", "校园招聘", "社会招聘", "实习实践",
    "实习生招聘", "银行招聘", "央企招聘", "国企招聘", "最新招聘", "更多", "更多招聘",
}
_RESULT_MARKERS = (
    "拟录用", "拟接收", "录用公示", "录用人员公示", "体检名单", "面试名单",
    "笔试名单", "成绩公示",
)
_SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie",
    "key", "password", "refresh_token", "secret", "sig", "signature", "token",
}
_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")
_API_SECRET = re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b")
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_FULL_DATE = re.compile(
    r"(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
)
_BLOCK_TAGS = {"article", "dd", "div", "dl", "dt", "li", "p", "td", "tr"}
_IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}


class PublicDiscoveryUnavailable(RuntimeError):
    """No configured public listing endpoint could be read safely."""


@dataclass(frozen=True)
class PublicArticleMetadata:
    """Allowlisted metadata emitted by the public discovery layer."""

    publisher: str
    title: str
    url: str
    publish_time: str | None
    raw_excerpt: str
    is_recruitment: bool
    recruitment_year: int | None
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_radar_article(self) -> dict[str, Any]:
        """Map the neutral record to ``RadarArticleInput`` field names."""
        return {
            "publisher": self.publisher,
            "article_title": self.title,
            "article_url": self.url,
            "publish_time": self.publish_time,
            "raw_excerpt": self.raw_excerpt,
            "is_recruitment": self.is_recruitment,
            "recruitment_year": self.recruitment_year,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class PublicDiscoveryBatch:
    source_id: str
    source_url: str
    content_hash: str
    page_fingerprint: str
    articles: tuple[PublicArticleMetadata, ...]

    def article_dicts(self) -> list[dict[str, Any]]:
        return [article.to_dict() for article in self.articles]

    def radar_articles(self) -> list[dict[str, Any]]:
        return [article.to_radar_article() for article in self.articles]


@dataclass
class _Container:
    tag: str
    parts: list[str]


@dataclass
class _Anchor:
    href: str
    title_attribute: str
    parts: list[str]
    containers: tuple[_Container, ...]
    event_index: int


class _PublicListingParser(HTMLParser):
    """Collect links plus nearby visible text without executing page code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self.events: list[str] = []
        self._containers: list[_Container] = []
        self._anchor: _Anchor | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.casefold()
        if lowered in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if lowered in _BLOCK_TAGS:
            self._containers.append(_Container(lowered, []))
        if lowered != "a" or self._anchor is not None:
            return
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        href = clean_text(attributes.get("href"), limit=2_000)
        if href:
            self._anchor = _Anchor(
                href=href,
                title_attribute=clean_text(attributes.get("title"), limit=300),
                parts=[],
                containers=tuple(self._containers),
                event_index=len(self.events),
            )

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in _IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if lowered == "a" and self._anchor is not None:
            self.anchors.append(self._anchor)
            self._anchor = None
        if lowered in _BLOCK_TAGS:
            for index in range(len(self._containers) - 1, -1, -1):
                if self._containers[index].tag == lowered:
                    del self._containers[index:]
                    break

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = clean_text(data, limit=4_000)
        if not value:
            return
        self.events.append(value)
        for container in self._containers:
            container.parts.append(value)
        if self._anchor is not None:
            self._anchor.parts.append(value)

    def anchor_context(self, anchor: _Anchor) -> str:
        candidates: list[str] = []
        for container in reversed(anchor.containers):
            value = clean_text(" ".join(container.parts), limit=2_000)
            if value and len(value) <= 2_000:
                candidates.append(value)
        if candidates:
            # Prefer the tightest containing block.  Looking outward merely to
            # find a date can accidentally attach a neighbouring article's
            # timestamp (or campaign year) to this link.
            return min(candidates, key=len)
        start = max(0, anchor.event_index - 3)
        end = min(len(self.events), anchor.event_index + len(anchor.parts) + 4)
        return clean_text(" ".join(self.events[start:end]), limit=2_000)


FetchPage = Callable[..., WatchFetchResult]


def _redact_text(value: Any, *, limit: int) -> str:
    text = clean_text(value, limit=100_000)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _PHONE.sub("[redacted-phone]", text)
    text = _LANDLINE.sub("[redacted-phone]", text)
    text = _API_SECRET.sub("[redacted-secret]", text)
    text = _UUID.sub("[redacted-uuid]", text)
    return clean_text(text, limit=limit)


def _safe_source_url(value: str, *, allowed_domains: Iterable[str]) -> str | None:
    raw = clean_text(value, limit=2_000)
    try:
        query = urllib.parse.urlsplit(raw).query
    except ValueError:
        return None
    if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _ in urllib.parse.parse_qsl(query)):
        return None
    try:
        canonical = canonicalize_url(raw, allow_empty=False)
    except ValueError:
        return None
    hostname = (urllib.parse.urlsplit(canonical).hostname or "").casefold()
    allowed = tuple(domain.casefold().lstrip(".") for domain in allowed_domains)
    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed):
        return None
    return canonical


def _publish_date(text: str) -> str | None:
    match = _FULL_DATE.search(text)
    if not match:
        return None
    return normalize_date("-".join(match.groups()))


def _recruitment_year(text: str) -> int | None:
    patterns = (
        r"(?<!\d)(20\d{2})\s*届",
        # This function receives a link title already classified as a
        # recruitment signal, so an explicit four-digit year followed by 年 is
        # campaign metadata even when a long organization name precedes 招聘.
        r"(?<!\d)(20\d{2})\s*年(?:度)?",
        r"(?<!\d)(\d{2})\s*届",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        year = int(match.group(1))
        if index == 2:
            year += 2000
        if 2020 <= year <= 2100:
            return year
    return None


def _signal_classification(text: str) -> tuple[bool, str]:
    folded = text.casefold()
    recruitment = (
        any(marker.casefold() in folded for marker in _RECRUITMENT_MARKERS)
        or bool(re.search(r"(?<!\d)(?:20)?\d{2}\s*届", folded))
    )
    if not recruitment:
        return False, "other"
    if (
        any(marker.casefold() in folded for marker in _COMMERCIAL_ONLY_MARKERS)
        and not any(marker.casefold() in folded for marker in _VACANCY_MARKERS)
    ):
        return False, "other"
    if any(marker in folded for marker in _RESULT_MARKERS):
        return True, "recruitment_result_signal"
    if any(marker.casefold() in folded for marker in _CAMPUS_MARKERS) or re.search(
        r"(?<!\d)(?:20)?\d{2}\s*届", folded
    ):
        return True, "campus_recruitment_signal"
    if any(marker in folded for marker in _SOCIAL_MARKERS):
        return True, "social_recruitment_signal"
    return True, "recruitment_signal"


def _extract_articles(
    page: WatchFetchResult,
    *,
    publisher: str,
    allowed_domains: Iterable[str],
    max_articles: int,
) -> tuple[PublicArticleMetadata, ...]:
    parser = _PublicListingParser()
    try:
        parser.feed(page.raw_text)
        parser.close()
    except Exception as exc:
        raise PublicDiscoveryUnavailable("公开招聘列表 HTML 无法安全解析。") from exc

    articles: list[PublicArticleMetadata] = []
    seen_urls: set[str] = set()
    for anchor in parser.anchors:
        title = _redact_text(" ".join(anchor.parts) or anchor.title_attribute, limit=300)
        if len(title) < 4 or title.casefold() in _GENERIC_NAV_TITLES:
            continue
        absolute = urllib.parse.urljoin(page.final_url, anchor.href)
        safe_url = _safe_source_url(absolute, allowed_domains=allowed_domains)
        if not safe_url or safe_url in seen_urls:
            continue
        context = _redact_text(parser.anchor_context(anchor), limit=1_500)
        # Listing containers are often broad navigation panels.  Recruitment
        # classification therefore uses the link title itself; nearby text is
        # used only for a date/excerpt and may never turn an unrelated link
        # into a recruitment signal.
        is_recruitment, classification = _signal_classification(title)
        if not is_recruitment:
            continue
        articles.append(PublicArticleMetadata(
            publisher=_redact_text(publisher, limit=160),
            title=title,
            url=safe_url,
            publish_time=_publish_date(context),
            raw_excerpt=context,
            is_recruitment=True,
            # Campaign year comes from the link title only.  A publication date
            # in nearby text is not evidence of the recruitment cohort.
            recruitment_year=_recruitment_year(title),
            classification=classification,
        ))
        seen_urls.add(safe_url)
        if len(articles) >= max_articles:
            break
    return tuple(articles)


def _fetch_first_available(
    urls: Iterable[str],
    *,
    fetcher: FetchPage,
    timeout_seconds: float,
) -> WatchFetchResult:
    last_error: WatchFetchError | None = None
    for url in urls:
        try:
            return fetcher(
                url,
                _RECRUITMENT_MARKERS,
                timeout_seconds=timeout_seconds,
            )
        except WatchFetchError as exc:
            last_error = exc
    raise PublicDiscoveryUnavailable(
        "公开招聘列表暂时无法访问；没有伪造成功结果。"
    ) from last_error


def _discover(
    *,
    source_id: str,
    publisher: str,
    urls: Iterable[str],
    allowed_domains: Iterable[str],
    max_articles: int,
    timeout_seconds: float,
    fetcher: FetchPage,
) -> PublicDiscoveryBatch:
    if not 1 <= max_articles <= 100:
        raise ValueError("max_articles must be between 1 and 100")
    page = _fetch_first_available(
        urls,
        fetcher=fetcher,
        timeout_seconds=timeout_seconds,
    )
    articles = _extract_articles(
        page,
        publisher=publisher,
        allowed_domains=allowed_domains,
        max_articles=max_articles,
    )
    serialized = json.dumps(
        [article.to_dict() for article in articles],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return PublicDiscoveryBatch(
        source_id=source_id,
        source_url=page.final_url,
        content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        page_fingerprint=page.fingerprint,
        articles=articles,
    )


def discover_sasac_recruitment_articles(
    *,
    max_articles: int = 50,
    timeout_seconds: float = 12,
    fetcher: FetchPage | None = None,
) -> PublicDiscoveryBatch:
    """Discover recruitment announcements from the public SASAC listing."""
    return _discover(
        source_id="public-sasac-recruitment",
        publisher="国务院国资委",
        urls=SASAC_RECRUITMENT_URLS,
        allowed_domains=("sasac.gov.cn",),
        max_articles=max_articles,
        timeout_seconds=timeout_seconds,
        fetcher=fetcher or fetch_watch_page,
    )


def discover_bank_recruitment_articles(
    *,
    max_articles: int = 50,
    timeout_seconds: float = 12,
    fetcher: FetchPage | None = None,
) -> PublicDiscoveryBatch:
    """Discover public ``yhks.cn`` articles as unverified recruitment clues."""
    return _discover(
        source_id="public-bank-recruitment",
        publisher="银行招聘网",
        urls=BANK_RECRUITMENT_URLS,
        allowed_domains=("yhks.cn",),
        max_articles=max_articles,
        timeout_seconds=timeout_seconds,
        fetcher=fetcher or fetch_watch_page,
    )
