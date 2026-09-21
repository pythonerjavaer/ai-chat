"""Fallback parsing of public metadata only; article content is never parsed."""

from __future__ import annotations

import html
import inspect
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Awaitable, Callable

from .fetcher import PublicPageResult, fetch_public_page, metadata_prefix
from .models import WechatArticleMetadata
from .normalizer import UnsafeWechatURLError, normalize_wechat_url


logger = logging.getLogger(__name__)
# Keep all public extraction strategies together; no scattered selectors.
TITLE_METADATA_KEYS = ("og:title", "twitter:title")
TITLE_DOM_IDS = frozenset({"activity-name"})
TITLE_DOM_CLASSES = frozenset({"rich_media_title"})
SOURCE_METADATA_KEYS = ("og:article:author", "wechat:account", "weixin:account", "account_name")
SOURCE_DOM_IDS = frozenset({"js_name", "js_wx_follow_nickname"})
SOURCE_DOM_CLASSES = frozenset({"rich_media_meta_nickname"})
DATE_METADATA_KEYS = ("article:published_time", "og:published_time", "publish_time", "datepublished")
_GENERIC_TITLES = frozenset({
    "微信公众平台", "微信公众号", "wechat", "访问验证", "安全验证", "环境异常",
    "该内容已被发布者删除", "此内容已被发布者删除", "内容已删除", "内容不存在",
})
_BLOCKED_MARKERS = (
    "当前环境异常", "完成验证后即可继续访问", "环境异常，请完成验证", "访问过于频繁",
    "为了保护您的网络安全", "请输入验证码",
)
_CAPTCHA_CONTROL = re.compile(
    r"<(?:input|form|iframe)\b[^>]*(?:id|name)\s*=\s*[\"'](?:captcha|verifycode|seccode)[\"']",
    re.IGNORECASE,
)


def _clean(value: str | None, limit: int) -> str | None:
    cleaned = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return cleaned[:limit] if cleaned else None


class _MetadataHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.dom_titles: list[str] = []
        self.sources: list[str] = []
        self.html_titles: list[str] = []
        self.dates: list[str] = []
        self._stack: list[tuple[str, set[str]]] = []
        self._captures: dict[str, list[str]] = {}
        self._ignore = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or attributes.get("itemprop", "")).casefold()
            if key and attributes.get("content") and key not in self.metadata:
                self.metadata[key] = attributes["content"]
            return
        if tag in {"br", "img", "input", "link", "hr", "source", "wbr", "area", "embed", "param"}:
            return
        if tag in {"script", "style", "noscript", "template"}:
            self._ignore += 1
        kinds: set[str] = set()
        if not self._ignore:
            identifier = attributes.get("id", "")
            classes = set(attributes.get("class", "").split())
            if identifier in TITLE_DOM_IDS or classes & TITLE_DOM_CLASSES:
                kinds.add("dom_title")
            if identifier in SOURCE_DOM_IDS or classes & SOURCE_DOM_CLASSES:
                kinds.add("source")
            if tag == "title":
                kinds.add("html_title")
            if tag == "time" and identifier == "publish_time" and attributes.get("datetime"):
                self.dates.append(attributes["datetime"])
            for kind in kinds:
                self._captures.setdefault(kind, [])
        self._stack.append((tag, kinds))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1][0] == tag:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._ignore:
            return
        for kind, parts in self._captures.items():
            if sum(len(item) for item in parts) < 1_000:
                parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self._ignore = max(0, self._ignore - 1)
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] != tag:
                continue
            closing = self._stack[index:]
            del self._stack[index:]
            for _, kinds in reversed(closing):
                for kind in kinds:
                    value = "".join(self._captures.pop(kind, []))
                    target = {"dom_title": self.dom_titles, "source": self.sources, "html_title": self.html_titles}[kind]
                    target.append(value)
            break


def extract_title_from_metadata(parsed: _MetadataHTMLParser) -> str | None:
    for key in TITLE_METADATA_KEYS:
        value = _clean(parsed.metadata.get(key), 300)
        if value and value.casefold() not in _GENERIC_TITLES:
            return value
    return None


def extract_title_from_dom(parsed: _MetadataHTMLParser) -> str | None:
    return next((value for raw in parsed.dom_titles if (value := _clean(raw, 300)) and value.casefold() not in _GENERIC_TITLES), None)


def extract_title_from_html_title(parsed: _MetadataHTMLParser) -> str | None:
    return next((value for raw in parsed.html_titles if (value := _clean(raw, 300)) and value.casefold() not in _GENERIC_TITLES), None)


def _published_at(parsed: _MetadataHTMLParser, prefix: str) -> datetime | None:
    values = [parsed.metadata.get(key, "") for key in DATE_METADATA_KEYS] + parsed.dates
    # A literal ct assignment is a known public WeChat publication timestamp.
    # Match only the literal; never execute scripts or read arbitrary dates.
    timestamp = re.search(r"\b(?:var|let|const)\s+ct\s*=\s*[\"']?([0-9]{10})[\"']?\s*;", prefix)
    if timestamp:
        values.append(timestamp.group(1))
    for value in values:
        try:
            if re.fullmatch(r"[0-9]{10}", value):
                date_value = datetime.fromtimestamp(int(value), tz=timezone.utc)
            else:
                date_value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                # An unzoned date/time does not establish a publication instant.
                if date_value.tzinfo is None:
                    continue
            if 2000 <= date_value.year <= datetime.now(timezone.utc).year + 1:
                return date_value
        except (ValueError, OverflowError, OSError):
            continue
    return None


MetadataFetcher = Callable[[str], Awaitable[PublicPageResult]]


async def parse_wechat_article_metadata(
    url: str, expected_source_name: str | None = None, *, fetcher: MetadataFetcher | None = None,
) -> WechatArticleMetadata:
    try:
        normalized = normalize_wechat_url(url)
    except UnsafeWechatURLError as exc:
        return WechatArticleMetadata(url="", normalized_url="", fetch_status="unsafe_url", error=str(exc))
    expected = _clean(expected_source_name, 160)
    base = {"url": normalized, "normalized_url": normalized, "source_name": expected,
            "source_name_detection": "configured" if expected else "unknown"}
    result = (fetcher or fetch_public_page)(normalized)
    page = await result if inspect.isawaitable(result) else result
    if page.fetch_status != "success":
        return WechatArticleMetadata(**base, fetch_status=page.fetch_status, error=page.error)
    try:
        final_url = normalize_wechat_url(page.final_url)
    except UnsafeWechatURLError:
        return WechatArticleMetadata(**base, fetch_status="unsafe_url", error="文章最终链接未通过安全检查。")
    base.update(url=final_url, normalized_url=final_url)
    prefix = metadata_prefix(page.metadata_html)
    if any(marker in prefix.casefold() for marker in _BLOCKED_MARKERS) or _CAPTCHA_CONTROL.search(prefix):
        return WechatArticleMetadata(**base, fetch_status="blocked", error="公开页面要求访问验证；未尝试绕过。")
    parsed = _MetadataHTMLParser()
    try:
        parsed.feed(prefix)
        parsed.close()
    except (ValueError, AssertionError):
        return WechatArticleMetadata(**base, fetch_status="parse_failed", error="公开页面元数据无法解析。")
    title = extract_title_from_metadata(parsed) or extract_title_from_dom(parsed) or extract_title_from_html_title(parsed)
    source = next((_clean(parsed.metadata.get(key), 160) for key in SOURCE_METADATA_KEYS if _clean(parsed.metadata.get(key), 160)), None)
    source = source or next((_clean(value, 160) for value in parsed.sources if _clean(value, 160)), None)
    if source:
        base.update(source_name=source, source_name_detection="page")
    status = "success" if title else "parse_failed"
    logger.info("wechat_metadata_parse", extra={
        "source": base["source_name"], "url": final_url, "parse_result": status,
        "source_name_detection": base["source_name_detection"],
    })
    return WechatArticleMetadata(
        **base, title=title, published_at=_published_at(parsed, prefix), fetch_status=status,
        error=None if title else "未从公开元数据或标题元素中找到文章标题。",
    )
