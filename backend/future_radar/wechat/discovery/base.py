"""Discovery stays independent of article metadata fetching."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from ..models import DiscoveredArticle


class WechatDiscoveryProvider(ABC):
    @abstractmethod
    async def discover(self, source_account: Mapping[str, Any]) -> list[DiscoveredArticle]:
        raise NotImplementedError


class DiscoveryProviderUnavailable(RuntimeError):
    """No suitable free public automatic-discovery provider is configured."""
