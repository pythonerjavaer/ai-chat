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
            self.item = {"title": "", "source_name": "", "href": "", "published_at": None}
            self.depth = 1
            return
        if self.item is None:
            return
        self.depth += 1
        if tag == "div" and "s-p" in classes:
            self.in_source_box = self.depth
        if tag == "h3":
            self.in_h3 = self.depth
        if tag == "a" and self.in_h3 and not self.item["href"] and attrs.get("href"):
            self.item["href"] = attrs["href"]
            self.capture, self.capture_depth, self.buffer = "title", self.depth, []
        elif tag == "a" and self.in_source_box and not self.item["source_name"]:
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
        self.depth -= 1


def parse_sogou_results(document: str, configured_source: str, *, now: datetime | None = None) -> list[DiscoveredArticle]:
    parser = _SogouResultsParser()
    parser.feed(document)
    now = now or datetime.now(timezone.utc)
    recent = now - timedelta(days=14)
    items: list[DiscoveredArticle] = []
    for row in parser.results:
        if not exact_source_match(configured_source, row["source_name"]):
            continue
        published = row["published_at"]
        if published and published < recent:
            continue
        discovery_url = urljoin("https://weixin.sogou.com/", row["href"])
        if urlsplit(discovery_url).hostname != "weixin.sogou.com":
            continue
        items.append(DiscoveredArticle(
            url=discovery_url, discovery_url=discovery_url, title=row["title"],
            source_name=row["source_name"], expected_source_name=configured_source,
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
        items = parse_sogou_results(document, source_name)
        for item in items:
            item.article_url = await self.resolve_article_url(item.discovery_url or item.url)
        return items

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
