"""Strict API schemas for Future Radar."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalization import (
    PRIMARY_CATEGORY_CODES,
    normalize_taxonomy_tags,
    normalize_taxonomy_value,
)


SourceType = Literal[
    "official_html", "official_api", "ats", "wechat_public",
    "openai_web_search", "manual", "other_public_source", "public_feed",
]
ManualScanType = Literal["quick", "deep"]


class RadarProgramInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(default=None, max_length=180)
    company: str = Field(min_length=1, max_length=160)
    program_name: str = Field(min_length=1, max_length=240)
    recruitment_year: int | None = Field(default=None, ge=2020, le=2100)
    recruitment_type: Literal["campus", "autumn", "spring", "internship", "graduate", "other"] = "other"
    region: str = Field(default="", max_length=160)
    opening_date: date | None = None
    closing_date: date | None = None
    status: Literal["open", "closed", "unknown"] = "open"
    verification_status: Literal["pending", "verified", "conflicted", "rejected"] = "pending"
    confidence_score: float = Field(default=0, ge=0, le=1)
    official_url: str | None = Field(default=None, pattern=r"^https://", max_length=2_000)
    evidence: list[Annotated[str, Field(min_length=1, max_length=280)]] = Field(
        default_factory=list, max_length=12
    )

    @field_validator("evidence")
    @classmethod
    def evidence_has_no_contacts(cls, values: list[str]) -> list[str]:
        return _validate_evidence(values)


class RadarJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(default=None, max_length=180)
    program_external_id: str | None = Field(default=None, max_length=180)
    company: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=280)
    city: str = Field(default="", max_length=160)
    region: str = Field(default="", max_length=160)
    employer_type: str = Field(default="", max_length=80)
    industry: str = Field(default="", max_length=120)
    primary_category: str | None = Field(default=None, max_length=80)
    organization_category: str = Field(default="", max_length=80)
    industry_tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=30
    )
    role_tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=30
    )
    official_url: str | None = Field(default=None, pattern=r"^https://", max_length=2_000)
    application_url: str | None = Field(default=None, pattern=r"^https://", max_length=2_000)
    opening_date: date | None = None
    closing_date: date | None = None
    status: Literal["open", "closed", "unknown"] = "open"
    verification_status: Literal["pending", "verified", "conflicted", "rejected"] = "pending"
    confidence_score: float = Field(default=0, ge=0, le=1)
    description: str = Field(default="", max_length=8_000)
    responsibilities: str = Field(default="", max_length=8_000)
    requirements: str = Field(default="", max_length=8_000)
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=30
    )
    evidence: list[Annotated[str, Field(min_length=1, max_length=280)]] = Field(
        default_factory=list, max_length=12
    )

    @field_validator("evidence")
    @classmethod
    def evidence_has_no_contacts(cls, values: list[str]) -> list[str]:
        return _validate_evidence(values)

    @field_validator("primary_category", mode="before")
    @classmethod
    def primary_category_is_supported(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            return value
        normalized = normalize_taxonomy_value(value)
        if normalized not in PRIMARY_CATEGORY_CODES:
            raise ValueError("primary_category must be one of the supported category codes")
        return normalized

    @field_validator("organization_category", mode="before")
    @classmethod
    def organization_category_is_normalized(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            return value
        return normalize_taxonomy_value(value)

    @field_validator("industry_tags", "role_tags", mode="before")
    @classmethod
    def taxonomy_tags_are_normalized(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            return values
        if len(values) > 30:
            raise ValueError("taxonomy tag lists may contain at most 30 items")
        return normalize_taxonomy_tags(values)


class RadarArticleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_external_id: str | None = Field(default=None, max_length=180)
    publisher: str = Field(default="", max_length=160)
    article_title: str = Field(min_length=1, max_length=300)
    article_url: str | None = Field(default=None, pattern=r"^https://", max_length=2_000)
    publish_time: datetime | None = None
    raw_excerpt: str = Field(default="", max_length=1_500)
    is_recruitment: bool = False
    recruitment_year: int | None = Field(default=None, ge=2020, le=2100)
    classification: str = Field(default="unknown", max_length=80)


class FrostFireSyncV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["FROSTFIRE_SYNC_V1"]
    batch_id: str | None = Field(default=None, max_length=180)
    source_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
    )
    source_name: str | None = Field(default=None, max_length=160)
    observed_at: datetime | None = None
    snapshot_complete: bool = False
    programs: list[RadarProgramInput] = Field(default_factory=list, max_length=10)
    jobs: list[RadarJobInput] = Field(default_factory=list, max_length=10)
    articles: list[RadarArticleInput] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def bounded_batch(self):
        if len(self.programs) + len(self.jobs) + len(self.articles) > 20:
            raise ValueError("A sync batch may contain at most 20 total entities.")
        return self


class RadarRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=50
    )
    scan_type: ManualScanType = "quick"
    force: bool = False


class SourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
    )
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field(default="web", max_length=40)
    company: str | None = Field(default=None, max_length=160)
    source_type: SourceType
    url: str | None = Field(default=None, pattern=r"^https://", max_length=2_000)
    account_name: str | None = Field(default=None, max_length=160)
    account_id: str | None = Field(default=None, max_length=160)
    enabled: bool = True
    priority: int = Field(default=50, ge=0, le=100)
    trust_level: Literal["discovery", "verification"] = "discovery"
    interval_minutes: int = Field(default=120, ge=5, le=43_200)
    adapter_config: dict[str, Any] = Field(default_factory=dict)
    query_config: dict[str, Any] = Field(default_factory=dict)
    region_config: dict[str, Any] = Field(default_factory=dict)


class SourcePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    platform: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=160)
    source_type: SourceType | None = None
    url: str | None = Field(default=None, pattern=r"^https://", max_length=2_000)
    account_name: str | None = Field(default=None, max_length=160)
    account_id: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    trust_level: Literal["discovery", "verification"] | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=43_200)
    adapter_config: dict[str, Any] | None = None
    query_config: dict[str, Any] | None = None
    region_config: dict[str, Any] | None = None


def _validate_evidence(values: list[str]) -> list[str]:
    email = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
    phones = (
        re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
        re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)"),
        re.compile(r"(?<!\d)\+\d{8,15}(?!\d)"),
    )
    cleaned: list[str] = []
    for raw in values:
        value = raw.strip()
        if "\n" in value or "\r" in value:
            raise ValueError("Evidence must be a single-line statement.")
        if email.search(value) or any(pattern.search(value) for pattern in phones):
            raise ValueError("Evidence must not contain email addresses or phone numbers.")
        cleaned.append(value)
    return cleaned
