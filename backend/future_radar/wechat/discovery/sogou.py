"""Low-frequency public Sogou WeChat discovery without cookies or bypasses."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from ..models import DiscoveredArticle
from .base import DiscoveryProviderUnavailable, WechatDiscoveryProvider

SEARCH_URL = "https://weixin.sogou.com/weixin"
MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024
_BLOCK_MARKERS = ("antispider", "请输入验证码", "用户您好，我们的系统检测到您网络中存在异常访问请求")


def normalize_source_name(value: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def exact_source_match(configured: str, found: str | None) -> bool:
    return bool(found) and normalize_source_name(configured) == normalize_source_name(found)


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


class _SogouResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, Any]] = []
        self.item: dict[str, Any] | None = None
        self.depth = 0
        self.capture: str | None = None
        self.capture_depth = 0
        self.buffer: list[str] = []
        self.in_source_box = 0
        self.in_h3 = 0
        self.script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        if tag == "li" and self.item is None:
            self.item = {"title": "", "source_name": "", "href": "", "published_at": None, "raw_date": None}
            self.depth = 1
            return
        if self.item is None:
            return
        # HTMLParser does not emit end tags for void elements (notably the
        # thumbnail <img> in every Sogou result).  Counting one as a nested
        # element made every later closing tag off by one and silently yielded
        # zero parsed results in production.
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.depth += 1
        if tag == "div" and "s-p" in classes:
            self.in_source_box = self.depth
        if tag == "h3":
            self.in_h3 = self.depth
        if tag == "a" and self.in_h3 and not self.item["href"] and attrs.get("href"):
            self.item["href"] = attrs["href"]
            self.capture, self.capture_depth, self.buffer = "title", self.depth, []
        elif tag in {"a", "span"} and self.in_source_box and not self.item["source_name"]:
            self.capture, self.capture_depth, self.buffer = "source_name", self.depth, []
        if tag == "script":
            self.script = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.buffer.append(data)
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.item is None:
            return
        if tag == "script" and self.script is not None:
            match = re.search(r"timeConvert\(['\"]?(\d{9,13})", "".join(self.script))
            if match:
                stamp = int(match.group(1))
                if stamp > 10_000_000_000:
                    stamp //= 1000
                self.item["raw_date"] = match.group(1)
                self.item["published_at"] = datetime.fromtimestamp(stamp, timezone.utc)
            self.script = None
        if self.capture and self.depth == self.capture_depth:
            self.item[self.capture] = _text("".join(self.buffer))
            self.capture, self.buffer = None, []
        if self.in_source_box == self.depth:
            self.in_source_box = 0
        if self.in_h3 == self.depth:
            self.in_h3 = 0
        if tag == "li" and self.depth == 1:
            if self.item["title"] and self.item["href"]:
                self.results.append(self.item)
            self.item = None
            self.depth = 0
            return
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.depth -= 1


def parse_sogou_candidates(document: str, configured_source: str, *, now: datetime | None = None,
                           window_days: int = 14) -> list[dict[str, Any]]:
    """Return bounded structured decisions, never raw HTML or a DOM tree."""
    parser = _SogouResultsParser()
    parser.feed(document)
    now = now or datetime.now(timezone.utc)
    recent = now - timedelta(days=window_days)
    candidates: list[dict[str, Any]] = []
    for row in parser.results[:20]:
        source_name, title = row.get("source_name"), row.get("title")
        discovery_url = urljoin("https://weixin.sogou.com/", row.get("href") or "")
        reason = "accepted"
        if not title:
            reason = "missing_title"
        elif not source_name:
            reason = "missing_source"
        elif not exact_source_match(configured_source, source_name):
            reason = "source_name_mismatch"
        elif not row.get("published_at"):
            reason = "date_parse_failed"
        elif row["published_at"] < recent:
            reason = "date_out_of_range"
        elif urlsplit(discovery_url).hostname != "weixin.sogou.com":
            reason = "invalid_result"
        candidates.append({
            "raw_title": title or None, "raw_source_name": source_name or None,
            "normalized_source_name": normalize_source_name(source_name),
            "raw_date": row.get("raw_date"), "published_at": row.get("published_at"),
            "discovery_url": discovery_url or None, "resolved_wechat_url": None,
            "accepted": reason == "accepted", "rejection_reason": reason,
        })
    return candidates


def parse_sogou_results(document: str, configured_source: str, *, now: datetime | None = None,
                        window_days: int = 14) -> list[DiscoveredArticle]:
    items: list[DiscoveredArticle] = []
    for row in parse_sogou_candidates(document, configured_source, now=now, window_days=window_days):
        if not row["accepted"]:
            continue
        published = row["published_at"]
        discovery_url = row["discovery_url"]
        items.append(DiscoveredArticle(
            url=discovery_url, discovery_url=discovery_url, title=row["raw_title"],
            source_name=row["raw_source_name"], expected_source_name=configured_source,
            published_at=published, provider="sogou_wechat",
        ))
    return items


Requester = Callable[..., Awaitable[Any]]


class SogouWechatDiscoveryProvider(WechatDiscoveryProvider):
    name = "sogou_wechat"

    def __init__(self, requester: Requester | None = None) -> None:
        self.requester = requester or self._request

    @staticmethod
    async def _request(url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            async with client.stream("GET", url, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_SEARCH_RESPONSE_BYTES:
                        raise DiscoveryProviderUnavailable("公开搜索响应超过安全大小限制。")
                    body.extend(chunk)
                headers = {
                    key: value for key, value in response.headers.items()
                    if key.casefold() not in {"content-encoding", "content-length"}
                }
                return httpx.Response(
                    response.status_code, headers=headers, content=bytes(body),
                    request=httpx.Request("GET", str(response.url)),
                )

    @staticmethod
    def _validate_response(response: Any) -> str:
        status = int(response.status_code)
        final_url = str(response.url)
        document = response.text
        if len(document.encode("utf-8", errors="ignore")) > MAX_SEARCH_RESPONSE_BYTES:
            raise DiscoveryProviderUnavailable("公开搜索响应超过安全大小限制。")
        lowered = (final_url + "\n" + document[:12000]).casefold()
        if status in {403, 429} or any(marker.casefold() in lowered for marker in _BLOCK_MARKERS):
            raise DiscoveryProviderUnavailable(f"公开搜索访问受限（HTTP {status}）。")
        if status != 200:
            raise DiscoveryProviderUnavailable(f"公开搜索暂不可用（HTTP {status}）。")
        if "news-list" not in document:
            raise DiscoveryProviderUnavailable("公开搜索返回了无法识别的页面。")
        return document

    async def discover(self, source_account: Mapping[str, Any]) -> list[DiscoveredArticle]:
        items, _ = await self.discover_with_debug(source_account)
        return items

    async def discover_with_debug(self, source_account: Mapping[str, Any], *, window_days: int = 14) -> tuple[list[DiscoveredArticle], list[dict[str, Any]]]:
        source_name = str(source_account.get("source_name") or source_account.get("name") or "").strip()
        if not source_name:
            return []
        try:
            response = await self.requester(SEARCH_URL, params={"type": "2", "query": source_name, "page": "1"})
        except DiscoveryProviderUnavailable:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise DiscoveryProviderUnavailable("公开搜索连接超时或暂不可达。") from None
        document = self._validate_response(response)
        candidates = parse_sogou_candidates(document, source_name, window_days=window_days)
        items = parse_sogou_results(document, source_name, window_days=window_days)
        by_url = {item.discovery_url: item for item in items}
        for item in items:
            item.article_url = await self.resolve_article_url(item.discovery_url or item.url)
            candidate = next((row for row in candidates if row["discovery_url"] == item.discovery_url), None)
            if candidate is not None:
                candidate["resolved_wechat_url"] = item.article_url
                if not item.article_url:
                    # The discovery record remains valid, but expose that the
                    # optional normal redirect did not resolve to a direct URL.
                    candidate["rejection_reason"] = "resolve_failed"
        return items, candidates

    async def resolve_article_url(self, discovery_url: str) -> str | None:
        if urlsplit(discovery_url).hostname != "weixin.sogou.com":
            return None
        try:
            response = await self.requester(discovery_url)
        except (httpx.HTTPError, TimeoutError):
            return None
        final = str(response.url)
        parsed = urlsplit(final)
        if parsed.scheme == "https" and parsed.hostname == "mp.weixin.qq.com" and parsed.path.startswith("/s"):
            return final
        return None
