"""Bitemporal job facts: business validity and what the service knew when.

Intervals are UTC, half-open [from, to). A missing upper bound is infinity.
All writes run inside the caller's existing short, serialized transaction.
Old assertions are closed, never overwritten; corrections split validity ranges.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

FIELDS = (
    "id", "external_id", "company", "title", "city", "region", "status",
    "opening_date", "closing_date", "verification_status", "official_url",
    "application_url", "description", "responsibilities", "requirements",
    "tags", "industry_tags", "role_tags", "primary_category", "program_id",
)
JSON_FIELDS = {"tags", "industry_tags", "role_tags"}


def instant(value: str | datetime | None = None) -> str:
    parsed = value if isinstance(value, datetime) else (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    )
    if parsed.tzinfo is None:
        raise ValueError("Use an ISO 8601 timestamp with an explicit timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    result = {field: raw.get(field) for field in FIELDS}
    for field in JSON_FIELDS:
        value = result[field]
        if isinstance(value, str):
            value = json.loads(value or "[]")
        result[field] = value or []
    return result


def encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migrate(connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS radar_job_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            system_from TEXT NOT NULL,
            system_to TEXT,
            snapshot_data TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            origin TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            CHECK(valid_to IS NULL OR valid_from < valid_to),
            CHECK(system_to IS NULL OR system_from < system_to),
            FOREIGN KEY(job_id) REFERENCES radar_jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_radar_versions_asof
            ON radar_job_versions(job_id, system_from, system_to, valid_from);
        CREATE INDEX IF NOT EXISTS idx_radar_versions_current
            ON radar_job_versions(job_id, valid_from) WHERE system_to IS NULL;
        CREATE TABLE IF NOT EXISTS radar_webhook_deliveries (
            event_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            first_received_at TEXT NOT NULL,
            last_received_at TEXT NOT NULL,
            deliveries INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_radar_webhook_received
            ON radar_webhook_deliveries(last_received_at DESC);
    """)
    # Existing rows establish a baseline now. We do not invent past knowledge.
    version = "future_radar_v5_bitemporal_jobs"
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
        return
    now = instant()
    cursor = ""
    while True:
        rows = connection.execute(
            "SELECT * FROM radar_jobs WHERE id>? ORDER BY id LIMIT 200", (cursor,),
        ).fetchall()
        if not rows:
            break
        records = []
        for row in rows:
            raw = dict(row)
            data = encode(snapshot(raw))
            records.append((raw["id"], now, None, now, data,
                            hashlib.sha256(data.encode()).hexdigest(), "migration_baseline", ""))
        connection.executemany(
            """INSERT INTO radar_job_versions
               (job_id,valid_from,valid_to,system_from,snapshot_data,snapshot_hash,origin,reason)
               VALUES (?,?,?,?,?,?,?,?)""", records,
        )
        cursor = str(rows[-1]["id"])
    connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)", (version, now))


def _insert(connection, records: list[tuple]) -> None:
    if records:
        connection.executemany(
            """INSERT INTO radar_job_versions
               (job_id,valid_from,valid_to,system_from,snapshot_data,snapshot_hash,origin,reason)
               VALUES (?,?,?,?,?,?,?,?)""", records,
        )


def observe(connection, jobs: list[dict[str, Any]], *, recorded_at: str) -> None:
    """Capture observed changes in batches; heartbeats do not create versions."""
    if not jobs:
        return
    jobs = list({str(job["id"]): job for job in jobs}.values())
    placeholders = ",".join("?" for _ in jobs)
    current: dict[str, list[dict]] = {}
    for row in connection.execute(
        f"SELECT * FROM radar_job_versions WHERE job_id IN ({placeholders}) AND system_to IS NULL",
        [job["id"] for job in jobs],
    ).fetchall():
        current.setdefault(row["job_id"], []).append(dict(row))
    inserts, closes = [], []
    for job in jobs:
        data = encode(snapshot(job))
        digest = hashlib.sha256(data.encode()).hexdigest()
        previous = current.get(str(job["id"]), [])
        latest = next((row for row in previous if row["valid_to"] is None), None)
        if latest and latest["snapshot_hash"] == digest:
            continue
        at = instant(recorded_at)
        if previous:
            newest = max(row["system_from"] for row in previous)
            if at <= newest:
                at = instant(datetime.fromisoformat(newest) + timedelta(microseconds=1))
        # An observation applies from its observation time onward. No source
        # effective date was supplied, so claiming earlier validity is unsafe.
        for row in previous:
            if row["valid_to"] is not None and row["valid_to"] <= at:
                continue
            closes.append((at, row["id"]))
            if row["valid_from"] < at:
                inserts.append((job["id"], row["valid_from"], at, at,
                                row["snapshot_data"], row["snapshot_hash"], row["origin"], row["reason"]))
        inserts.append((job["id"], at, None, at, data, digest, "observed_change", ""))
    if closes:
        connection.executemany("UPDATE radar_job_versions SET system_to=? WHERE id=? AND system_to IS NULL", closes)
    _insert(connection, inserts)


def history(connection, job_id: str, *, valid_at: str | None = None,
            known_at: str | None = None, page: int = 1, page_size: int = 30) -> dict:
    where, params = ["job_id=?"], [job_id]
    if valid_at:
        at = instant(valid_at)
        where.extend(["valid_from<=?", "(valid_to IS NULL OR valid_to>?)"])
        params.extend([at, at])
    if known_at:
        at = instant(known_at)
        where.extend(["system_from<=?", "(system_to IS NULL OR system_to>?)"])
        params.extend([at, at])
    else:
        where.append("system_to IS NULL")
    clause = " AND ".join(where)
    rows = connection.execute(
        f"SELECT * FROM radar_job_versions WHERE {clause} ORDER BY valid_from DESC,id DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["job"] = json.loads(item.pop("snapshot_data"))
        item.pop("snapshot_hash")
        # A verified current row must not make its old unverified facts public.
        if item["job"].get("verification_status") == "verified":
            items.append(item)
    coverage = connection.execute(
        "SELECT MIN(system_from) AS since, MAX(system_from) AS revision FROM radar_job_versions WHERE job_id=?", (job_id,),
    ).fetchone()
    return {"items": items, "page": page, "page_size": page_size,
            "has_more": len(rows) == page_size, "knowledge_since": coverage["since"],
            "revision": coverage["revision"], "valid_at": valid_at, "known_at": known_at,
            "interval_semantics": "UTC [from,to); null upper bound means unbounded",
            "history_note": "旧数据从迁移基线开始记录；观测变更以观测时间生效，已确认的历史事实可作区间更正。"}


def correct(connection, job_id: str, *, valid_from: str, valid_to: str,
            changes: dict, expected_revision: str, reason: str) -> dict:
    """Correct a confirmed past interval while preserving previous knowledge."""
    start, end, now = instant(valid_from), instant(valid_to), instant()
    if not start < end <= now:
        raise ValueError("Correction must specify a nonempty interval in the past.")
    latest = connection.execute(
        "SELECT MAX(system_from) FROM radar_job_versions WHERE job_id=?", (job_id,),
    ).fetchone()[0]
    if latest != expected_revision:
        raise ValueError("History changed; reload its revision before correcting it.")
    if latest and now <= latest:
        now = instant(datetime.fromisoformat(latest) + timedelta(microseconds=1))
    rows = connection.execute(
        """SELECT * FROM radar_job_versions WHERE job_id=? AND system_to IS NULL
           AND valid_from<? AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from""",
        (job_id, end, start),
    ).fetchall()
    if not rows or rows[0]["valid_from"] > start:
        raise ValueError("The requested interval predates recorded history.")
    inserts, closes = [], []
    for row in rows:
        left, right = max(start, row["valid_from"]), min(end, row["valid_to"] or end)
        closes.append((now, row["id"]))
        if row["valid_from"] < left:
            inserts.append((job_id, row["valid_from"], left, now, row["snapshot_data"], row["snapshot_hash"], row["origin"], row["reason"]))
        corrected = {**json.loads(row["snapshot_data"]), **changes}
        if (corrected.get("opening_date") and corrected.get("closing_date")
                and corrected["opening_date"] > corrected["closing_date"]):
            raise ValueError("Opening date cannot follow closing date.")
        data = encode(corrected)
        inserts.append((job_id, left, right, now, data, hashlib.sha256(data.encode()).hexdigest(), "confirmed_correction", reason))
        if row["valid_to"] is None or row["valid_to"] > right:
            inserts.append((job_id, right, row["valid_to"], now, row["snapshot_data"], row["snapshot_hash"], row["origin"], row["reason"]))
    connection.executemany("UPDATE radar_job_versions SET system_to=? WHERE id=? AND system_to IS NULL", closes)
    _insert(connection, inserts)
    return {"corrected": True, "revision": now, "affected_assertions": len(rows)}
