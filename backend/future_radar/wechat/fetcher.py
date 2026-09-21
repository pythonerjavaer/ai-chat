"""Bounded public metadata prefix fetching, with no login or body retention."""

from __future__ import annotations

import asyncio
import codecs
import inspect
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from .models import FetchStatus
from .normalizer import UnsafeWechatURLError, validate_wechat_article_url


logger = logging.getLogger(__name__)
MAX_METADATA_BYTES = 192_000
MAX_REDIRECTS = 3
# Stop before the first article-body container. The HTTP client may receive a
# network chunk spanning this boundary; that suffix is immediately discarded,
# never parsed, returned, logged, or persisted. The response is then closed.
_BODY_START = re.compile(
    r"<article\b|<(?:div|section)\b[^>]*\b(?:"
    r"id\s*=\s*(?:[\"']js_content[\"']|js_content(?=[\s>]))|"
    r"class\s*=\s*(?:[\"'][^\"']*\brich_media_content\b|"
    r"rich_media_content(?=[\s/>])))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PublicPageResult:
    url: str
    final_url: str
    fetch_status: FetchStatus
    metadata_html: str = ""
    error: str | None = None
    status_code: int | None = None
    truncated: bool = False


Resolver = Callable[[str], Awaitable[Iterable[str]] | Iterable[str]]


def metadata_prefix(html: str) -> str:
    """Pure guard also applied to injected/local parser inputs."""
    limited = html[:MAX_METADATA_BYTES]
    boundary = _BODY_START.search(limited)
    return limited[:boundary.start()] if boundary else limited


async def _resolve(hostname: str) -> list[str]:
    result = await asyncio.get_running_loop().getaddrinfo(
        hostname, 443, type=socket.SOCK_STREAM,
    )
    return list({str(item[4][0]) for item in result})


async def _check_public_dns(url: str, resolver: Resolver) -> None:
    hostname = urlsplit(url).hostname or ""
    resolved = resolver(hostname)
    addresses = await resolved if inspect.isawaitable(resolved) else resolved
    try:
        values = [ipaddress.ip_address(value) for value in addresses]
    except (ValueError, TypeError) as exc:
        raise UnsafeWechatURLError("文章域名没有有效的公网地址。") from exc
    if not values or any(not address.is_global for address in values):
        raise UnsafeWechatURLError("文章域名解析到了非公网地址。")


async def _read_metadata_prefix(response: httpx.Response, max_bytes: int) -> tuple[str, bool]:
    charset = response.charset_encoding or "utf-8"
    try:
        decoder = codecs.getincrementaldecoder(charset)(errors="replace")
    except LookupError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts = ""
    consumed = 0
    async for chunk in response.aiter_bytes(chunk_size=1_024):
        remaining = max_bytes - consumed
        if remaining <= 0:
            return parts, True
        piece = chunk[:remaining]
        consumed += len(piece)
        parts += decoder.decode(piece)
        boundary = _BODY_START.search(parts)
        if boundary:
            return parts[:boundary.start()], True
        if consumed >= max_bytes:
            return parts, True
    parts += decoder.decode(b"", final=True)
    return parts, False


async def fetch_public_page(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None, timeout_seconds: float = 12.0,
    max_bytes: int = MAX_METADATA_BYTES, max_attempts: int = 2,
) -> PublicPageResult:
    """Fetch only the bounded prefix of a public article.

    Redirects are followed manually so *every* hop is article-allowlisted and
    DNS-checked before connecting. Only timeout/network/502/503/504 errors may
    receive one retry. Captchas, 403 and 429 never trigger evasion or retries.
    """
    if not 0 < timeout_seconds <= 60 or not 1 <= max_bytes <= MAX_METADATA_BYTES:
        raise ValueError("metadata fetch limits are out of bounds")
    if not 1 <= max_attempts <= 2:
        raise ValueError("max_attempts must be 1 or 2")
    started = time.monotonic()
    safe_url = ""
    try:
        safe_url = validate_wechat_article_url(url)
    except UnsafeWechatURLError as exc:
        return PublicPageResult("", "", "unsafe_url", error=str(exc))
    result = PublicPageResult(safe_url, safe_url, "network_error", error="公开文章暂时无法访问。")
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(
                transport=transport, follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(timeout_seconds),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
                headers={
                    "User-Agent": "FrostFire-Recruitment-Metadata/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            ) as client:
                for attempt in range(max_attempts):
                    try:
                        result = await _fetch_attempt(
                            client, safe_url, resolver or _resolve, max_bytes,
                        )
                    except (httpx.TimeoutException, TimeoutError):
                        result = PublicPageResult(safe_url, safe_url, "timeout", error="公开文章读取超时。")
                    except (httpx.RequestError, OSError):
                        result = PublicPageResult(safe_url, safe_url, "network_error", error="公开文章网络连接失败。")
                    except UnsafeWechatURLError as exc:
                        result = PublicPageResult(safe_url, safe_url, "unsafe_url", error=str(exc))
                    transient = (
                        result.fetch_status in {"timeout", "network_error"}
                        or result.status_code in {502, 503, 504}
                    )
                    if not transient or attempt + 1 >= max_attempts:
                        break
                    await asyncio.sleep(0.15)
    except TimeoutError:
        result = PublicPageResult(safe_url, safe_url, "timeout", error="公开文章读取超时。")
    logger.info("wechat_metadata_fetch", extra={
        "source": "wechat", "url": safe_url, "fetch_result": result.fetch_status,
        "http_status": result.status_code, "duration_ms": round((time.monotonic() - started) * 1_000),
    })
    return result


async def _fetch_attempt(
    client: httpx.AsyncClient, url: str, resolver: Resolver, max_bytes: int,
) -> PublicPageResult:
    current = url
    seen: set[str] = set()
    for redirect_number in range(MAX_REDIRECTS + 1):
        if current in seen:
            return PublicPageResult(url, current, "http_error", error="文章页面发生循环跳转。")
        seen.add(current)
        await _check_public_dns(current, resolver)
        # A public read must not silently acquire/use a WeChat session cookie.
        client.cookies.clear()
        async with client.stream("GET", current) as response:
            status = response.status_code
            if status in {301, 302, 303, 307, 308}:
                if redirect_number >= MAX_REDIRECTS or not response.headers.get("location"):
                    return PublicPageResult(url, current, "http_error", error="文章页面跳转次数过多或目标缺失。", status_code=status)
                current = validate_wechat_article_url(urljoin(current, response.headers["location"]))
                continue
            if status < 200 or status >= 300:
                outcome: FetchStatus = "http_403" if status == 403 else "http_404" if status == 404 else "http_error"
                return PublicPageResult(url, current, outcome, error=f"公开文章返回 HTTP {status}。", status_code=status)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                return PublicPageResult(url, current, "parse_failed", error="公开文章未返回 HTML 元数据页面。", status_code=status)
            prefix, truncated = await _read_metadata_prefix(response, max_bytes)
            return PublicPageResult(url, current, "success", metadata_html=prefix, status_code=status, truncated=truncated)
    return PublicPageResult(url, current, "http_error", error="文章页面跳转未完成。")
