"""Pluggable WeChat URL discovery providers."""

from .base import DiscoveryProviderUnavailable, WechatDiscoveryProvider
from .manual import ManualDiscoveryProvider
from .search import SearchDiscoveryProvider
from .sogou import SogouWechatDiscoveryProvider

__all__ = [
    "DiscoveryProviderUnavailable", "WechatDiscoveryProvider",
    "ManualDiscoveryProvider", "SearchDiscoveryProvider", "SogouWechatDiscoveryProvider",
]
