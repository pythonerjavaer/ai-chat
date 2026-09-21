"""Small connector facade; persistence and scheduling belong to services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from .discovery.base import WechatDiscoveryProvider
from .discovery.manual import ManualDiscoveryProvider
from .fetcher import PublicPageResult, fetch_public_page
from .models import DiscoveredArticle, WechatArticleMetadata
from .parser import MetadataFetcher, parse_wechat_article_metadata


class SourceConnector(ABC):
    @abstractmethod
    async def discover(self, source_account: Mapping[str, Any]) -> list[DiscoveredArticle]:
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, url: str) -> PublicPageResult:
        raise NotImplementedError

    @abstractmethod
    async def parse(self, url: str, expected_source_name: str | None = None) -> WechatArticleMetadata:
        raise NotImplementedError


class WechatArticleConnector(SourceConnector):
    def __init__(
        self, discovery_provider: WechatDiscoveryProvider | None = None,
        *, fetcher: MetadataFetcher | None = None,
    ) -> None:
        self.discovery_provider = discovery_provider or ManualDiscoveryProvider()
        self.fetcher = fetcher or fetch_public_page

    async def discover(self, source_account: Mapping[str, Any]) -> list[DiscoveredArticle]:
        return await self.discovery_provider.discover(source_account)

    async def fetch(self, url: str) -> PublicPageResult:
        return await self.fetcher(url)

    async def parse(self, url: str, expected_source_name: str | None = None) -> WechatArticleMetadata:
        return await parse_wechat_article_metadata(url, expected_source_name, fetcher=self.fetcher)
