#!/usr/bin/env python3
"""Restore an explicitly approved *public opportunity* snapshot, not accounts.

Default operation is an offline dry run.  --apply alone reads the named DSN
environment variable and writes only the Future Radar tables, in one transaction.
Historical verification states are copied from the approved snapshot, never
created by this tool.  It performs no official-page, AI, browser, or Keychain call.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Sequence
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
VERSION = "FROSTFIRE_PUBLIC_POOL_SNAPSHOT_V1"
MAX_BYTES = 32_000_000
MAX_JOBS = 5_000
STATUSES = ("open", "unknown", "closed")
VERIFICATIONS = ("pending", "verified", "conflicted", "rejected")
SOURCE_TYPES = {
    "official_html", "official_api", "ats", "wechat_public", "openai_web_search",
    "manual", "other_public_source", "public_feed",
}
TEXT_LIMITS = {
    "company": 160, "title": 280, "city": 160, "region": 160,
    "employer_type": 80, "industry": 120, "primary_category": 80,
    "organization_category": 80, "description": 8_000,
    "responsibilities": 8_000, "requirements": 8_000,
}
JSON_FIELDS = ("industry_tags", "role_tags", "tags")
JOB_COLUMNS = (
    "id", "external_id", "program_id", "company_id", "company", "title", "city",
    "region", "employer_type", "industry", "primary_category", "organization_category",
    "industry_tags", "role_tags", "official_url", "application_url", "opening_date",
    "closing_date", "status", "verification_status", "confidence_score", "description",
    "responsibilities", "requirements", "tags", "content_hash", "source_id",
    "first_seen_at", "last_seen_at", "last_changed_at", "created_at", "updated_at",
)
EMPTY_DATA_TABLES = (
    "radar_jobs", "radar_companies", "recruitment_programs", "job_sources",
    "program_sources", "source_articles", "radar_events", "radar_runs",
    "radar_sync_batches", "radar_ai_cache", "radar_source_snapshots", "radar_locks",
)
FORBIDDEN_KEYS = {
    "source_thread_id", "conversation_id", "conversation_uuid", "cookie", "cookies",
    "authorization", "password", "password_hash", "api_key", "apikey", "access_token",
    "refresh_token", "token", "username", "user_id", "messages", "sessions",
    "chat_history", "personal_background",
}
SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie", "key",
    "password", "refresh_token", "secret", "sig", "signature", "token",
    "email", "phone", "mobile", "telephone",
}
SECRET = re.compile(
    r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{16,}\b|"
    r"\beyJ[a-z0-9_-]{16,}\.[a-z0-9_-]{16,}\.[a-z0-9_-]{10,}\b|"
    r"\b(?:authorization|cookie|api[_ -]?key|access[_ -]?token)\s*[:=]\s*\S+"
)
EMAIL = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")
UUID = re.compile(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
PRIVATE_CHAT = re.compile(r"(?i)(?:chatgpt\.com|chat\.openai\.com)/(?:c|share)/")


class RestoreError(ValueError):
    """Messages contain constant reason codes, never rejected values or DSNs."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _private_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                raise RestoreError("private_property_not_allowed")
            _private_material(item)
    elif isinstance(value, list):
        for item in value:
            _private_material(item)
    elif isinstance(value, str):
        decoded = unquote(value)
        if SECRET.search(decoded) or PRIVATE_CHAT.search(decoded):
            raise RestoreError("private_or_credential_material_not_allowed")


def _text(value: Any, *, limit: int, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise RestoreError("invalid_public_text")
    if EMAIL.search(value) or PHONE.search(value) or UUID.search(value):
        raise RestoreError("contact_or_private_identifier_in_public_prose")
    if "\x00" in value:
        raise RestoreError("invalid_public_text")
    return value


def _identifier(value: Any, *, logical_source: bool = False, public_external: bool = False) -> str:
    limit = 64 if logical_source else 180
    if public_external:
        # The public API can already redact a phone-shaped ATS identifier. Its
        # unique exported spelling is preserved, not guessed or rewritten.
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
            raise RestoreError("invalid_public_identifier")
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0," + str(limit - 1) + r"}", value):
        raise RestoreError("invalid_public_identifier")
    # App job IDs/public ATS IDs may be UUIDs. Logical source IDs may not be
    # private conversation UUIDs. Never rewrite the approved stable job IDs.
    if logical_source and UUID.fullmatch(value):
        raise RestoreError("source_id_must_be_logical")
    return value


def _timestamp(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise RestoreError("invalid_snapshot_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise RestoreError("invalid_snapshot_timestamp") from exc
    return value


def _date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise RestoreError("invalid_snapshot_date") from exc
    return value


def _url(value: Any, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or len(value) > 2_000 or value != value.strip():
        raise RestoreError("invalid_public_url")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        if (
            parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or hostname in {"localhost", "chatgpt.com", "chat.openai.com"}
            or hostname.endswith((".localhost", ".local", ".internal"))
            or any(char in value for char in ("\x00", "\r", "\n", "\\"))
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if "." not in hostname:
                raise RestoreError("invalid_public_url")
        else:
            if not address.is_global:
                raise RestoreError("invalid_public_url")
        if EMAIL.search(unquote(value)):
            raise RestoreError("contact_in_public_url")
        if any(key.casefold() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query)):
            raise RestoreError("sensitive_public_url_query")
    except ValueError as exc:
        if isinstance(exc, RestoreError):
            raise
        raise RestoreError("invalid_public_url") from exc
    # Validation only. Do not strip public job identifiers, fragment routes,
    # tracking parameters, or modify the original clickable application URL.
    return value


def _source(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RestoreError("invalid_public_source")
    source_id = _identifier(raw.get("source_id"), logical_source=True)
    source_type = raw.get("source_type") or "manual"
    trust = raw.get("trust_level") or "discovery"
    role = raw.get("verification_role") or "discovery"
    if source_type not in SOURCE_TYPES or trust not in {"discovery", "verification"} or role not in {"discovery", "verification"}:
        raise RestoreError("invalid_public_source_classification")
    if not isinstance(raw.get("active"), bool):
        raise RestoreError("invalid_public_source_active_flag")
    return {
        "source_id": source_id, "name": _text(raw.get("name"), limit=160) or source_id,
        "source_type": source_type, "trust_level": trust,
        "source_url": _url(raw.get("source_url")), "verification_role": role,
        "discovered_at": _timestamp(raw.get("discovered_at")),
        "last_seen_at": _timestamp(raw.get("last_seen_at")), "active": raw["active"],
    }


def _job(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RestoreError("invalid_public_job")
    item = {field: _text(raw.get(field), limit=limit, required=field in {"company", "title"}) for field, limit in TEXT_LIMITS.items()}
    item["id"] = _identifier(raw.get("id"))
    item["external_id"] = _identifier(raw.get("external_id"), public_external=True)
    item["program_id"] = _identifier(raw["program_id"]) if raw.get("program_id") else None
    item["program_name"] = _text(raw.get("program_name"), limit=240) or None
    if item["program_id"] and not item["program_name"]:
        raise RestoreError("program_metadata_missing_from_snapshot")
    year = raw.get("recruitment_year")
    if year is not None and (type(year) is not int or not 2020 <= year <= 2100):
        raise RestoreError("invalid_recruitment_year")
    item["recruitment_year"] = year
    for field in JSON_FIELDS:
        values = raw.get(field) or []
        if not isinstance(values, list) or len(values) > 30:
            raise RestoreError("invalid_public_tag_list")
        item[field] = [_text(value, limit=100, required=True) for value in values]
    for field in ("official_url", "application_url"):
        item[field] = _url(raw.get(field))
    if not item["official_url"] and not item["application_url"]:
        raise RestoreError("public_job_has_no_clickable_url")
    for field in ("opening_date", "closing_date"):
        item[field] = _date(raw.get(field))
    item["status"] = raw.get("status")
    item["verification_status"] = raw.get("verification_status")
    if item["status"] not in STATUSES or item["verification_status"] not in VERIFICATIONS:
        raise RestoreError("invalid_historical_status")
    confidence = raw.get("confidence_score")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise RestoreError("invalid_historical_confidence")
    item["confidence_score"] = confidence
    for field in ("first_seen_at", "last_seen_at", "last_changed_at"):
        item[field] = _timestamp(raw.get(field))
    event = raw.get("latest_event_type")
    if event is not None and event not in {"NEW", "UPDATED", "CLOSED", "REOPENED", "VERIFIED"}:
        raise RestoreError("unsupported_historical_job_event")
    item["latest_event_type"] = event
    item["latest_event_at"] = _timestamp(raw.get("latest_event_at"), optional=True)
    if bool(event) != bool(item["latest_event_at"]):
        raise RestoreError("incomplete_historical_job_event")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > 100:
        raise RestoreError("public_provenance_missing_or_unbounded")
    item["sources"] = [_source(source) for source in sources]
    # All non-whitelisted scoring/profile fields are intentionally discarded.
    return item


def _counts(jobs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(jobs),
        "verification_status": {key: sum(job["verification_status"] == key for job in jobs) for key in VERIFICATIONS},
        "status": {key: sum(job["status"] == key for job in jobs) for key in STATUSES},
    }


@dataclass(frozen=True)
class Snapshot:
    sha256: str
    data: dict[str, Any]

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return self.data["jobs"]

    @property
    def anchor(self) -> str:
        return "restore-public-pool-" + self.sha256[:24]

    def summary(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        return {
            "snapshot_sha256": self.sha256, "public_snapshot_sha256": digest(self.data),
            "counts": self.data["counts"], "snapshot_source_id": self.anchor,
            "expired_on_current_utc_date": sum(bool(job["closing_date"] and job["closing_date"] <= today) for job in self.jobs),
            "historical_verification_preserved_not_rechecked": True,
            "account_tables_read_or_written": False,
        }


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError("duplicate_json_property")
        result[key] = value
    return result


def load_snapshot(path: Path, expected_sha256: str) -> Snapshot:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RestoreError("invalid_expected_sha256")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise RestoreError("snapshot_not_readable") from exc
    if len(raw) > MAX_BYTES:
        raise RestoreError("snapshot_too_large")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise RestoreError("snapshot_sha256_mismatch")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreError("snapshot_invalid_json") from exc
    if not isinstance(data, dict) or data.get("schema_version") != VERSION:
        raise RestoreError("unsupported_public_snapshot_version")
    if set(data) != {"schema_version", "captured_at", "source_origin", "counts", "jobs"}:
        raise RestoreError("unsupported_public_snapshot_top_level_fields")
    _private_material(data)
    captured = _timestamp(data.get("captured_at"))
    origin = _url(data.get("source_origin"), required=True)
    parsed_origin = urlsplit(origin or "")
    if parsed_origin.path not in ("", "/") or parsed_origin.query or parsed_origin.fragment:
        raise RestoreError("source_origin_must_be_public_site_root")
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list) or not 1 <= len(raw_jobs) <= MAX_JOBS:
        raise RestoreError("invalid_snapshot_job_count")
    jobs = [_job(job) for job in raw_jobs]
    if len({job["id"] for job in jobs}) != len(jobs) or len({job["external_id"] for job in jobs}) != len(jobs):
        raise RestoreError("duplicate_snapshot_job_identity")
    counts = _counts(jobs)
    declared = data.get("counts")
    if not isinstance(declared, dict) or set(declared) != set(counts) or type(declared.get("total")) is not int or declared["total"] != counts["total"]:
        raise RestoreError("snapshot_counts_mismatch")
    for kind in ("verification_status", "status"):
        values = declared.get(kind)
        if not isinstance(values, dict) or set(values) - set(counts[kind]):
            raise RestoreError("snapshot_counts_mismatch")
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise RestoreError("snapshot_counts_mismatch")
        if {key: values.get(key, 0) for key in counts[kind]} != counts[kind]:
            raise RestoreError("snapshot_counts_mismatch")
    return Snapshot(actual, {"schema_version": VERSION, "captured_at": captured, "source_origin": origin, "counts": counts, "jobs": jobs})


class _AtomicScripts:
    """sqlite3.executescript implicitly commits; execute static DDL atomically."""
    def __init__(self, connection: Any):
        self.connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def executescript(self, script: str) -> None:
        buffer = ""
        for char in script:
            buffer += char
            if char == ";" and sqlite3.complete_statement(buffer):
                self.connection.execute(buffer)
                buffer = ""
        if buffer.strip():
            self.connection.execute(buffer)


def _insert(connection: Any, table: str, row: dict[str, Any]) -> None:
    # table/column names come exclusively from the fixed source code below.
    columns = tuple(row)
    connection.execute(
        "INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES (" + ", ".join("?" for _ in columns) + ")",
        tuple(canonical(row[key]) if isinstance(row[key], (dict, list)) else row[key] for key in columns),
    )


def _source_rows(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for job in snapshot.jobs:
        for source in job["sources"]:
            candidate = {key: source[key] for key in ("source_id", "name", "source_type", "trust_level")}
            previous = sources.setdefault(source["source_id"], candidate)
            if previous != candidate:
                raise RestoreError("inconsistent_public_source_metadata")
    sources[snapshot.anchor] = {"source_id": snapshot.anchor, "name": "公开机会历史恢复快照", "source_type": "manual", "trust_level": "discovery"}
    return sources


def _job_row(job: dict[str, Any], snapshot: Snapshot) -> dict[str, Any]:
    row = {key: job.get(key) for key in JOB_COLUMNS}
    row["company_id"] = _company_id(job["company"])
    row["source_id"] = snapshot.anchor
    row["created_at"] = job["first_seen_at"]
    row["updated_at"] = job["last_changed_at"]
    row["content_hash"] = digest({key: job.get(key) for key in JOB_COLUMNS if key not in {"content_hash", "company_id", "source_id", "created_at", "updated_at"}})
    return row


def _company_key(company: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", company).casefold())


def _company_id(company: str) -> str:
    return "restore-company-" + digest(_company_key(company))[:32]


def _company_rows(snapshot: Snapshot) -> list[dict[str, Any]]:
    companies = {}
    now = snapshot.data["captured_at"]
    for job in snapshot.jobs:
        company_id = _company_id(job["company"])
        companies.setdefault(company_id, {
            "id": company_id, "external_id": company_id, "name": job["company"],
            "normalized_name": _company_key(job["company"]),
            "employer_type": job["employer_type"], "industry": job["industry"],
            "created_at": now, "updated_at": now,
        })
    return sorted(companies.values(), key=lambda row: row["id"])


def _program_rows(snapshot: Snapshot) -> list[dict[str, Any]]:
    programs = {}
    now = snapshot.data["captured_at"]
    for job in snapshot.jobs:
        program_id = job["program_id"]
        if not program_id:
            continue
        program = {
            "id": program_id, "external_id": "restore-program-" + digest(program_id)[:32],
            "company_id": _company_id(job["company"]), "company": job["company"],
            "program_name": job["program_name"], "recruitment_year": job["recruitment_year"],
            "recruitment_type": "other", "status": "unknown", "verification_status": "pending",
            "content_hash": digest({key: job[key] for key in ("program_id", "company", "program_name", "recruitment_year")}),
            "source_id": snapshot.anchor, "first_seen_at": now, "last_seen_at": now,
            "last_changed_at": now, "created_at": now, "updated_at": now,
        }
        if programs.setdefault(program_id, program) != program:
            raise RestoreError("inconsistent_program_metadata")
    return sorted(programs.values(), key=lambda row: row["id"])


def _expected_links(job: dict[str, Any], snapshot: Snapshot) -> list[dict[str, Any]]:
    # A merged API opportunity may contain several URLs for the same original
    # source. The complete list is preserved verbatim in snapshot metadata;
    # the existing (job_id, source_id) relational key receives one representative.
    groups: dict[str, list[dict[str, Any]]] = {}
    for source in job["sources"]:
        groups.setdefault(source["source_id"], []).append(source)
    links = []
    for source_id, group in sorted(groups.items()):
        representative = max(group, key=lambda source: (
            source["source_url"] == job["application_url"],
            source["verification_role"] == "verification", source["last_seen_at"],
        ))
        links.append({
            "job_id": job["id"], "source_id": source_id, "source_url": representative["source_url"],
            "discovered_at": min(source["discovered_at"] for source in group),
            "last_seen_at": max(source["last_seen_at"] for source in group),
            "source_type": representative["source_type"],
            "verification_role": "verification" if any(source["verification_role"] == "verification" for source in group) else "discovery",
            "evidence": [], "active": int(any(source["active"] for source in group)), "missing_successes": 0,
        })
    links.append({
        "job_id": job["id"], "source_id": snapshot.anchor, "source_url": None,
        "discovered_at": snapshot.data["captured_at"], "last_seen_at": snapshot.data["captured_at"],
        "source_type": "manual", "verification_role": "discovery", "evidence": [],
        "active": 1, "missing_successes": 0,
    })
    return links


def _verify(connection: Any, snapshot: Snapshot) -> None:
    for table, expected_registry in (("radar_companies", _company_rows(snapshot)), ("recruitment_programs", _program_rows(snapshot))):
        columns = tuple(expected_registry[0]) if expected_registry else ("id",)
        registry = [dict(row) for row in connection.execute("SELECT " + ", ".join(columns) + " FROM " + table + " ORDER BY id").fetchall()]
        if registry != expected_registry:
            raise RestoreError("restored_registry_does_not_match_snapshot")
    rows = connection.execute("SELECT " + ", ".join(JOB_COLUMNS) + " FROM radar_jobs ORDER BY id").fetchall()
    actual = []
    for row in rows:
        value = dict(row)
        for field in JSON_FIELDS:
            value[field] = json.loads(value[field])
        actual.append(value)
    expected = sorted((_job_row(job, snapshot) for job in snapshot.jobs), key=lambda row: row["id"])
    if actual != expected:
        raise RestoreError("restored_job_values_do_not_match_snapshot")
    raw_snapshot = connection.execute(
        "SELECT metadata FROM radar_source_snapshots WHERE source_id=? AND content_hash=?", (snapshot.anchor, snapshot.sha256),
    ).fetchall()
    if len(raw_snapshot) != 1 or json.loads(raw_snapshot[0]["metadata"]).get("snapshot") != snapshot.data:
        raise RestoreError("restored_public_provenance_does_not_match_snapshot")
    source = connection.execute("SELECT enabled, source_type, trust_level, adapter_config FROM monitor_sources WHERE id=?", (snapshot.anchor,)).fetchone()
    if not source or source["enabled"] or source["source_type"] != "manual" or source["trust_level"] != "discovery" or json.loads(source["adapter_config"]) != {"adapter": "manual"}:
        raise RestoreError("snapshot_anchor_not_push_only")
    expected_links = sorted((link for job in snapshot.jobs for link in _expected_links(job, snapshot)), key=lambda row: (row["job_id"], row["source_id"]))
    actual_links = []
    columns = tuple(expected_links[0])
    for row in connection.execute("SELECT " + ", ".join(columns) + " FROM job_sources ORDER BY job_id, source_id").fetchall():
        value = dict(row)
        value["evidence"] = json.loads(value["evidence"])
        actual_links.append(value)
    if expected_links != actual_links:
        raise RestoreError("restored_source_links_do_not_match_snapshot")


def apply_snapshot(snapshot: Snapshot, connection_factory: Callable[[], Any]) -> dict[str, Any]:
    """The factory must target an isolated/approved empty recruitment schema."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from backend.future_radar.schema import migrate

    sources = _source_rows(snapshot)
    connection = connection_factory()
    key = "public-pool-restore-" + snapshot.sha256
    try:
        connection.execute("BEGIN IMMEDIATE")
        if hasattr(connection, "ensure_schema"):
            connection.ensure_schema()
        migrate(_AtomicScripts(connection))
        prior = connection.execute("SELECT payload_hash FROM radar_sync_batches WHERE idempotency_key=?", (key,)).fetchone()
        if prior:
            if prior["payload_hash"] != snapshot.sha256:
                raise RestoreError("restore_ledger_hash_mismatch")
            _verify(connection, snapshot)
            connection.commit()
            return {**snapshot.summary(), "status": "already_restored_verified", "idempotent_replay": True}
        if any(connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in EMPTY_DATA_TABLES):
            raise RestoreError("target_recruitment_data_not_empty")
        now = snapshot.data["captured_at"]
        for source_id, source in sources.items():
            existing = connection.execute("SELECT id FROM monitor_sources WHERE id=?", (source_id,)).fetchone()
            if existing:
                if source_id == snapshot.anchor:
                    raise RestoreError("snapshot_anchor_already_exists_without_ledger")
                continue
            _insert(connection, "monitor_sources", {
                "id": source_id, "name": source["name"], "platform": "snapshot",
                "source_type": source["source_type"], "enabled": 0, "priority": 0,
                "trust_level": source["trust_level"], "interval_minutes": 1_440,
                "adapter_config": {"adapter": "manual"}, "query_config": {}, "region_config": {},
                "status": "disabled", "verification_status": "unverified", "created_at": now, "updated_at": now,
            })
        for company in _company_rows(snapshot):
            _insert(connection, "radar_companies", company)
        for program in _program_rows(snapshot):
            _insert(connection, "recruitment_programs", program)
        for job in snapshot.jobs:
            _insert(connection, "radar_jobs", _job_row(job, snapshot))
            for link in _expected_links(job, snapshot):
                _insert(connection, "job_sources", link)
            if job["latest_event_type"]:
                _insert(connection, "radar_events", {
                    "event_key": "restore-event-" + digest([snapshot.sha256, job["id"]]),
                    "entity_type": "job", "entity_id": job["id"], "external_id": job["external_id"],
                    "event_type": job["latest_event_type"], "after_data": job,
                    "changed_fields": [], "detected_at": job["latest_event_at"], "source_id": snapshot.anchor,
                })
        _insert(connection, "radar_source_snapshots", {
            "source_id": snapshot.anchor, "fetched_at": now, "content_hash": snapshot.sha256,
            "normalized_content": "", "metadata": {
                "kind": "public_pool_restore", "historical_verification_preserved_not_rechecked": True,
                "snapshot": snapshot.data,
            },
        })
        _verify(connection, snapshot)
        result = {**snapshot.summary(), "status": "restored_and_verified", "idempotent_replay": False}
        _insert(connection, "radar_sync_batches", {"idempotency_key": key, "payload_hash": snapshot.sha256, "source_id": snapshot.anchor, "result": result, "created_at": now})
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--dry-run", action="store_true", help="offline validation (default)")
    actions.add_argument("--apply", action="store_true", help="restore an approved snapshot to an empty PostgreSQL recruitment schema")
    parser.add_argument("--target-env", default="FROSTFIRE_PUBLIC_POOL_DATABASE_URL", help="DSN environment variable name, not the DSN itself")
    parser.add_argument("--schema", default="frostfire")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot, args.expected_sha256)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", args.target_env) or args.target_env in {"HOME", "CODEX_HOME"}:
            raise RestoreError("invalid_target_environment_name")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", args.schema) or args.schema in {"public", "auth", "storage", "realtime", "extensions", "information_schema", "graphql", "graphql_public"} or args.schema.startswith("pg_"):
            raise RestoreError("target_schema_must_be_private")
        if not args.apply:
            result = {**snapshot.summary(), "status": "dry_run_validated_no_remote_connection"}
        else:
            dsn = os.environ.get(args.target_env, "")
            if not dsn:
                raise RestoreError("target_dsn_environment_missing")
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from backend.storage import close_postgres_pools, connect_postgres
            try:
                result = apply_snapshot(snapshot, lambda: connect_postgres(dsn, schema=args.schema, timeout=60, max_size=2))
            finally:
                close_postgres_pools()
        print(canonical(result))
        return 0
    except RestoreError as exc:
        print(canonical({"status": "error", "code": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Psycopg/OS messages may contain DSNs, local paths, or submitted data.
        print(canonical({"status": "error", "code": "public_pool_restore_failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
