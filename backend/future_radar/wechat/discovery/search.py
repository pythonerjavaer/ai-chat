"""Extension contract only; no unverified free-search backend is enabled."""

from __future__ import annotations

from abc import abstractmethod
import re
from typing import Any

from ..models import DiscoveredArticle
from .base import WechatDiscoveryProvider


def build_discovery_queries(source: dict[str, Any] | str) -> list[str]:
    """Saveable query suggestions, not calls to a search engine or model."""
    name = source if isinstance(source, str) else source.get("source_name") or source.get("name") or ""
    name = re.sub(r"\s+", " ", str(name)).replace('"', "").strip()[:160]
    if not name:
        return []
    return [
        f'site:mp.weixin.qq.com "{name}" "{term}"'
        for term in ("2027", "2027届", "校园招聘", "招聘", "秋招")
    ]


class SearchDiscoveryProvider(WechatDiscoveryProvider):
    """Future lawful providers must implement search and account attribution.

    Public search HTML may expose titles but fail article-link resolution or
    impose access restrictions. No scraping workaround or paid API fallback
    is silently installed here. Manual discovery is the runnable v1 provider.
    """

    @abstractmethod
    async def search(self, query: str) -> list[DiscoveredArticle]:
        raise NotImplementedError
