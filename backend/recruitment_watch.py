"""Zero-token monitoring for public recruitment pages.

The monitor deliberately uses deterministic HTML extraction, keyword matching,
and SHA-256 fingerprints.  It never sends fetched content to an AI provider.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Iterable


DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_BYTES = 1_500_000
ALLOWED_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "text/plain", "application/json",
    "application/ld+json",
)


class WatchFetchError(ValueError):
    """A safe, user-displayable watch validation or fetch error."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


def normalize_html_text(value: str) -> str:
    """Extract stable visible text and collapse insignificant whitespace."""
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
        visible = parser.text()
    except Exception as exc:
        raise WatchFetchError("页面 HTML 无法解析。") from exc
    return re.sub(r"\s+", " ", visible).strip()


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    folded = text.casefold()
    hits: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        normalized = re.sub(r"\s+", " ", str(keyword)).strip()
        key = normalized.casefold()
        if key and key not in seen and key in folded:
            hits.append(normalized)
            seen.add(key)
    return hits


def _resolved_addresses(hostname: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WatchFetchError("无法解析监控网址的域名。") from exc
    addresses = set()
    for record in records:
        try:
            addresses.add(ipaddress.ip_address(record[4][0]))
        except ValueError as exc:
            raise WatchFetchError("监控网址解析到了无效地址。") from exc
    if not addresses:
        raise WatchFetchError("监控网址没有可用的公网地址。")
    return addresses


def validate_public_https_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate an HTTPS URL and reject local, private, or special networks."""
    candidate = str(url).strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise WatchFetchError("监控网址格式无效。") from exc
    if parsed.scheme.lower() != "https":
        raise WatchFetchError("监控网址必须使用 HTTPS。")
    if not parsed.hostname or parsed.username or parsed.password:
        raise WatchFetchError("监控网址不能包含账号信息，且必须包含有效域名。")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise WatchFetchError("不能监控本机或局域网地址。")
    if port not in (None, 443):
        raise WatchFetchError("监控网址只允许标准 HTTPS 端口。")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise WatchFetchError("不能监控内网、回环或保留地址。")
    if resolve_dns:
        addresses = _resolved_addresses(hostname)
        if any(not address.is_global for address in addresses):
            raise WatchFetchError("监控域名解析到了内网、回环或保留地址。")
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc, parsed.path or "/", parsed.query, "")
    )


def normalize_public_https_urls(
    url: str,
    *,
    resolve_dns: bool = True,
) -> tuple[str, str]:
    """Return a user-facing URL and a fragment-free URL used for HTTP fetches."""
    candidate = str(url).strip()
    original = urllib.parse.urlsplit(candidate)
    fetch_url = validate_public_https_url(candidate, resolve_dns=resolve_dns)
    fetch_parts = urllib.parse.urlsplit(fetch_url)
    display_url = urllib.parse.urlunsplit(
        (
            fetch_parts.scheme,
            fetch_parts.netloc,
            fetch_parts.path,
            fetch_parts.query,
            original.fragment,
        )
    )
    return display_url, fetch_url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url = validate_public_https_url(
            urllib.parse.urljoin(req.full_url, newurl),
            resolve_dns=True,
        )
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


@dataclass(frozen=True)
class WatchFetchResult:
    url: str
    final_url: str
    fingerprint: str
    keyword_hits: list[str]
    content_bytes: int
    http_status: int
    text: str = ""


def fetch_watch_page(
    url: str,
    keywords: Iterable[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    opener_factory: Callable[..., urllib.request.OpenerDirector] = urllib.request.build_opener,
) -> WatchFetchResult:
    """Fetch and fingerprint a public page without invoking an AI model."""
    safe_url = validate_public_https_url(url, resolve_dns=True)
    opener = opener_factory(_SafeRedirectHandler())
    request = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "FrostFire-Recruitment-Watch/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
        },
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            final_url = validate_public_https_url(response.geturl(), resolve_dns=True)
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise WatchFetchError(f"监控页面返回 HTTP {status}。")
            content_type = response.headers.get_content_type().lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise WatchFetchError("监控网址没有返回可读取的网页文本。")
            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    content_length = int(length_header)
                except ValueError:
                    content_length = None
                if content_length is not None and content_length > max_bytes:
                    raise WatchFetchError("监控页面超过允许的大小。")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise WatchFetchError("监控页面超过允许的大小。")
            charset = response.headers.get_content_charset() or "utf-8"
    except WatchFetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise WatchFetchError(f"监控页面返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WatchFetchError("监控页面暂时无法访问。") from exc

    try:
        html = payload.decode(charset, errors="replace")
    except LookupError:
        html = payload.decode("utf-8", errors="replace")
    text = normalize_html_text(html)
    if not text:
        raise WatchFetchError("监控页面没有可比较的文本内容。")
    return WatchFetchResult(
        url=safe_url,
        final_url=final_url,
        fingerprint=content_fingerprint(text),
        keyword_hits=keyword_hits(text, keywords),
        content_bytes=len(payload),
        http_status=status,
        text=text,
    )
