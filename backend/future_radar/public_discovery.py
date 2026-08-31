"""Deterministic discovery for public recruitment article listings.

Article indexes deliberately stop at *article signals*.  Employer discovery
also follows public, same-origin listing/pagination/detail links.  Neither path
declares a vacancy verified: callers must apply the official-page evidence gate.
No login, JavaScript execution, guessed private API, or CAPTCHA bypass is used.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

from ..recruitment_watch import (
    WatchFetchError, WatchFetchResult, fetch_watch_page, normalize_html_text,
    validate_public_https_url,
)
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


# These are independent, per-employer HTTP budgets, not a shared result cap or
# an AI spending limit.  A large employer cannot consume another one's quota.
OFFICIAL_LIST_PAGE_BUDGET = 24
OFFICIAL_DETAIL_PAGE_BUDGET = 120
OFFICIAL_DISCOVERY_SECONDS = 90.0


class OfficialDiscoveryCancelled(RuntimeError):
    """The existing scan/source lease no longer permits further HTTP work."""


def check_discovery_cancellation(check: Callable[[], None] | None) -> None:
    if check is not None:
        try:
            check()
        except Exception as exc:
            raise OfficialDiscoveryCancelled("Official discovery lost its scan lease.") from exc


@dataclass(frozen=True)
class OfficialJobCandidate:
    job: dict[str, Any]
    page_text: str
    final_url: str


@dataclass(frozen=True)
class OfficialJobDiscoveryBatch:
    candidates: tuple[OfficialJobCandidate, ...]
    coverage: dict[str, Any]
    content_hash: str


_ROLE_WORDS = re.compile(
    r"工程师|分析师|研究员|经理|专员|助理|顾问|管培生|培训生|设计师|开发|[岗职]位?|"
    r"\b(?:engineer|analyst|associate|consultant|trainee|scientist|designer|developer)\b",
    re.IGNORECASE,
)
_DUTY_WORDS = re.compile(
    r"岗位职责|工作职责|职位描述|任职要求|任职资格|岗位要求|职位要求|"
    r"\b(?:responsibilities|qualifications|job description|requirements)\b", re.IGNORECASE,
)
_LIST_WORDS = re.compile(
    r"岗位列表|职位列表|招聘职位|招聘岗位|查看职位|查看岗位|全部职位|全部岗位|校园招聘|校招|"
    r"\b(?:careers?|vacancies|job search|search jobs|view jobs|all jobs|positions|campus)\b",
    re.IGNORECASE,
)
_DETAIL_PATH = re.compile(
    r"/(?:jobs?|positions?|vacanc(?:y|ies))/(?!search(?:/|$)|list(?:/|$))[^/?]+|"
    r"(?:job|position|vacancy)[_-]?(?:detail|id)|/detail(?:/|\?)", re.IGNORECASE,
)
_NEXT_WORDS = re.compile(r"^(?:下一页|下页|更多职位|更多岗位|加载更多|next(?:\s+page)?|load more|[›»>])$", re.IGNORECASE)
_FORBIDDEN_ACTION = re.compile(
    # Match action endpoints, not words inside a vacancy slug or a static app
    # directory (e.g. Account-Management_R-123 or hzzp-apply-web/static/index).
    r"(?:^|/)(?:login|logout|signin|signup|register|apply|application|resume|account|profile|delete|unsubscribe)(?:[/.;]|$)",
    re.IGNORECASE,
)
_NON_HTML = re.compile(r"\.(?:pdf|docx?|xlsx?|zip|jpe?g|png|gif|svg|mp4|exe)(?:$|\?)", re.IGNORECASE)


class _OfficialListingParser(_PublicListingParser):
    def __init__(self) -> None:
        super().__init__()
        self.json_ld: list[str] = []
        self.headings: list[str] = []
        self.next_links: list[str] = []
        self.disabled_links: set[str] = set()
        self.opaque_more = False
        self._script: list[str] | None = None
        self._heading: list[str] | None = None
        self._control: tuple[dict[str, str], list[str]] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag == "script" and attributes.get("type", "").casefold() == "application/ld+json":
            self._script = []
        if not self._ignored_depth:
            if tag == "h1" or (tag == "h2" and re.search(r"job|position", attributes.get("class", ""))):
                self._heading = []
            if tag in {"a", "button", "link"}:
                disabled = attributes.get("aria-disabled") == "true" or "disabled" in attributes
                disabled = disabled or "disabled" in attributes.get("class", "").split()
                if disabled and attributes.get("href"):
                    self.disabled_links.add(attributes["href"])
                if not disabled:
                    if "next" in attributes.get("rel", "").split():
                        self.next_links.append(attributes.get("href", ""))
                    if tag != "link":
                        self._control = (attributes, [])
        super().handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        if not self._ignored_depth:
            if self._heading is not None:
                self._heading.append(data)
            if self._control is not None:
                self._control[1].append(data)
        super().handle_data(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.json_ld.append("".join(self._script))
            self._script = None
        if tag in {"h1", "h2"} and self._heading is not None:
            self.headings.append(clean_text(" ".join(self._heading), limit=280))
            self._heading = None
        if tag in {"a", "button"} and self._control is not None:
            attributes, parts = self._control
            label = clean_text(" ".join(parts) or attributes.get("aria-label"), limit=160)
            if _NEXT_WORDS.fullmatch(label):
                href = attributes.get("href", "")
                if href and not href.startswith(("#", "javascript:")):
                    self.next_links.append(href)
                else:
                    self.opaque_more = True
            self._control = None
        super().handle_endtag(tag)


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.casefold(), parts.netloc.casefold()


def _official_link(value: str, base: str) -> str | None:
    """Only public read-only navigation on the entry point's exact origin."""
    if not value or value.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    try:
        url = urllib.parse.urljoin(base, value)
        parts = urllib.parse.urlsplit(url)
        # Hash routers need a provider-specific reader; stripping their route
        # would otherwise fetch the same shell and invent a successful visit.
        if parts.fragment.startswith(("/", "!")) or _origin(url) != _origin(base):
            return None
        if _EMAIL.search(url) or _API_SECRET.search(url):
            return None
        if _FORBIDDEN_ACTION.search(parts.path) or _NON_HTML.search(parts.path):
            return None
        if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _ in urllib.parse.parse_qsl(parts.query)):
            return None
        return canonicalize_url(url, allow_empty=False)
    except (ValueError, WatchFetchError):
        return None


def _same_origin_opener(origin: tuple[str, str]):
    class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urljoin(req.full_url, newurl)
            if _origin(target) != origin:
                raise WatchFetchError("Official discovery redirect left its public origin.")
            if not _official_link(target, req.full_url):
                raise WatchFetchError("Official discovery redirect is not safe public navigation.")
            validate_public_https_url(target, resolve_dns=True)
            return super().redirect_request(req, fp, code, msg, headers, target)

    # fetch_watch_page supplies its own safe redirect handler. Replace it with
    # an equally strict handler which additionally rejects cross-origin hops
    # *before* making that request, not merely after receiving its response.
    return lambda *_handlers: urllib.request.build_opener(SameOriginRedirect())


def _job_postings(value: Any) -> Iterable[dict[str, Any]]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            kinds = item.get("@type", [])
            if "JobPosting" in (kinds if isinstance(kinds, list) else [kinds]):
                yield item
            else:
                stack.extend(item[key] for key in ("@graph", "mainEntity", "itemListElement", "item") if key in item)


def _concrete_title(title: str) -> bool:
    return bool(
        2 <= len(title) <= 240 and _ROLE_WORDS.search(title)
        and not re.search(r"岗位列表|职位列表|全部岗位|全部职位|岗位详情|职位详情|招聘公告|招聘简章|job search", title, re.IGNORECASE)
        and title not in _GENERIC_NAV_TITLES and not _LIST_WORDS.fullmatch(title)
        and not _DUTY_WORDS.fullmatch(title)
    )


def _detail_candidates(
    parser: _OfficialListingParser, page: WatchFetchResult, company: str, hint: str,
) -> tuple[list[OfficialJobCandidate], list[tuple[str, str]]]:
    candidates: list[OfficialJobCandidate] = []
    linked: list[tuple[str, str]] = []
    visible = str(page.text or "")
    for raw in parser.json_ld:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for item in _job_postings(payload):
            title = clean_text(item.get("title"), limit=280)
            target = _official_link(str(item.get("url") or page.final_url), page.final_url)
            if not target or not title:
                continue
            if target != _official_link(page.final_url, page.final_url):
                linked.append((target, title))
                continue
            # Workday can serve the whole vacancy as public JobPosting JSON-LD
            # with no visible text at all. If a visible body *does* exist, keep
            # requiring its title to agree (reject unresolved/stale SPA shells).
            if visible.strip() and title.casefold() not in visible.casefold():
                continue
            description = normalize_html_text(str(item.get("description") or ""))
            if len(description) < 30:
                continue
            organization = item.get("hiringOrganization")
            employer = clean_text(organization.get("name"), limit=160) if isinstance(organization, dict) else ""
            locations = item.get("jobLocation", [])
            locations = locations if isinstance(locations, list) else [locations]
            cities = []
            for location in locations:
                address = location.get("address", {}) if isinstance(location, dict) else {}
                if isinstance(address, dict) and address.get("addressLocality"):
                    cities.append(clean_text(address["addressLocality"], limit=120))
            # JobPosting fields are public, typed evidence too (e.g. Workday
            # serves the JD in JSON-LD). Only allowlisted fields are used;
            # missing employer/cohort/open-state evidence is never fabricated.
            evidence_text = f"{visible}\nJobPosting 岗位名称：{title}\n招聘机构：{employer}\n职位描述：{description}"
            valid_through = normalize_date(str(item.get("validThrough") or "")[:10])
            candidates.append(OfficialJobCandidate(
                job={
                    "company": employer or company, "title": _redact_text(title, limit=240),
                    "city": "、".join(dict.fromkeys(cities)), "official_url": target,
                    # datePosted is a publication date, never application opening.
                    "opening_date": None, "closing_date": None,
                    "posting_expired": bool(valid_through and valid_through <= date.today().isoformat()),
                    "requirements": _redact_text(f"{description}\n{visible}", limit=20_000),
                },
                page_text=evidence_text, final_url=target,
            ))
    if not candidates and _DUTY_WORDS.search(visible):
        for title in [*parser.headings, hint]:
            if not _concrete_title(title) or title.casefold() not in visible.casefold():
                continue
            location = re.search(r"(?:工作地点|工作城市|职位地点|Location)\s*[:：]\s*([^\s；;<>{}]{2,60})", visible, re.IGNORECASE)
            candidates.append(OfficialJobCandidate(
                job={
                    "company": company, "title": _redact_text(title, limit=240),
                    "city": _redact_text(location[1], limit=120) if location else "", "official_url": _official_link(page.final_url, page.final_url),
                    "opening_date": None, "closing_date": None,
                    "requirements": _redact_text(visible, limit=20_000),
                },
                page_text=visible, final_url=_official_link(page.final_url, page.final_url),
            ))
            break
    return candidates, linked


def discover_official_job_pages(
    urls: Iterable[str], *, company: str, fetcher: FetchPage | None = None,
    initial_pages: dict[str, WatchFetchResult] | None = None,
    max_listing_pages: int = OFFICIAL_LIST_PAGE_BUDGET,
    max_detail_pages: int = OFFICIAL_DETAIL_PAGE_BUDGET,
    max_seconds: float = OFFICIAL_DISCOVERY_SECONDS,
    timeout_seconds: float = 8.0,
    before_fetch: Callable[[str], None] | None = None,
    cancellation_check: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> OfficialJobDiscoveryBatch:
    """Read actual employer links with bounded breadth-first pagination.

    ``pagination_complete`` describes the linked lists, never the company's
    entire inventory. Only explicit total-count/page-count/empty-list evidence
    can establish it. Missing links, JS-only pagination and exhausted budgets
    are partial. Callers must *always* preserve unobserved old vacancies.
    """
    if max_listing_pages < 1 or max_detail_pages < 1 or max_seconds <= 0:
        raise ValueError("Official discovery budgets must be positive.")
    fetch = fetcher or fetch_watch_page
    # Preserve existing narrow injected fetcher signatures. The built-in
    # transport and **kwargs wrappers opt in; unrelated watch callers do not.
    try:
        parameters = inspect.signature(fetch).parameters
        structured_body_options = {"allow_structured_body": True} if (
            "allow_structured_body" in parameters
            or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        ) else {}
    except (TypeError, ValueError):
        structured_body_options = {}
    started = clock()
    lists: deque[tuple[str, int, str]] = deque()
    details: deque[tuple[str, int, str]] = deque()
    queued: set[str] = set()
    read_urls: set[str] = set()
    sources: list[dict[str, Any]] = []
    totals: dict[int, set[int]] = {}
    page_numbers: dict[int, set[int]] = {}
    page_totals: dict[int, set[int]] = {}
    detail_urls: dict[int, set[str]] = {}
    fingerprints: list[tuple[str, str]] = []
    listing_fingerprints: dict[int, set[str]] = {}
    candidates: dict[tuple[str, str], OfficialJobCandidate] = {}
    initial = initial_pages or {}
    for raw_url in dict.fromkeys(urls):
        url = _official_link(raw_url, raw_url)
        index = len(sources)
        sources.append({
            "source_id": hashlib.sha256(str(raw_url).encode()).hexdigest()[:16],
            "status": "pending", "listing_pages_fetched": 0, "detail_pages_fetched": 0,
            "listing_failures": 0, "detail_failures": 0, "unparsed_detail_pages": 0,
            "blocked_detail_links": 0,
            "unresolved_pagination": 0, "deferred_listing_pages": 0, "deferred_detail_pages": 0,
            "pagination_complete": False,
        })
        totals[index], page_numbers[index], page_totals[index], detail_urls[index] = set(), set(), set(), set()
        listing_fingerprints[index] = set()
        if url and url not in queued:
            (details if _DETAIL_PATH.search(url) else lists).append((url, index, ""))
            queued.add(url)
        else:
            sources[index]["status"] = "discovery_limited"
            sources[index]["reason"] = "unsafe_or_duplicate_entry_point"

    listing_count = detail_count = 0
    stop_reason = "linked_pages_exhausted"
    while lists or details:
        check_discovery_cancellation(cancellation_check)
        remaining = max_seconds - (clock() - started)
        if remaining <= 0:
            stop_reason = "time_budget"
            break
        can_list = bool(lists and listing_count < max_listing_pages)
        can_detail = bool(details and detail_count < max_detail_pages)
        if not can_list and not can_detail:
            stop_reason = "page_budget"
            break
        # Interleave detail visits so a huge nav tree cannot use all wall time
        # before any discovered vacancy is inspected.
        kind = "detail" if can_detail and (not can_list or listing_count > detail_count) else "listing"
        url, owner, hint = (details if kind == "detail" else lists).popleft()
        if kind == "detail":
            detail_count += 1
        else:
            listing_count += 1
        source = sources[owner]
        try:
            page = initial.get(url)
            if page is None:
                if before_fetch:
                    before_fetch(urllib.parse.urlsplit(url).hostname or "")
                check_discovery_cancellation(cancellation_check)
                remaining = max_seconds - (clock() - started)
                if remaining <= 0:
                    (details if kind == "detail" else lists).appendleft((url, owner, hint))
                    stop_reason = "time_budget"
                    break
                page = fetch(
                    url, (), timeout_seconds=min(timeout_seconds, remaining), max_bytes=1_500_000,
                    opener_factory=_same_origin_opener(_origin(url)),
                    **structured_body_options,
                )
            check_discovery_cancellation(cancellation_check)
            final = _official_link(page.final_url, url)
            if not final:
                raise WatchFetchError("Unusable official page redirect.")
            source[f"{kind}_pages_fetched"] += 1
            fingerprints.append((url, page.fingerprint))
            # Detect a pagination endpoint returning the same first page or
            # redirecting every page back to the homepage.
            if final in read_urls:
                source["unresolved_pagination"] += 1
                continue
            read_urls.add(final)
            if kind == "listing":
                if page.fingerprint in listing_fingerprints[owner]:
                    source["unresolved_pagination"] += 1
                    continue
                listing_fingerprints[owner].add(page.fingerprint)
            parser = _OfficialListingParser()
            parser.feed(getattr(page, "raw_text", ""))
            parser.close()
            found, structured_links = _detail_candidates(parser, page, company, hint)
            for candidate in found:
                candidates[(candidate.final_url, candidate.job["title"].casefold())] = candidate
            if kind == "detail" and not found:
                source["unparsed_detail_pages"] += 1
            if parser.opaque_more:
                source["unresolved_pagination"] += 1
            listing_text = page.text if kind == "listing" and not found else ""
            total = re.search(r"(?:共\s*|total\s*:?\s*)(\d+)\s*(?:个|条)?\s*(?:岗位|职位|jobs?|positions?)", listing_text, re.IGNORECASE)
            if total:
                totals[owner].add(int(total[1]))
            counter = re.search(r"(?:第\s*(\d+)\s*页\s*[/,，]?\s*共\s*(\d+)\s*页|page\s+(\d+)\s+of\s+(\d+))", listing_text, re.IGNORECASE)
            if counter:
                page_numbers[owner].add(int(counter[1] or counter[3]))
                page_totals[owner].add(int(counter[2] or counter[4]))
            if re.search(r"暂无(?:在招)?(?:岗位|职位)|未找到(?:相关)?(?:岗位|职位)|no (?:jobs|positions|vacancies) found", listing_text, re.IGNORECASE):
                totals[owner].add(0)

            links: list[tuple[str, str, str]] = [(href, title, "detail") for href, title in structured_links]
            links.extend((href, "", "listing") for href in parser.next_links)
            for anchor in parser.anchors:
                label = clean_text(" ".join(anchor.parts) or anchor.title_attribute, limit=280)
                href = anchor.href
                if href in parser.disabled_links:
                    continue
                if re.fullmatch(r"申请职位|申请岗位|立即申请|立即投递|投递简历|apply(?: now)?", label, re.IGNORECASE):
                    continue
                if _NEXT_WORDS.fullmatch(label) or _LIST_WORDS.fullmatch(label):
                    links.append((href, label, "listing"))
                elif label.isdigit() and re.search(r"[?&](?:page|p|pageNo|pageIndex)=\d+|/page/\d+", href, re.IGNORECASE):
                    links.append((href, "", "listing"))
                elif _concrete_title(label) or _DETAIL_PATH.search(href):
                    links.append((href, label, "detail"))
                elif _LIST_WORDS.search(label):
                    links.append((href, label, "listing"))
            for href, label, link_kind in links:
                target = _official_link(href, final)
                if not target:
                    source["unresolved_pagination" if link_kind == "listing" else "blocked_detail_links"] += 1
                    continue
                if link_kind == "detail":
                    detail_urls[owner].add(target)
                if target not in queued and target not in read_urls:
                    queued.add(target)
                    (details if link_kind == "detail" else lists).append((target, owner, label))
        except (WatchFetchError, OSError, ValueError, TypeError):
            # Deliberately persist stable reason codes, never raw exceptions
            # containing tokens, private links or response bodies.
            source[f"{kind}_failures"] += 1

    for queue, field in ((lists, "deferred_listing_pages"), (details, "deferred_detail_pages")):
        for _, owner, _ in queue:
            sources[owner][field] += 1
    for index, source in enumerate(sources):
        if source["status"] == "discovery_limited":
            continue
        total_accounted = len(totals[index]) == 1 and next(iter(totals[index])) == len(detail_urls[index])
        pages_accounted = (
            len(page_totals[index]) == 1 and 0 < next(iter(page_totals[index])) <= max_listing_pages
            and page_numbers[index] == set(range(1, next(iter(page_totals[index])) + 1))
        )
        source["listed_detail_urls"] = len(detail_urls[index])
        source["reported_totals"] = sorted(totals[index])
        metadata_accounted = bool(totals[index] or page_totals[index]) and (
            (not totals[index] or total_accounted) and (not page_totals[index] or pages_accounted)
        )
        source["pagination_complete"] = bool(
            metadata_accounted and not source["listing_failures"]
            and not source["deferred_listing_pages"] and not source["unresolved_pagination"]
        )
        complete = source["pagination_complete"] and not any(source[key] for key in (
            "detail_failures", "deferred_detail_pages", "unparsed_detail_pages", "blocked_detail_links",
        ))
        source["status"] = "healthy" if complete else (
            "failed" if not source["listing_pages_fetched"] and not source["detail_pages_fetched"] else "partial"
        )
        source["reason"] = "linked_list_complete" if complete else (
            stop_reason if source["deferred_listing_pages"] or source["deferred_detail_pages"] else "unconfirmed_or_failed_pages"
        )
    coverage = {
        "scope": "linked_official_lists", "employer": _redact_text(company, limit=160),
        "source_count": len(sources), "sources": sources,
        "listing_page_budget": max_listing_pages, "detail_page_budget": max_detail_pages,
        "pagination_complete": bool(sources) and all(source["pagination_complete"] for source in sources),
        "status": (
            "discovery_limited" if not sources else
            "healthy" if all(source["status"] == "healthy" for source in sources) else
            "failed" if all(source["status"] == "failed" for source in sources) else "partial"
        ),
        "candidate_count": len(candidates),
        "completion_reason": stop_reason if sources else "no_official_entry_point",
        # Generic links are never an authoritative whole-company inventory.
        "snapshot_complete": False,
    }
    digest = hashlib.sha256(json.dumps(
        [sorted(fingerprints), [item.job for item in candidates.values()], coverage],
        ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    return OfficialJobDiscoveryBatch(tuple(candidates.values()), coverage, digest)


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
