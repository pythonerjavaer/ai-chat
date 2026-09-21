"""Strict public article URL identity and deterministic metadata deduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ALLOWED_ARTICLE_HOSTS = frozenset({"mp.weixin.qq.com"})
IDENTITY_QUERY_KEYS = ("__biz", "mid", "idx", "sn")
_SENSITIVE_QUERY_KEYS = frozenset({
    "access_token", "token", "key", "pass_ticket", "password", "auth",
    "authorization", "cookie", "secret", "api_key", "appsecret",
})


class UnsafeWechatURLError(ValueError):
    """The URL is not an allowlisted, credential-free public WeChat article."""


def validate_wechat_article_url(url: str) -> str:
    """Validate and return a canonical article URL, without network access.

    Only the public short article route and the long article identity route
    are accepted. Account backends/profile APIs are not article URLs.
    """
    if not isinstance(url, str) or not url or len(url) > 2_000:
        raise UnsafeWechatURLError("请输入有效的微信公众号公开文章 HTTPS 链接。")
    if any(ord(char) < 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise UnsafeWechatURLError("文章链接包含不安全字符。")
    try:
        parsed = urlsplit(url.strip())
        if (
            parsed.scheme != "https" or parsed.hostname not in ALLOWED_ARTICLE_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise UnsafeWechatURLError("只允许 mp.weixin.qq.com 的公开文章 HTTPS 链接。")
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=50)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, UnsafeWechatURLError):
            raise
        raise UnsafeWechatURLError("文章链接格式无效。") from exc
    if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _ in query):
        raise UnsafeWechatURLError("文章链接不能包含凭据或会话参数。")
    if re.fullmatch(r"/s/[A-Za-z0-9_-]{1,200}/?", parsed.path):
        return urlunsplit(("https", "mp.weixin.qq.com", parsed.path.rstrip("/"), "", ""))
    if parsed.path not in {"/s", "/s/"}:
        raise UnsafeWechatURLError("链接必须指向公开文章，不能指向微信后台或账号接口。")
    identity: dict[str, str] = {}
    for key, value in query:
        if key in IDENTITY_QUERY_KEYS:
            if key in identity and identity[key] != value:
                raise UnsafeWechatURLError("文章链接存在冲突的身份参数。")
            identity[key] = value
    if (
        not re.fullmatch(r"[A-Za-z0-9_+=/-]{1,200}", identity.get("__biz", ""))
        or not re.fullmatch(r"[0-9]{1,32}", identity.get("mid", ""))
        or not re.fullmatch(r"[0-9]{1,5}", identity.get("idx", ""))
        or ("sn" in identity and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity["sn"]))
    ):
        raise UnsafeWechatURLError("文章长链接缺少有效的 __biz、mid 或 idx 身份参数。")
    canonical_query = urlencode([(key, identity[key]) for key in IDENTITY_QUERY_KEYS if key in identity])
    return urlunsplit(("https", "mp.weixin.qq.com", "/s", canonical_query, ""))


def normalize_wechat_url(url: str) -> str:
    return validate_wechat_article_url(url)


def normalize_metadata_text(value: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).casefold()


def metadata_fingerprint(source_name: str | None, title: str | None) -> str:
    """An auxiliary cross-URL duplicate hint, never an account identity proof."""
    material = normalize_metadata_text(source_name) + "\x00" + normalize_metadata_text(title)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
