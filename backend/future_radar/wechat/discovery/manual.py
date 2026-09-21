"""Manual URL discovery: no network, account login or search API."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..models import DiscoveredArticle
from ..normalizer import normalize_wechat_url
from .base import WechatDiscoveryProvider


class ManualDiscoveryProvider(WechatDiscoveryProvider):
    def __init__(self, urls: Iterable[str] | None = None) -> None:
        self.urls = list(urls) if urls is not None else None

    async def discover(self, source_account: Mapping[str, Any]) -> list[DiscoveredArticle]:
        urls = self.urls
        if urls is None:
            urls = source_account.get("urls") or ([source_account["seed_url"]] if source_account.get("seed_url") else [])
        expected = source_account.get("source_name") or source_account.get("name") or None
        seen: set[str] = set()
        result: list[DiscoveredArticle] = []
        for url in urls:
            canonical = normalize_wechat_url(url)
            if canonical not in seen:
                result.append(DiscoveredArticle(
                    url=canonical, discovery_url=canonical, article_url=canonical,
                    expected_source_name=expected, source_name=expected, provider="manual",
                ))
                seen.add(canonical)
        return result
