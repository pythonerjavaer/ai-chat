"""Shared application schema and additive migrations for Future Radar.

SQLite uses these statements directly; the PostgreSQL storage bridge translates
their dialect. Migrations stay idempotent so an existing ``recruitment_jobs`` deployment can
continue serving the legacy API while the richer radar tables are populated.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from .normalization import (
    SEMANTIC_JOB_FIELDS, infer_primary_category_from_metadata, semantic_hash,
    telecom_primary_category,
)
from ..recruitment_directory import employer_category_override


logger = logging.getLogger(__name__)
OPERATOR_CATEGORY_MIGRATION = "future_radar_v3_operator_categories"
EMPLOYER_CATEGORY_MIGRATION = "future_radar_v4_employer_directory_categories"


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS radar_companies (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL UNIQUE,
            employer_type TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            official_domain TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS monitor_sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'web',
            company TEXT,
            source_type TEXT NOT NULL,
            url TEXT,
            domain TEXT,
            account_name TEXT,
            account_id TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 50,
            trust_level TEXT NOT NULL DEFAULT 'discovery',
            interval_minutes INTEGER NOT NULL DEFAULT 120,
            adapter_config TEXT NOT NULL DEFAULT '{}',
            query_config TEXT NOT NULL DEFAULT '{}',
            region_config TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            verification_status TEXT NOT NULL DEFAULT 'unverified',
            last_checked_at TEXT,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error TEXT,
            last_content_hash TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recruitment_programs (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL UNIQUE,
            company_id TEXT,
            company TEXT NOT NULL,
            program_name TEXT NOT NULL,
            recruitment_year INTEGER,
            recruitment_type TEXT NOT NULL DEFAULT 'other',
            region TEXT NOT NULL DEFAULT '',
            opening_date TEXT,
            closing_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            verification_status TEXT NOT NULL DEFAULT 'pending',
            confidence_score REAL NOT NULL DEFAULT 0,
            official_url TEXT,
            content_hash TEXT NOT NULL,
            source_id TEXT,
            missing_successes INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES radar_companies(id) ON DELETE SET NULL,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS radar_jobs (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL UNIQUE,
            program_id TEXT,
            company_id TEXT,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            city TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            employer_type TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            primary_category TEXT NOT NULL DEFAULT '',
            organization_category TEXT NOT NULL DEFAULT '',
            industry_tags TEXT NOT NULL DEFAULT '[]',
            role_tags TEXT NOT NULL DEFAULT '[]',
            official_url TEXT,
            application_url TEXT,
            opening_date TEXT,
            closing_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            verification_status TEXT NOT NULL DEFAULT 'pending',
            confidence_score REAL NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            responsibilities TEXT NOT NULL DEFAULT '',
            requirements TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL,
            source_id TEXT,
            missing_successes INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (program_id) REFERENCES recruitment_programs(id) ON DELETE SET NULL,
            FOREIGN KEY (company_id) REFERENCES radar_companies(id) ON DELETE SET NULL,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS source_articles (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            article_external_id TEXT NOT NULL,
            publisher TEXT NOT NULL DEFAULT '',
            article_title TEXT NOT NULL,
            article_url TEXT,
            publish_time TEXT,
            content_hash TEXT NOT NULL,
            raw_excerpt TEXT NOT NULL DEFAULT '',
            is_recruitment INTEGER NOT NULL DEFAULT 0,
            recruitment_year INTEGER,
            classification TEXT NOT NULL DEFAULT 'unknown',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, article_external_id),
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS job_sources (
            job_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_url TEXT,
            discovered_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            source_type TEXT NOT NULL,
            verification_role TEXT NOT NULL DEFAULT 'discovery',
            evidence TEXT NOT NULL DEFAULT '[]',
            active INTEGER NOT NULL DEFAULT 1,
            missing_successes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (job_id, source_id),
            FOREIGN KEY (job_id) REFERENCES radar_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS program_sources (
            program_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_url TEXT,
            discovered_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            source_type TEXT NOT NULL,
            verification_role TEXT NOT NULL DEFAULT 'discovery',
            evidence TEXT NOT NULL DEFAULT '[]',
            active INTEGER NOT NULL DEFAULT 1,
            missing_successes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (program_id, source_id),
            FOREIGN KEY (program_id) REFERENCES recruitment_programs(id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS radar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            run_id TEXT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            before_data TEXT,
            after_data TEXT,
            changed_fields TEXT NOT NULL DEFAULT '[]',
            detected_at TEXT NOT NULL,
            source_id TEXT,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS radar_runs (
            id TEXT PRIMARY KEY,
            trigger_type TEXT NOT NULL DEFAULT 'scheduled',
            scan_type TEXT NOT NULL DEFAULT 'scheduled',
            force_scan INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            sources_checked INTEGER NOT NULL DEFAULT 0,
            sources_succeeded INTEGER NOT NULL DEFAULT 0,
            sources_failed INTEGER NOT NULL DEFAULT 0,
            sources_skipped INTEGER NOT NULL DEFAULT 0,
            programs_discovered INTEGER NOT NULL DEFAULT 0,
            new_jobs INTEGER NOT NULL DEFAULT 0,
            updated_jobs INTEGER NOT NULL DEFAULT 0,
            closed_jobs INTEGER NOT NULL DEFAULT 0,
            reopened_jobs INTEGER NOT NULL DEFAULT 0,
            unchanged_jobs INTEGER NOT NULL DEFAULT 0,
            articles_discovered INTEGER NOT NULL DEFAULT 0,
            ai_calls INTEGER NOT NULL DEFAULT 0,
            model_tokens_used INTEGER NOT NULL DEFAULT 0,
            errors TEXT NOT NULL DEFAULT '[]',
            source_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS radar_sync_batches (
            idempotency_key TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            source_id TEXT NOT NULL,
            run_id TEXT,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS radar_locks (
            lock_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS radar_ai_cache (
            cache_key TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            result TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS radar_source_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            normalized_content TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_monitor_sources_due
            ON monitor_sources(enabled, status, priority DESC, last_checked_at);
        CREATE INDEX IF NOT EXISTS idx_radar_programs_status_year
            ON recruitment_programs(status, recruitment_year, last_changed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_programs_company
            ON recruitment_programs(company, recruitment_year);
        CREATE INDEX IF NOT EXISTS idx_radar_jobs_status_deadline
            ON radar_jobs(status, closing_date, last_changed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_jobs_company
            ON radar_jobs(company, title, city);
        CREATE INDEX IF NOT EXISTS idx_radar_jobs_program
            ON radar_jobs(program_id, status);
        CREATE INDEX IF NOT EXISTS idx_source_articles_source_publish
            ON source_articles(source_id, publish_time DESC);
        CREATE INDEX IF NOT EXISTS idx_job_sources_source_active
            ON job_sources(source_id, active, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_program_sources_source_active
            ON program_sources(source_id, active, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_events_detected
            ON radar_events(detected_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_events_entity
            ON radar_events(entity_type, entity_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_runs_started
            ON radar_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_snapshots_retention
            ON radar_source_snapshots(source_id, fetched_at DESC);
        """
    )
    _ensure_column(connection, "monitor_sources", "lease_owner", "TEXT")
    _ensure_column(connection, "monitor_sources", "lease_expires_at", "TEXT")
    _ensure_column(connection, "job_sources", "evidence", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "program_sources", "evidence", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "radar_runs", "articles_discovered", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "radar_runs", "ai_calls", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "radar_runs", "model_tokens_used", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "radar_runs", "scan_type", "TEXT NOT NULL DEFAULT 'scheduled'")
    _ensure_column(connection, "radar_runs", "force_scan", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "radar_runs", "sources_skipped", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "radar_jobs", "primary_category", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "radar_jobs", "organization_category", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "radar_jobs", "industry_tags", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "radar_jobs", "role_tags", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "radar_jobs", "description", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "radar_jobs", "responsibilities", "TEXT NOT NULL DEFAULT ''")
    # Run the versioned repair before the older metadata-only backfill, so a
    # repaired empty category always receives its matching semantic hash too.
    _backfill_employer_categories(connection)
    _backfill_primary_categories(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_radar_jobs_primary_category "
        "ON radar_jobs(primary_category, status, last_changed_at DESC)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('future_radar_v1', datetime('now'))"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('future_radar_v2_job_taxonomy', datetime('now'))"
    )
    _backfill_operator_categories(connection)


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    existing = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _backfill_primary_categories(connection: sqlite3.Connection) -> None:
    """Backfill from structured employer metadata, never company/title/JD prose."""
    rows = connection.execute(
        "SELECT id, employer_type, industry, organization_category, industry_tags, tags "
        "FROM radar_jobs "
        "WHERE primary_category IS NULL OR TRIM(primary_category)=''"
    ).fetchall()
    updates: list[tuple[str, str]] = []
    for row in rows:
        job_id, employer_type, industry, organization_category, raw_industry_tags, raw_tags = row

        def decode_list(value: object) -> list[object]:
            try:
                decoded = json.loads(value) if isinstance(value, str) else value
            except (TypeError, json.JSONDecodeError):
                decoded = []
            return list(decoded) if isinstance(decoded, (list, tuple, set)) else []

        category = infer_primary_category_from_metadata({
            "employer_type": employer_type,
            "industry": industry,
            "organization_category": organization_category,
            "industry_tags": decode_list(raw_industry_tags),
            "tags": decode_list(raw_tags),
        })
        if category:
            updates.append((category, str(job_id)))
    if updates:
        connection.executemany(
            "UPDATE radar_jobs SET primary_category=? "
            "WHERE id=? AND (primary_category IS NULL OR TRIM(primary_category)='')",
            updates,
        )


def _backfill_operator_categories(connection: sqlite3.Connection) -> None:
    """One bounded directory correction; do not mutate historical job facts.

    Preserve source links, evidence, dates, statuses, IDs, and timestamps. Only
    recognized operator/branch names get the telecom category and a matching
    semantic hash. Non-operator records remain byte-for-byte untouched.
    """
    if connection.execute(
        "SELECT version FROM schema_migrations WHERE version=?", (OPERATOR_CATEGORY_MIGRATION,)
    ).fetchone():
        return
    updates = []
    for row in connection.execute(
        "SELECT * FROM radar_jobs WHERE primary_category IS NULL OR primary_category != ?",
        ("state_tech_telecom",),
    ).fetchall():
        item = dict(row)
        if not telecom_primary_category(item.get("company")):
            continue
        item["primary_category"] = "state_tech_telecom"
        for key in ("tags", "industry_tags", "role_tags"):
            try:
                item[key] = json.loads(item[key]) if isinstance(item.get(key), str) else item.get(key)
            except (TypeError, ValueError):
                item[key] = []
        updates.append(("state_tech_telecom", semantic_hash(item, SEMANTIC_JOB_FIELDS), item["id"]))
    if updates:
        connection.executemany(
            "UPDATE radar_jobs SET primary_category=?, content_hash=? WHERE id=?", updates,
        )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
        (OPERATOR_CATEGORY_MIGRATION,),
    )
    logger.info("Future Radar operator category migration updated_count=%s", len(updates))


def _backfill_employer_categories(connection: sqlite3.Connection) -> None:
    """Repair real indexed categories without rewriting public recruiting facts.

    Existing source links, verification, statuses, dates, employing entities,
    IDs and timestamps are untouched. Revision triggers invalidate worker
    caches in this same transaction. Re-running startup does not repeat work.
    """
    if connection.execute(
        "SELECT version FROM schema_migrations WHERE version=?", (EMPLOYER_CATEGORY_MIGRATION,)
    ).fetchone():
        return
    updates = []
    for row in connection.execute("SELECT * FROM radar_jobs").fetchall():
        item = dict(row)
        for key in ("tags", "industry_tags", "role_tags"):
            try:
                item[key] = json.loads(item[key]) if isinstance(item.get(key), str) else item.get(key)
            except (TypeError, ValueError):
                item[key] = []
        category = (telecom_primary_category(item.get("company"))
                    or employer_category_override(item)
                    or item.get("primary_category")
                    or infer_primary_category_from_metadata(item))
        if not category or category == (item.get("primary_category") or ""):
            continue
        item["primary_category"] = category
        updates.append((category, semantic_hash(item, SEMANTIC_JOB_FIELDS), item["id"]))
    if updates:
        connection.executemany(
            "UPDATE radar_jobs SET primary_category=?, content_hash=? WHERE id=?", updates,
        )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
        (EMPLOYER_CATEGORY_MIGRATION,),
    )
    logger.info("Future Radar employer category migration updated_count=%s", len(updates))
