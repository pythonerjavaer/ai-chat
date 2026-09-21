"""Metadata contracts for the free, discovery-only WeChat connector."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


FetchStatus = Literal[
    "success", "timeout", "http_403", "http_404", "http_error",
    "network_error", "parse_failed", "blocked", "unsafe_url",
]
RelevanceStatus = Literal["relevant", "possible", "irrelevant"]


class WechatArticleMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["wechat"] = "wechat"
    source_name: str | None = None
    title: str | None = None
    url: str
    normalized_url: str
    published_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fetch_status: FetchStatus
    source_name_detection: Literal["page", "configured", "unknown"] = "unknown"
    error: str | None = None


class TitleClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevance_status: RelevanceStatus
    relevance_score: int = Field(ge=0, le=100)
    matched_keywords: list[str] = Field(default_factory=list)


class DiscoveredArticle(BaseModel):
    """A public URL, not a claim about the article's contents or verification."""

    model_config = ConfigDict(extra="forbid")

    url: str
    expected_source_name: str | None = None
    title: str | None = None
    source_name: str | None = None
    discovery_url: str | None = None
    article_url: str | None = None
    published_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str = "manual"
    found_by_queries: list[str] = Field(default_factory=list)
