"""Pluggable WeChat URL discovery providers."""

from .base import DiscoveryProviderUnavailable, WechatDiscoveryProvider
from .manual import ManualDiscoveryProvider
from .search import SearchDiscoveryProvider

__all__ = [
    "DiscoveryProviderUnavailable", "WechatDiscoveryProvider",
    "ManualDiscoveryProvider", "SearchDiscoveryProvider",
]
