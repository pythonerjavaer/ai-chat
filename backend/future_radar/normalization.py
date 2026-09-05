"""Deterministic normalization and identity helpers for Future Radar."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import unicodedata
from datetime import date, datetime
from typing import Any, Iterable

from ..recruitment_rating import merge_source_ratings
from ..recruitment_watch import WatchFetchError, validate_public_https_url
from ..recruitment_directory import employer_category_override


TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "msclkid", "ref", "referrer", "source", "spm",
}
TRACKING_QUERY_PREFIXES = ("utm_", "mc_", "pk_")
PRIMARY_CATEGORY_CODES: tuple[str, ...] = (
    "state_energy_resources",
    "state_tech_telecom",
    "tobacco_monopoly",
    "policy_state_banks",
    "securities_public_funds_asset_management",
    "insurance_integrated_finance",
    "internet_tech",
    "consumer_foreign_consulting",
    "quant_private_hedge",
    "big_four_professional_services",
)
SEMANTIC_JOB_FIELDS = (
    "external_id", "company", "title", "city", "region",
    "employer_type", "industry", "primary_category", "organization_category",
    "industry_tags", "role_tags", "official_url", "application_url",
    "opening_date", "closing_date", "status", "verification_status",
    "description", "responsibilities", "requirements", "tags", "source_ratings",
)
SEMANTIC_PROGRAM_FIELDS = (
    "external_id", "company", "program_name", "recruitment_year",
    "recruitment_type", "region", "opening_date", "closing_date", "status",
    "verification_status", "official_url",
)


def clean_text(value: Any, *, limit: int = 4_000) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def normalized_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", clean_text(value).casefold())


_OPERATOR_ROOTS = {
    "china_mobile": ("中国移动", "中国移动集团", "中国移动通信集团", "中国移动通信集团有限公司", "中国移动有限公司"),
    "china_telecom": ("中国电信", "中国电信集团", "中国电信集团有限公司", "中国电信股份有限公司"),
    "china_unicom": ("中国联通", "中国联合网络通信", "中国联合网络通信集团", "中国联合网络通信集团有限公司", "中国联合网络通信有限公司", "中国联合网络通信股份有限公司"),
}
_OPERATOR_BRANDS = {
    "china_mobile": ("咪咕文化科技有限公司", "咪咕公司", "中移物联网有限公司", "中移互联网有限公司", "中移金融科技有限公司", "中移信息技术有限公司", "中移苏州软件技术有限公司", "中移杭州信息技术有限公司", "中移九天人工智能科技（北京）有限公司", "中移九天人工智能科技有限公司", "中国移动九天人工智能科技公司", "九天人工智能研究院", "九天研究院", "中国移动通信研究院", "中国移动研究院", "中国移动通信研究院有限公司", "中国移动数智事业部", "中移数智事业部"),
    "china_telecom": ("天翼云科技有限公司", "天翼云", "中国电信云计算研究院", "中国电信研究院", "中国电信云网运营部", "中电信人工智能科技北京有限公司", "中电信数智科技有限公司", "中电信数智科技有限公司集成公司", "中电信量子信息科技集团有限公司", "天翼安全科技有限公司", "天翼物联科技有限公司", "天翼数字生活科技有限公司", "天翼视联科技股份有限公司", "中电信数政科技有限公司"),
    "china_unicom": ("联通数字科技有限公司", "联通数科", "联通智网科技股份有限公司", "联通软件研究院", "中国联通软件研究院", "中国联通研究院", "联通在线信息科技有限公司", "联通数字科技有限公司数科本部"),
}
_OPERATOR_REGIONS = (
    "北京", "天津", "上海", "重庆", "河北", "河南", "云南", "辽宁", "黑龙江", "湖南",
    "安徽", "山东", "新疆", "江苏", "浙江", "江西", "湖北", "广西", "甘肃", "山西",
    "内蒙古", "陕西", "吉林", "福建", "贵州", "广东", "青海", "西藏", "四川", "宁夏",
    "海南", "香港", "澳门", "国际",
)


def canonical_telecom_operator(company: Any) -> str | None:
    """Bounded employer-directory correction, never a keyword search in job prose.

    Exact group/brand names and their legal regional branches are recognized.
    Unrelated companies mentioning a carrier, resellers and recruitment portals
    are not reclassified.  All other employers keep their existing taxonomy.
    """
    key = normalized_key(company)
    if any(marker in key for marker in ("招聘", "合作伙伴", "代理商", "加盟", "外包", "服务商")):
        return None
    for operator, roots in _OPERATOR_ROOTS.items():
        brands = tuple(normalized_key(name) for name in _OPERATOR_BRANDS[operator])
        if key in roots or key in brands:
            return operator
        for root in (*roots, *brands):
            if not key.startswith(root):
                continue
            branch = key[len(root):]
            if not branch.startswith(_OPERATOR_REGIONS):
                continue
            if branch in _OPERATOR_REGIONS:
                return operator
            if re.fullmatch(r"[\u4e00-\u9fff]{2,24}(?:公司|有限公司|分公司|研究院|分院)", branch):
                return operator
    return None


def telecom_primary_category(company: Any) -> str:
    return "state_tech_telecom" if canonical_telecom_operator(company) else ""


def canonicalize_url(value: Any, *, allow_empty: bool = True) -> str | None:
    raw = clean_text(value, limit=2_000)
    if not raw and allow_empty:
        return None
    try:
        safe = validate_public_https_url(raw, resolve_dns=False)
    except WatchFetchError as exc:
        raise ValueError(str(exc)) from exc
    parsed = urllib.parse.urlsplit(safe)
    query = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        folded = key.casefold()
        if folded in TRACKING_QUERY_KEYS or folded.startswith(TRACKING_QUERY_PREFIXES):
            continue
        query.append((key, item))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc.casefold(), path, urllib.parse.urlencode(query), "")
    )


def normalize_date(value: Any) -> str | None:
    if value in (None, "", "unknown", "未知"):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = clean_text(value, limit=40)
    match = re.fullmatch(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def normalize_tags(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = clean_text(raw, limit=80)
        key = value.casefold()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return result[:30]


def normalize_taxonomy_value(value: Any, *, limit: int = 80) -> str:
    """Normalize a machine taxonomy token without guessing its meaning."""
    normalized = clean_text(value, limit=limit).casefold()
    return re.sub(r"[\s\-]+", "_", normalized).strip("_")


def normalize_taxonomy_tags(values: Any) -> list[str]:
    """Return stable, case-insensitive and de-duplicated machine tags."""
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = normalize_taxonomy_value(raw)
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    # Taxonomy tags are an unordered set semantically.  Persisting a canonical
    # order keeps diffing stable when upstream extractors emit the same tags in
    # a different order.
    return sorted(result)[:30]


def infer_primary_category_from_metadata(item: dict[str, Any]) -> str:
    """Use the public employer directory or structured organization metadata."""
    # Keep the dependency local: recruitment owns the alias vocabulary, while
    # Radar normalization owns persistence. Company is an identity lookup only;
    # title/JD prose is intentionally absent from this projection.
    from ..recruitment import primary_employer_category

    category = primary_employer_category({
        "company": item.get("company"),
        "employer_type": item.get("employer_type"),
        "industry": item.get("industry"),
        "organization_category": item.get("organization_category"),
        "industry_tags": item.get("industry_tags"),
        "tags": item.get("tags"),
    })
    return category if category in PRIMARY_CATEGORY_CODES else ""


def normalize_evidence(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    email = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
    phone = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
    result: list[str] = []
    for raw in values:
        value = clean_text(raw, limit=280)
        if value and not email.search(value) and not phone.search(value):
            result.append(value)
    return list(dict.fromkeys(result))[:12]


def stable_digest(*parts: Any, prefix: str = "radar", length: int = 24) -> str:
    identity = "\x1f".join(normalized_key(part) for part in parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def stable_program_external_id(item: dict[str, Any]) -> str:
    supplied = clean_text(item.get("external_id"), limit=180)
    if supplied:
        return supplied
    return stable_digest(
        item.get("company"),
        item.get("recruitment_year"),
        item.get("recruitment_type"),
        item.get("program_name"),
        prefix="program",
    )


def stable_job_external_id(item: dict[str, Any]) -> str:
    supplied = clean_text(item.get("external_id"), limit=180)
    if supplied:
        return supplied
    # A job identity deliberately excludes source and dates so it remains
    # stable across mirrors and deadline updates.
    return stable_digest(
        item.get("company"),
        item.get("program_external_id") or item.get("program_id"),
        item.get("title"),
        item.get("city"),
        prefix="job",
    )


def semantic_hash(item: dict[str, Any], fields: Iterable[str]) -> str:
    payload: dict[str, Any] = {}
    for field in fields:
        value = item.get(field)
        if field == "tags":
            value = sorted(normalize_tags(value), key=str.casefold)
        elif field in {"industry_tags", "role_tags"}:
            value = sorted(normalize_taxonomy_tags(value))
        elif isinstance(value, str):
            value = clean_text(value)
        payload[field] = value
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_program(item: dict[str, Any]) -> dict[str, Any]:
    company = clean_text(item.get("company"), limit=160)
    name = clean_text(item.get("program_name") or item.get("name"), limit=240)
    if not company or not name:
        raise ValueError("Program company and program_name are required.")
    year = item.get("recruitment_year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not 2020 <= year <= 2100:
        year = None
    normalized = {
        "company": company,
        "program_name": name,
        "recruitment_year": year,
        "recruitment_type": clean_text(item.get("recruitment_type") or "other", limit=40).lower(),
        "region": clean_text(item.get("region"), limit=160),
        "opening_date": normalize_date(item.get("opening_date")),
        "closing_date": normalize_date(item.get("closing_date")),
        "status": clean_text(item.get("status") or "open", limit=24).lower(),
        "verification_status": clean_text(
            item.get("verification_status") or "pending", limit=24
        ).lower(),
        "confidence_score": max(0.0, min(1.0, float(item.get("confidence_score") or 0))),
        "official_url": canonicalize_url(item.get("official_url")),
        "evidence": normalize_evidence(item.get("evidence")),
    }
    if normalized["status"] not in {"open", "closed", "unknown"}:
        normalized["status"] = "unknown"
    if normalized["verification_status"] not in {"pending", "verified", "conflicted", "rejected"}:
        normalized["verification_status"] = "pending"
    normalized["external_id"] = stable_program_external_id({**item, **normalized})
    normalized["content_hash"] = semantic_hash(normalized, SEMANTIC_PROGRAM_FIELDS)
    return normalized


def normalize_job(item: dict[str, Any]) -> dict[str, Any]:
    company = clean_text(item.get("company"), limit=160)
    title = clean_text(item.get("title"), limit=280)
    if not company or not title:
        raise ValueError("Job company and title are required.")
    official_url = canonicalize_url(item.get("official_url") or item.get("url"))
    application_url = canonicalize_url(item.get("application_url")) or official_url
    primary_category = normalize_taxonomy_value(item.get("primary_category"))
    if primary_category and primary_category not in PRIMARY_CATEGORY_CODES:
        raise ValueError(f"Unsupported primary_category: {primary_category}")
    directory_category = telecom_primary_category(company) or employer_category_override(item)
    if directory_category:
        primary_category = directory_category
    elif not primary_category:
        primary_category = infer_primary_category_from_metadata(item)
    normalized = {
        "program_id": clean_text(item.get("program_id"), limit=180) or None,
        "program_external_id": clean_text(item.get("program_external_id"), limit=180) or None,
        "company": company,
        "title": title,
        "city": clean_text(item.get("city"), limit=160),
        "region": clean_text(item.get("region"), limit=160),
        "employer_type": clean_text(item.get("employer_type"), limit=80),
        "industry": clean_text(item.get("industry"), limit=120),
        "primary_category": primary_category,
        "organization_category": normalize_taxonomy_value(
            item.get("organization_category")
        ),
        "industry_tags": normalize_taxonomy_tags(item.get("industry_tags")),
        "role_tags": normalize_taxonomy_tags(item.get("role_tags")),
        "official_url": official_url,
        "application_url": application_url,
        "opening_date": normalize_date(item.get("opening_date")),
        "closing_date": normalize_date(item.get("closing_date")),
        "status": clean_text(item.get("status") or "open", limit=24).lower(),
        "verification_status": clean_text(
            item.get("verification_status") or "pending", limit=24
        ).lower(),
        "confidence_score": max(0.0, min(1.0, float(item.get("confidence_score") or 0))),
        "description": clean_text(item.get("description"), limit=8_000),
        "responsibilities": clean_text(item.get("responsibilities"), limit=8_000),
        "requirements": clean_text(item.get("requirements"), limit=8_000),
        "tags": normalize_tags(item.get("tags")),
        "source_ratings": merge_source_ratings(item.get("source_ratings"), item.get("source_rating")),
        "evidence": normalize_evidence(item.get("evidence")),
    }
    if normalized["status"] not in {"open", "closed", "unknown"}:
        normalized["status"] = "unknown"
    if normalized["verification_status"] not in {"pending", "verified", "conflicted", "rejected", "source_screened"}:
        normalized["verification_status"] = "pending"
    normalized["external_id"] = stable_job_external_id({**item, **normalized})
    normalized["content_hash"] = semantic_hash(normalized, SEMANTIC_JOB_FIELDS)
    return normalized


def changed_fields(before: dict[str, Any], after: dict[str, Any], fields: Iterable[str]) -> list[str]:
    return [field for field in fields if before.get(field) != after.get(field)]
