"""Explicit monitoring ratings, independent of official-page verification.

The transport model is shared with the offline importer. Stored provenance is
attached by the ingest boundary; ratings never grant a verification claim.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .chatgpt_sources import KNOWN_CHATGPT_SOURCE_IDS


class SourceRating(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["job", "company"]
    tier_code: Literal["T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3"] | None = None
    score: float | None = Field(default=None, ge=0, le=100, strict=True, allow_inf_nan=False)
    reason: str | None = Field(default=None, max_length=280)

    @field_validator("reason")
    @classmethod
    def single_line_reason(cls, value: str | None) -> str | None:
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError("Rating reason must be a short single-line statement.")
        if value and re.search(
            r"(?i)(?:[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}|"
            r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)|"
            r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)|(?<!\d)\+\d{8,15}(?!\d)|"
            r"(?:chatgpt\.com|chat\.openai\.com)/|"
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
            r"\b(?:sk-[\w-]{8,}|bearer\s+\S+)|"
            r"(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+)", value,
        ):
            raise ValueError("Rating reason must not contain private identifiers, contacts or credentials.")
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def require_explicit_rating(self):
        if self.tier_code is None and self.score is None:
            raise ValueError("source_rating requires an explicit tier_code or score.")
        return self


def normalize_source_rating(value: Any) -> dict[str, Any] | None:
    """Validate a persisted rating and retain only bounded provenance fields."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    try:
        rating = SourceRating.model_validate({
            key: value[key] for key in ("scope", "tier_code", "score", "reason") if key in value
        }).model_dump(exclude_none=True)
    except ValueError:
        return None
    source_id = str(value.get("source_id") or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", source_id):
        rating["source_id"] = source_id
    for key in ("source_updated_at", "observed_at"):
        if value.get(key):
            try:
                parsed = datetime.fromisoformat(str(value[key]).replace("Z", "+00:00"))
                rating[key] = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    rating_key = str(value.get("rating_key") or "")
    if re.fullmatch(r"candidate-[0-9a-f]{32}", rating_key):
        rating["rating_key"] = rating_key
    return rating


def merge_source_ratings(*collections: Any) -> list[dict[str, Any]]:
    """Keep conflicting observations; only a newer version of one row replaces it."""
    ratings: dict[str, dict[str, Any]] = {}
    for values in collections:
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except (TypeError, ValueError):
                continue
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            rating = normalize_source_rating(value)
            if not rating:
                continue
            key = rating.get("rating_key") or json.dumps(rating, sort_keys=True)
            previous = ratings.get(key)
            previous_time = (previous or {}).get("source_updated_at") or (previous or {}).get("observed_at") or ""
            incoming_time = rating.get("source_updated_at") or rating.get("observed_at") or ""
            if previous and (previous_time, previous.get("observed_at") or "") > (
                incoming_time, rating.get("observed_at") or "",
            ):
                continue
            ratings[key] = rating
    return sorted(ratings.values(), key=lambda rating: json.dumps(rating, sort_keys=True))


def resolve_source_ratings(job: dict[str, Any]) -> dict[str, Any]:
    ratings = merge_source_ratings(job.get("source_ratings"), job.get("source_rating"))
    job_ratings = [rating for rating in ratings if rating["scope"] == "job"]
    choices = job_ratings or ratings
    values = {(rating.get("tier_code"), rating.get("score")) for rating in choices}
    selected = choices[0] if len(values) == 1 else None
    return {
        "source_rating": selected,
        "source_ratings": ratings,
        "rating_status": (
            "conflicted" if len(values) > 1 else
            "applied" if job_ratings else "company_reference" if ratings else None
        ),
        "rating_source": (
            "chatgpt" if selected and selected.get("source_id") in KNOWN_CHATGPT_SOURCE_IDS
            else "external" if selected else None
        ),
    }
