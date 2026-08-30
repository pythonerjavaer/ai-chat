"""Persistence primitives for Future Radar's SQLite data model."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from ..live_sources import is_actionable_recruitment_listing, is_recruitment_program_listing
from .normalization import clean_text, normalized_key


JSON_SOURCE_FIELDS = ("adapter_config", "query_config", "region_config")
JSON_RUN_FIELDS = ("errors", "source_ids")
JSON_JOB_FIELDS = ("tags", "industry_tags", "role_tags")

RUN_LOCK_TTL_SECONDS = 30 * 60
SOURCE_LOCK_TTL_SECONDS = 20 * 60
QUICK_SCAN_ADAPTERS = frozenset({
    "official_html",
    "official_api",
    "ats",
    "public_feed",
    "legacy_database",
    "other_public_source",
    "public_recruitment_index",
})
DEEP_SCAN_ADAPTERS = frozenset({
    "openai_web_search", "wechat_public", "wechat_web_search",
})

PUBLIC_JOB_EVENT_FIELDS = (
    "company", "title", "city", "region", "status",
    "opening_date", "closing_date", "verification_status",
)
PUBLIC_PROGRAM_EVENT_FIELDS = (
    "company", "program_name", "recruitment_year", "recruitment_type",
    "region", "status", "opening_date", "closing_date",
    "verification_status",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class RadarRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def seed_sources(self, sources: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.transaction() as connection:
            for source in sources:
                connection.execute(
                    """
                    INSERT INTO monitor_sources
                        (id, name, platform, company, source_type, url, domain,
                         account_name, account_id, enabled, priority, trust_level,
                         interval_minutes, adapter_config, query_config, region_config,
                         status, verification_status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        platform=excluded.platform,
                        company=excluded.company,
                        source_type=excluded.source_type,
                        url=excluded.url,
                        domain=excluded.domain,
                        account_name=excluded.account_name,
                        account_id=excluded.account_id,
                        enabled=excluded.enabled,
                        priority=excluded.priority,
                        trust_level=excluded.trust_level,
                        interval_minutes=excluded.interval_minutes,
                        adapter_config=excluded.adapter_config,
                        query_config=excluded.query_config,
                        region_config=excluded.region_config,
                        verification_status=excluded.verification_status,
                        status=CASE
                            WHEN excluded.enabled=0 THEN 'disabled'
                            WHEN monitor_sources.status='disabled' THEN 'pending'
                            ELSE monitor_sources.status
                        END,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source["id"], source["name"], source.get("platform", "web"),
                        source.get("company"), source["source_type"], source.get("url"),
                        source.get("domain"), source.get("account_name"),
                        source.get("account_id"), int(source.get("enabled", True)),
                        int(source.get("priority", 50)), source.get("trust_level", "discovery"),
                        int(source.get("interval_minutes", 120)),
                        _json(source.get("adapter_config", {})),
                        _json(source.get("query_config", {})),
                        _json(source.get("region_config", {})),
                        source.get("status", "pending"),
                        source.get("verification_status", "unverified"), now, now,
                    ),
                )

    @staticmethod
    def decode_source(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for field in JSON_SOURCE_FIELDS:
            item[field] = _decode_json(item.get(field), {})
        item["enabled"] = bool(item.get("enabled"))
        return item

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_sources WHERE id = ?", (source_id,)
            ).fetchone()
        return self.decode_source(row)

    def list_sources(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if enabled is not None:
            where = "WHERE enabled = ?"
            params.append(int(enabled))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT ms.*,
                    (SELECT sa.article_title FROM source_articles sa
                     WHERE sa.source_id=ms.id ORDER BY COALESCE(sa.publish_time, sa.first_seen_at) DESC
                     LIMIT 1) AS latest_article_title,
                    (SELECT sa.publish_time FROM source_articles sa
                     WHERE sa.source_id=ms.id ORDER BY COALESCE(sa.publish_time, sa.first_seen_at) DESC
                     LIMIT 1) AS latest_article_at
                FROM monitor_sources ms {where}
                ORDER BY ms.priority DESC, ms.name COLLATE NOCASE
                """,
                params,
            ).fetchall()
        return [self.decode_source(row) or {} for row in rows]

    def due_sources(self, *, source_ids: list[str] | None = None) -> list[dict[str, Any]]:
        sources = self.list_sources(enabled=True)
        selected = set(source_ids or [])
        now = datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for source in sources:
            if selected and source["id"] not in selected:
                continue
            # Registry placeholders without a lawful public discovery entry
            # are status signals, not runnable crawlers. A scheduler pass must
            # not turn known limitations into fake source failures. Manual
            # Quick/Deep selection also excludes them by adapter family.
            if (
                not selected
                and source.get("adapter_config", {}).get("adapter")
                == "discovery_limited"
            ):
                continue
            last_checked = source.get("last_checked_at")
            if selected or not last_checked:
                due.append(source)
                continue
            try:
                checked = datetime.fromisoformat(str(last_checked).replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(source)
                continue
            if now - checked >= timedelta(minutes=max(1, int(source["interval_minutes"]))):
                due.append(source)
        return due

    @staticmethod
    def _adapter_name(source: dict[str, Any]) -> str:
        return str(
            source.get("adapter_config", {}).get("adapter")
            or source.get("source_type")
            or ""
        ).strip().casefold()

    @classmethod
    def _manual_scan_family(cls, source: dict[str, Any]) -> str | None:
        """Classify runnable sources without consulting scheduler due-times.

        A manual Quick Scan is deliberately deterministic.  Deep Scan owns
        discovery providers, including publicly configured WeChat articles and
        OpenAI web discovery.  ``discovery_limited`` placeholders are registry
        health signals rather than runnable sources and therefore belong to
        neither family.
        """
        adapter = cls._adapter_name(source)
        if source.get("id") == "legacy-recruitment-pipeline" and adapter == "legacy_database":
            return "quick"
        if adapter in QUICK_SCAN_ADAPTERS:
            return "quick"
        if adapter in DEEP_SCAN_ADAPTERS:
            return "deep"
        return None

    def manual_scan_sources(
        self,
        scan_type: str,
        source_ids: list[str] | None = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Return sources for a user-initiated scan.

        Manual scans intentionally ignore ``interval_minutes``: that field is
        exclusively a Scheduler cadence.  ``force`` is accepted as part of the
        service contract and bypasses freshness/due-time selection, but it does
        not reactivate a disabled source or make an unsafe placeholder
        runnable.  Run, source, domain and provider safety locks are enforced
        later by the service/adapters.
        """
        del force  # Selection already ignores due/freshness; safety remains.
        normalized_type = str(scan_type or "").strip().casefold()
        if normalized_type not in {"quick", "deep"}:
            raise ValueError("scan_type must be 'quick' or 'deep'.")
        selected = set(source_ids or [])
        result: list[dict[str, Any]] = []
        for source in self.list_sources(enabled=True):
            if selected and source["id"] not in selected:
                continue
            if self._manual_scan_family(source) != normalized_type:
                continue
            result.append(source)
        return result

    def deep_scan_retry_after(self, source_ids: list[str] | None = None) -> int:
        """Deep Scan has no post-run cooldown; active work is protected by locks.

        The argument is retained for a stable API and future provider-specific
        policies.  Manual Deep scans explicitly bypass optional AI extraction
        cache reuse; external provider/domain safety limits remain independent
        of this manual-run policy.
        """
        del source_ids
        return 0

    def user_scannable_sources(
        self, *, source_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Backward-compatible alias for the deterministic Quick Scan set."""
        return self.manual_scan_sources("quick", source_ids=source_ids)

    def create_source(self, source: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO monitor_sources
                    (id, name, platform, company, source_type, url, domain,
                     account_name, account_id, enabled, priority, trust_level,
                     interval_minutes, adapter_config, query_config, region_config,
                     status, verification_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source["id"], source["name"], source.get("platform", "web"),
                    source.get("company"), source["source_type"], source.get("url"),
                    source.get("domain"), source.get("account_name"), source.get("account_id"),
                    int(source.get("enabled", True)), int(source.get("priority", 50)),
                    source.get("trust_level", "discovery"),
                    int(source.get("interval_minutes", 120)),
                    _json(source.get("adapter_config", {})),
                    _json(source.get("query_config", {})),
                    _json(source.get("region_config", {})),
                    source.get("status", "pending"),
                    source.get("verification_status", "unverified"), now, now,
                ),
            )
        return self.get_source(source["id"]) or {}

    def patch_source(self, source_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name", "platform", "company", "source_type", "url", "domain",
            "account_name", "account_id", "enabled", "priority", "trust_level",
            "interval_minutes", "adapter_config", "query_config", "region_config",
        }
        fields = [field for field in changes if field in allowed]
        if not fields:
            return self.get_source(source_id)
        values: list[Any] = []
        assignments: list[str] = []
        for field in fields:
            value = changes[field]
            if field in JSON_SOURCE_FIELDS:
                value = _json(value or {})
            if field == "enabled":
                value = int(bool(value))
            assignments.append(f"{field} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.extend([utc_now(), source_id])
        with self.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE monitor_sources SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                return None
            if "enabled" in changes:
                connection.execute(
                    """
                    UPDATE monitor_sources SET status=CASE
                        WHEN enabled=0 THEN 'disabled'
                        WHEN status='disabled' THEN 'pending'
                        ELSE status END
                    WHERE id=?
                    """,
                    (source_id,),
                )
        return self.get_source(source_id)

    def acquire_lock(self, name: str, owner: str, ttl_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        now_iso = now.isoformat()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT owner, expires_at FROM radar_locks WHERE lock_name = ?", (name,)
            ).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now_iso:
                return False
            connection.execute(
                """
                INSERT INTO radar_locks (lock_name, owner, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_name) DO UPDATE SET
                    owner=excluded.owner, expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (name, owner, expires, now_iso),
            )
        return True

    def renew_lock(self, name: str, owner: str, ttl_seconds: int) -> bool:
        """Extend a lease only while the caller still owns it.

        The owner predicate prevents a delayed heartbeat from reclaiming a
        lease that another worker acquired after expiry.
        """
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE radar_locks SET expires_at=?, updated_at=?
                WHERE lock_name=? AND owner=?
                """,
                (expires, now.isoformat(), name, owner),
            )
        return cursor.rowcount == 1

    def release_lock(self, name: str, owner: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM radar_locks WHERE lock_name = ? AND owner = ?", (name, owner)
            )

    def create_run(
        self,
        trigger_type: str,
        source_ids: list[str],
        *,
        scan_type: str = "scheduled",
        force: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO radar_runs
                    (id, trigger_type, scan_type, force_scan, started_at, status,
                     errors, source_ids, created_at)
                VALUES (?, ?, ?, ?, ?, 'running', '[]', ?, ?)
                """,
                (
                    run_id, trigger_type, str(scan_type), int(bool(force)), now,
                    _json(source_ids), now,
                ),
            )
        return self.get_run(run_id) or {"id": run_id, "status": "running"}

    def finish_run(self, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE radar_runs SET
                    finished_at=?, status=?, sources_checked=?, sources_succeeded=?,
                    sources_failed=?, sources_skipped=?, programs_discovered=?, new_jobs=?, updated_jobs=?,
                    closed_jobs=?, reopened_jobs=?, unchanged_jobs=?, articles_discovered=?,
                    ai_calls=?, model_tokens_used=?, errors=?
                WHERE id=?
                """,
                (
                    utc_now(), summary["status"], summary["sources_checked"],
                    summary["sources_succeeded"], summary["sources_failed"],
                    summary.get("sources_skipped", 0),
                    summary["programs_discovered"], summary["new_jobs"],
                    summary["updated_jobs"], summary["closed_jobs"],
                    summary["reopened_jobs"], summary["unchanged_jobs"],
                    summary.get("articles_discovered", 0), summary.get("ai_calls", 0),
                    summary.get("model_tokens_used", 0),
                    _json(summary.get("errors", [])), run_id,
                ),
            )
        return self.get_run(run_id) or {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM radar_runs WHERE id = ?", (run_id,)).fetchone()
        item = _row(row)
        if item:
            for field in JSON_RUN_FIELDS:
                item[field] = _decode_json(item.get(field), [])
            item["force_scan"] = bool(item.get("force_scan"))
        return item

    def active_run_types(self) -> list[str]:
        """Return run types whose rows still own a live database lease.

        Service-created run IDs are also their lock-owner IDs.  This lets the
        dashboard distinguish a genuinely renewed long-running task from an
        abandoned row, or from a newer worker that acquired an expired lease.
        """
        now = datetime.now(timezone.utc)
        failure = _json([{
            "source_id": "",
            "code": "RUN_LEASE_EXPIRED",
            "message": "The Radar worker lease expired before completion.",
        }])
        with self.transaction() as connection:
            running = connection.execute(
                """
                SELECT r.id, r.scan_type,
                       CASE WHEN l.owner=r.id AND l.expires_at>? THEN 1 ELSE 0 END
                           AS lease_active
                FROM radar_runs r
                LEFT JOIN radar_locks l
                  ON l.lock_name=('future-radar-run:' || r.scan_type)
                WHERE r.status='running'
                  AND r.scan_type IN ('quick', 'deep', 'scheduled')
                """,
                (now.isoformat(),),
            ).fetchall()
            expired_ids = [str(row["id"]) for row in running if not row["lease_active"]]
            if expired_ids:
                connection.executemany(
                    """
                    UPDATE radar_runs SET status='failed', finished_at=?, errors=?
                    WHERE id=? AND status='running'
                    """,
                    [
                        (now.isoformat(), failure, run_id)
                        for run_id in expired_ids
                    ],
                )
        order = {"quick": 1, "deep": 2, "scheduled": 3}
        active = {
            str(row["scan_type"] or "scheduled")
            for row in running
            if row["lease_active"]
        }
        return sorted(active, key=lambda value: (order.get(value, 4), value))

    def list_runs(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM radar_runs").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM radar_runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for field in JSON_RUN_FIELDS:
                item[field] = _decode_json(item.get(field), [])
            item["force_scan"] = bool(item.get("force_scan"))
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def ensure_company(connection: sqlite3.Connection, company: str, *,
                       employer_type: str = "", industry: str = "") -> str:
        from .normalization import normalized_key, stable_digest

        normalized = normalized_key(company)
        company_id = stable_digest(company, prefix="company")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO radar_companies
                (id, external_id, name, normalized_name, employer_type, industry,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
                name=excluded.name,
                employer_type=CASE WHEN excluded.employer_type != ''
                    THEN excluded.employer_type ELSE radar_companies.employer_type END,
                industry=CASE WHEN excluded.industry != ''
                    THEN excluded.industry ELSE radar_companies.industry END,
                updated_at=excluded.updated_at
            """,
            (company_id, company_id, company, normalized, employer_type, industry, now, now),
        )
        row = connection.execute(
            "SELECT id FROM radar_companies WHERE normalized_name = ?", (normalized,)
        ).fetchone()
        return str(row["id"])

    @staticmethod
    def find_program(connection: sqlite3.Connection, external_id: str) -> dict[str, Any] | None:
        return _row(connection.execute(
            "SELECT * FROM recruitment_programs WHERE external_id = ?", (external_id,)
        ).fetchone())

    @staticmethod
    def find_job(connection: sqlite3.Connection, external_id: str) -> dict[str, Any] | None:
        item = _row(connection.execute(
            "SELECT * FROM radar_jobs WHERE external_id = ?", (external_id,)
        ).fetchone())
        if item:
            for field in JSON_JOB_FIELDS:
                item[field] = _decode_json(item.get(field), [])
        return item

    @staticmethod
    def insert_program(connection: sqlite3.Connection, item: dict[str, Any], *,
                       source_id: str, now: str) -> dict[str, Any]:
        program_id = str(uuid.uuid4())
        company_id = RadarRepository.ensure_company(connection, item["company"])
        connection.execute(
            """
            INSERT INTO recruitment_programs
                (id, external_id, company_id, company, program_name, recruitment_year,
                 recruitment_type, region, opening_date, closing_date, status,
                 verification_status, confidence_score, official_url, content_hash,
                 source_id, first_seen_at, last_seen_at, last_changed_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                program_id, item["external_id"], company_id, item["company"],
                item["program_name"], item.get("recruitment_year"),
                item.get("recruitment_type", "other"), item.get("region", ""),
                item.get("opening_date"), item.get("closing_date"), item.get("status", "open"),
                item.get("verification_status", "pending"), item.get("confidence_score", 0),
                item.get("official_url"), item["content_hash"], source_id,
                now, now, now, now, now,
            ),
        )
        return RadarRepository.find_program(connection, item["external_id"]) or {}

    @staticmethod
    def update_program(connection: sqlite3.Connection, program_id: str,
                       item: dict[str, Any], *, source_id: str, now: str) -> dict[str, Any]:
        company_id = RadarRepository.ensure_company(connection, item["company"])
        connection.execute(
            """
            UPDATE recruitment_programs SET
                company_id=?, company=?, program_name=?, recruitment_year=?,
                recruitment_type=?, region=?, opening_date=?, closing_date=?, status=?,
                verification_status=?, confidence_score=?, official_url=?, content_hash=?,
                source_id=?, missing_successes=0, last_seen_at=?, last_changed_at=?, updated_at=?
            WHERE id=?
            """,
            (
                company_id, item["company"], item["program_name"], item.get("recruitment_year"),
                item.get("recruitment_type", "other"), item.get("region", ""),
                item.get("opening_date"), item.get("closing_date"), item.get("status", "open"),
                item.get("verification_status", "pending"), item.get("confidence_score", 0),
                item.get("official_url"), item["content_hash"], source_id,
                now, now, now, program_id,
            ),
        )
        return RadarRepository.find_program(connection, item["external_id"]) or {}

    @staticmethod
    def touch_program(connection: sqlite3.Connection, program_id: str, now: str) -> None:
        connection.execute(
            "UPDATE recruitment_programs SET last_seen_at=?, missing_successes=0, updated_at=? WHERE id=?",
            (now, now, program_id),
        )

    @staticmethod
    def insert_job(connection: sqlite3.Connection, item: dict[str, Any], *,
                   source_id: str, program_id: str | None, now: str) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        company_id = RadarRepository.ensure_company(
            connection, item["company"], employer_type=item.get("employer_type", ""),
            industry=item.get("industry", ""),
        )
        connection.execute(
            """
            INSERT INTO radar_jobs
                (id, external_id, program_id, company_id, company, title, city, region,
                 employer_type, industry, primary_category, organization_category,
                 industry_tags, role_tags, official_url, application_url, opening_date,
                 closing_date, status, verification_status, confidence_score, description,
                 responsibilities, requirements, tags, content_hash, source_id, first_seen_at,
                 last_seen_at, last_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, item["external_id"], program_id, company_id, item["company"],
                item["title"], item.get("city", ""), item.get("region", ""),
                item.get("employer_type", ""), item.get("industry", ""),
                item.get("primary_category", ""), item.get("organization_category", ""),
                _json(item.get("industry_tags", [])), _json(item.get("role_tags", [])),
                item.get("official_url"), item.get("application_url"),
                item.get("opening_date"), item.get("closing_date"), item.get("status", "open"),
                item.get("verification_status", "pending"), item.get("confidence_score", 0),
                item.get("description", ""), item.get("responsibilities", ""),
                item.get("requirements", ""), _json(item.get("tags", [])),
                item["content_hash"], source_id, now, now, now, now, now,
            ),
        )
        return RadarRepository.find_job(connection, item["external_id"]) or {}

    @staticmethod
    def update_job(connection: sqlite3.Connection, job_id: str, item: dict[str, Any], *,
                   source_id: str, program_id: str | None, now: str) -> dict[str, Any]:
        company_id = RadarRepository.ensure_company(
            connection, item["company"], employer_type=item.get("employer_type", ""),
            industry=item.get("industry", ""),
        )
        connection.execute(
            """
            UPDATE radar_jobs SET
                program_id=?, company_id=?, company=?, title=?, city=?, region=?,
                employer_type=?, industry=?, primary_category=?, organization_category=?,
                industry_tags=?, role_tags=?, official_url=?, application_url=?, opening_date=?,
                closing_date=?, status=?, verification_status=?, confidence_score=?,
                description=?, responsibilities=?, requirements=?, tags=?, content_hash=?,
                source_id=?, missing_successes=0, last_seen_at=?, last_changed_at=?, updated_at=?
            WHERE id=?
            """,
            (
                program_id, company_id, item["company"], item["title"], item.get("city", ""),
                item.get("region", ""), item.get("employer_type", ""),
                item.get("industry", ""), item.get("primary_category", ""),
                item.get("organization_category", ""), _json(item.get("industry_tags", [])),
                _json(item.get("role_tags", [])), item.get("official_url"),
                item.get("application_url"), item.get("opening_date"), item.get("closing_date"),
                item.get("status", "open"), item.get("verification_status", "pending"),
                item.get("confidence_score", 0), item.get("description", ""),
                item.get("responsibilities", ""), item.get("requirements", ""),
                _json(item.get("tags", [])), item["content_hash"], source_id, now, now,
                now, job_id,
            ),
        )
        return RadarRepository.find_job(connection, item["external_id"]) or {}

    @staticmethod
    def touch_job(connection: sqlite3.Connection, job_id: str, now: str) -> None:
        connection.execute(
            "UPDATE radar_jobs SET last_seen_at=?, missing_successes=0, updated_at=? WHERE id=?",
            (now, now, job_id),
        )

    @staticmethod
    def link_job_source(connection: sqlite3.Connection, *, job_id: str, source: dict[str, Any],
                        source_url: str | None, verification_role: str, now: str,
                        evidence: list[str] | None = None) -> None:
        connection.execute(
            """
            INSERT INTO job_sources
                (job_id, source_id, source_url, discovered_at, last_seen_at,
                 source_type, verification_role, evidence, active, missing_successes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(job_id, source_id) DO UPDATE SET
                source_url=COALESCE(excluded.source_url, job_sources.source_url),
                last_seen_at=excluded.last_seen_at,
                source_type=excluded.source_type,
                verification_role=CASE
                    WHEN excluded.verification_role='verification' THEN 'verification'
                    ELSE job_sources.verification_role END,
                evidence=CASE WHEN excluded.evidence!='[]'
                    THEN excluded.evidence ELSE job_sources.evidence END,
                active=1, missing_successes=0
            """,
            (
                job_id, source["id"], source_url, now, now, source["source_type"],
                verification_role, _json(evidence or []),
            ),
        )

    @staticmethod
    def link_program_source(connection: sqlite3.Connection, *, program_id: str,
                            source: dict[str, Any], source_url: str | None,
                            verification_role: str, now: str,
                            evidence: list[str] | None = None) -> None:
        connection.execute(
            """
            INSERT INTO program_sources
                (program_id, source_id, source_url, discovered_at, last_seen_at,
                 source_type, verification_role, evidence, active, missing_successes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(program_id, source_id) DO UPDATE SET
                source_url=COALESCE(excluded.source_url, program_sources.source_url),
                last_seen_at=excluded.last_seen_at,
                source_type=excluded.source_type,
                verification_role=CASE
                    WHEN excluded.verification_role='verification' THEN 'verification'
                    ELSE program_sources.verification_role END,
                evidence=CASE WHEN excluded.evidence!='[]'
                    THEN excluded.evidence ELSE program_sources.evidence END,
                active=1, missing_successes=0
            """,
            (
                program_id, source["id"], source_url, now, now, source["source_type"],
                verification_role, _json(evidence or []),
            ),
        )

    @staticmethod
    def insert_event(connection: sqlite3.Connection, *, run_id: str, entity_type: str,
                     entity_id: str, external_id: str, event_type: str,
                     before: dict[str, Any] | None, after: dict[str, Any] | None,
                     fields: list[str], source_id: str, now: str) -> int:
        identity = f"{run_id}:{entity_type}:{entity_id}:{event_type}:{','.join(fields)}"
        event_key = __import__("hashlib").sha256(identity.encode("utf-8")).hexdigest()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO radar_events
                (event_key, run_id, entity_type, entity_id, external_id, event_type,
                 before_data, after_data, changed_fields, detected_at, source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key, run_id, entity_type, entity_id, external_id, event_type,
                _json(before) if before is not None else None,
                _json(after) if after is not None else None,
                _json(fields), now, source_id,
            ),
        )
        return int(cursor.lastrowid or 0)

    @staticmethod
    def upsert_article(connection: sqlite3.Connection, item: dict[str, Any], *,
                       source_id: str, now: str) -> tuple[str, bool, bool]:
        row = connection.execute(
            "SELECT id, content_hash FROM source_articles WHERE source_id=? AND article_external_id=?",
            (source_id, item["article_external_id"]),
        ).fetchone()
        if row:
            connection.execute(
                """
                UPDATE source_articles SET publisher=?, article_title=?, article_url=?,
                    publish_time=?, content_hash=?, raw_excerpt=?, is_recruitment=?,
                    recruitment_year=?, classification=?, last_seen_at=?
                WHERE id=?
                """,
                (
                    item.get("publisher", ""), item["article_title"], item.get("article_url"),
                    item.get("publish_time"), item["content_hash"], item.get("raw_excerpt", ""),
                    int(bool(item.get("is_recruitment"))), item.get("recruitment_year"),
                    item.get("classification", "unknown"), now, row["id"],
                ),
            )
            return str(row["id"]), False, row["content_hash"] != item["content_hash"]
        article_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO source_articles
                (id, source_id, article_external_id, publisher, article_title, article_url,
                 publish_time, content_hash, raw_excerpt, is_recruitment, recruitment_year,
                 classification, first_seen_at, last_seen_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id, source_id, item["article_external_id"], item.get("publisher", ""),
                item["article_title"], item.get("article_url"), item.get("publish_time"),
                item["content_hash"], item.get("raw_excerpt", ""),
                int(bool(item.get("is_recruitment"))), item.get("recruitment_year"),
                item.get("classification", "unknown"), now, now, now,
            ),
        )
        return article_id, True, True

    def update_source_success(self, source_id: str, *, content_hash: str | None,
                              status: str = "healthy") -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE monitor_sources SET status=?, last_checked_at=?, last_success_at=?,
                    last_error=NULL, last_content_hash=COALESCE(?, last_content_hash),
                    consecutive_failures=0, updated_at=? WHERE id=?
                """,
                (status, now, now, content_hash, now, source_id),
            )

    def update_source_limited(self, source_id: str, message: str) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE monitor_sources SET status='discovery_limited', last_checked_at=?,
                    last_error_at=?, last_error=?, updated_at=? WHERE id=?
                """,
                (now, now, message[:500], now, source_id),
            )

    def update_source_error(self, source_id: str, message: str) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE monitor_sources SET status='error', last_checked_at=?, last_error_at=?,
                    last_error=?, consecutive_failures=consecutive_failures+1, updated_at=?
                WHERE id=?
                """,
                (now, now, message[:500], now, source_id),
            )

    @staticmethod
    def linked_job_ids(connection: sqlite3.Connection, source_id: str) -> list[str]:
        rows = connection.execute(
            "SELECT job_id FROM job_sources WHERE source_id=? AND active=1", (source_id,)
        ).fetchall()
        return [str(row["job_id"]) for row in rows]

    @staticmethod
    def process_missing_jobs(connection: sqlite3.Connection, *, source: dict[str, Any],
                             seen_job_ids: set[str], threshold: int, run_id: str,
                             now: str) -> int:
        closed = 0
        rows = connection.execute(
            "SELECT job_id, missing_successes FROM job_sources WHERE source_id=? AND active=1",
            (source["id"],),
        ).fetchall()
        for row in rows:
            job_id = str(row["job_id"])
            if job_id in seen_job_ids:
                continue
            missing = int(row["missing_successes"]) + 1
            connection.execute(
                "UPDATE job_sources SET missing_successes=? WHERE job_id=? AND source_id=?",
                (missing, job_id, source["id"]),
            )
            if missing < threshold:
                continue
            connection.execute(
                "UPDATE job_sources SET active=0 WHERE job_id=? AND source_id=?",
                (job_id, source["id"]),
            )
            remaining = int(connection.execute(
                "SELECT COUNT(*) FROM job_sources WHERE job_id=? AND active=1", (job_id,)
            ).fetchone()[0])
            job = _row(connection.execute("SELECT * FROM radar_jobs WHERE id=?", (job_id,)).fetchone())
            if remaining == 0 and job and job["status"] != "closed":
                before = dict(job)
                connection.execute(
                    "UPDATE radar_jobs SET status='closed', last_changed_at=?, updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
                after = {**before, "status": "closed", "last_changed_at": now, "updated_at": now}
                RadarRepository.insert_event(
                    connection, run_id=run_id, entity_type="job", entity_id=job_id,
                    external_id=job["external_id"], event_type="CLOSED", before=before,
                    after=after, fields=["status"], source_id=source["id"], now=now,
                )
                closed += 1
        return closed

    @staticmethod
    def process_missing_programs(connection: sqlite3.Connection, *, source: dict[str, Any],
                                 seen_program_ids: set[str], threshold: int, run_id: str,
                                 now: str) -> int:
        closed = 0
        rows = connection.execute(
            "SELECT program_id, missing_successes FROM program_sources WHERE source_id=? AND active=1",
            (source["id"],),
        ).fetchall()
        for row in rows:
            program_id = str(row["program_id"])
            if program_id in seen_program_ids:
                continue
            missing = int(row["missing_successes"]) + 1
            connection.execute(
                "UPDATE program_sources SET missing_successes=? WHERE program_id=? AND source_id=?",
                (missing, program_id, source["id"]),
            )
            if missing < threshold:
                continue
            connection.execute(
                "UPDATE program_sources SET active=0 WHERE program_id=? AND source_id=?",
                (program_id, source["id"]),
            )
            remaining = int(connection.execute(
                "SELECT COUNT(*) FROM program_sources WHERE program_id=? AND active=1",
                (program_id,),
            ).fetchone()[0])
            program = _row(connection.execute(
                "SELECT * FROM recruitment_programs WHERE id=?", (program_id,)
            ).fetchone())
            if remaining == 0 and program and program["status"] != "closed":
                before = dict(program)
                connection.execute(
                    "UPDATE recruitment_programs SET status='closed', last_changed_at=?, updated_at=? WHERE id=?",
                    (now, now, program_id),
                )
                after = {**before, "status": "closed", "last_changed_at": now, "updated_at": now}
                RadarRepository.insert_event(
                    connection, run_id=run_id, entity_type="program", entity_id=program_id,
                    external_id=program["external_id"], event_type="PROGRAM_CLOSED",
                    before=before, after=after, fields=["status"], source_id=source["id"], now=now,
                )
                closed += 1
        return closed

    def save_snapshot(self, source_id: str, content_hash: str,
                      normalized_content: str, metadata: dict[str, Any]) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO radar_source_snapshots
                    (source_id, fetched_at, content_hash, normalized_content, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, now, content_hash, normalized_content[:20_000], _json(metadata)),
            )
            connection.execute(
                """
                DELETE FROM radar_source_snapshots
                WHERE source_id=? AND id NOT IN (
                    SELECT id FROM radar_source_snapshots
                    WHERE source_id=? ORDER BY fetched_at DESC LIMIT 10
                )
                """,
                (source_id, source_id),
            )

    def latest_snapshot_metadata(self, source_id: str) -> dict[str, Any] | None:
        """Return counts from the latest completed fetch, not its page content."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fetched_at, metadata FROM radar_source_snapshots
                WHERE source_id=? ORDER BY fetched_at DESC, id DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "fetched_at": row["fetched_at"],
            "metadata": _decode_json(row["metadata"], {}),
        }

    def sync_batch(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM radar_sync_batches WHERE idempotency_key=?", (key,)
            ).fetchone()
        item = _row(row)
        if item:
            item["result"] = _decode_json(item.get("result"), {})
        return item

    def save_sync_batch(self, *, key: str, payload_hash: str, source_id: str,
                        run_id: str, result: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO radar_sync_batches
                    (idempotency_key, payload_hash, source_id, run_id, result, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, payload_hash, source_id, run_id, _json(result), utc_now()),
            )

    def get_ai_cache(self, key: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM radar_ai_cache WHERE cache_key=?", (key,)).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE radar_ai_cache SET last_used_at=? WHERE cache_key=?", (utc_now(), key)
            )
        item = dict(row)
        item["result"] = _decode_json(item.get("result"), {})
        return item

    def save_ai_cache(self, *, key: str, content_hash: str, model: str,
                      schema_version: str, result: dict[str, Any],
                      input_tokens: int, output_tokens: int) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO radar_ai_cache
                    (cache_key, content_hash, model, schema_version, result,
                     input_tokens, output_tokens, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result=excluded.result, input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens, last_used_at=excluded.last_used_at
                """,
                (key, content_hash, model, schema_version, _json(result),
                 input_tokens, output_tokens, now, now),
            )

    @staticmethod
    def _decode_job(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field in JSON_JOB_FIELDS:
            item[field] = _decode_json(item.get(field), [])
        return item

    def _job_sources(self, connection: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT js.source_id, ms.name, ms.source_type, ms.trust_level, js.source_url,
                   js.verification_role, js.evidence, js.discovered_at, js.last_seen_at, js.active
            FROM job_sources js JOIN monitor_sources ms ON ms.id=js.source_id
            WHERE js.job_id=? ORDER BY js.verification_role DESC, js.discovered_at
            """,
            (job_id,),
        ).fetchall()
        return [
            {**dict(row), "active": bool(row["active"]), "evidence": _decode_json(row["evidence"], [])}
            for row in rows
        ]

    @staticmethod
    def _job_filter_clause(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        """Build the shared, parameterised job filter used by lists and stats."""
        where: list[str] = ["1=1"]
        params: list[Any] = []
        status = filters.get("status", "open")
        if status and status != "all":
            where.append("j.status=?")
            params.append(status)
        if filters.get("active_only", status == "open"):
            where.append("(j.closing_date IS NULL OR j.closing_date>DATE('now'))")
        for field in (
            "verification_status", "company", "city", "region", "employer_type",
            "industry", "primary_category", "organization_category", "program_id",
        ):
            value = filters.get(field)
            if value:
                where.append(f"j.{field}=?")
                params.append(value)
        primary_categories = filters.get("primary_categories")
        if primary_categories:
            if not isinstance(primary_categories, (list, tuple, set, frozenset)):
                primary_categories = [primary_categories]
            values = sorted({
                str(value).strip()
                for value in primary_categories
                if str(value).strip()
            })
            if values:
                placeholders = ",".join("?" for _ in values)
                where.append(f"j.primary_category IN ({placeholders})")
                params.extend(values)
        if filters.get("source_id"):
            where.append(
                "EXISTS (SELECT 1 FROM job_sources x "
                "WHERE x.job_id=j.id AND x.source_id=?)"
            )
            params.append(filters["source_id"])
        if filters.get("discovery_source_only"):
            # An attested web-search result may have a verification-role link
            # even though it originated from a discovery source.  Source trust
            # therefore identifies the search-update pool more accurately than
            # job_sources.verification_role alone.
            where.append(
                "EXISTS (SELECT 1 FROM job_sources ds "
                "JOIN monitor_sources dms ON dms.id=ds.source_id "
                "WHERE ds.job_id=j.id AND dms.trust_level='discovery')"
            )
        if filters.get("q"):
            needle = f"%{filters['q']}%"
            where.append(
                "(j.company LIKE ? OR j.title LIKE ? OR j.description LIKE ? "
                "OR j.responsibilities LIKE ? OR j.requirements LIKE ?)"
            )
            params.extend([needle, needle, needle, needle, needle])
        if filters.get("event_type"):
            where.append(
                "EXISTS (SELECT 1 FROM radar_events re "
                "WHERE re.entity_type='job' AND re.entity_id=j.id AND re.event_type=?)"
            )
            params.append(filters["event_type"])
        if filters.get("opening_before"):
            where.append("j.opening_date IS NOT NULL AND j.opening_date<=?")
            params.append(filters["opening_before"])
        if filters.get("opening_after"):
            where.append("j.opening_date IS NOT NULL AND j.opening_date>=?")
            params.append(filters["opening_after"])
        if filters.get("closing_before"):
            where.append("j.closing_date IS NOT NULL AND j.closing_date<=?")
            params.append(filters["closing_before"])
        if filters.get("closing_after"):
            where.append("j.closing_date IS NOT NULL AND j.closing_date>=?")
            params.append(filters["closing_after"])
        return " AND ".join(where), params

    def list_jobs(self, *, page: int = 1, page_size: int = 50,
                  filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        clause, params = self._job_filter_clause(filters)
        sort = filters.get("sort", "changed")
        order = {
            "closing": "j.closing_date IS NULL, j.closing_date, j.last_changed_at DESC, j.id",
            "opening": "j.opening_date IS NULL, j.opening_date DESC, j.last_changed_at DESC, j.id",
            "first_seen": "j.first_seen_at DESC, j.id",
            "company": "j.company COLLATE NOCASE, j.title COLLATE NOCASE, j.id",
            "changed": "j.last_changed_at DESC, j.id",
        }.get(sort, "j.last_changed_at DESC, j.id")
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM radar_jobs j WHERE {clause}", params
            ).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT j.*, p.program_name, p.recruitment_year,
                    (SELECT e.event_type FROM radar_events e
                     WHERE e.entity_type='job' AND e.entity_id=j.id
                     ORDER BY e.id DESC LIMIT 1) AS latest_event_type,
                    (SELECT e.detected_at FROM radar_events e
                     WHERE e.entity_type='job' AND e.entity_id=j.id
                     ORDER BY e.id DESC LIMIT 1) AS latest_event_at
                FROM radar_jobs j
                LEFT JOIN recruitment_programs p ON p.id=j.program_id
                WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = self._decode_job(row)
                sources = self._job_sources(connection, item["id"])
                item["sources"] = sources
                item["discovered_by"] = [s for s in sources if s["verification_role"] == "discovery"]
                item["verified_by"] = [s for s in sources if s["verification_role"] == "verification"]
                items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def job_stats(self, *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return unpaginated counts for the same safe filters as ``list_jobs``."""
        filters = filters or {}
        clause, params = self._job_filter_clause(filters)
        with self._connect() as connection:
            verification_rows = connection.execute(
                f"""
                SELECT j.verification_status, COUNT(*) AS count
                FROM radar_jobs j WHERE {clause}
                GROUP BY j.verification_status
                """,
                params,
            ).fetchall()
            status_rows = connection.execute(
                f"""
                SELECT j.status, COUNT(*) AS count
                FROM radar_jobs j WHERE {clause}
                GROUP BY j.status
                """,
                params,
            ).fetchall()
            category_rows = connection.execute(
                f"""
                SELECT COALESCE(NULLIF(j.primary_category, ''), 'uncategorized') AS category,
                       COUNT(*) AS count
                FROM radar_jobs j WHERE {clause}
                GROUP BY COALESCE(NULLIF(j.primary_category, ''), 'uncategorized')
                """,
                params,
            ).fetchall()
            source_count = int(connection.execute(
                f"""
                SELECT COUNT(DISTINCT js.source_id)
                FROM radar_jobs j
                JOIN job_sources js ON js.job_id=j.id
                JOIN monitor_sources ms ON ms.id=js.source_id
                WHERE {clause} AND ms.trust_level='discovery'
                """,
                params,
            ).fetchone()[0])

        verification_counts = {
            key: 0 for key in ("pending", "verified", "conflicted", "rejected")
        }
        verification_counts.update({
            str(row["verification_status"]): int(row["count"])
            for row in verification_rows
        })
        job_status_counts = {key: 0 for key in ("open", "closed", "unknown")}
        job_status_counts.update({
            str(row["status"]): int(row["count"])
            for row in status_rows
        })
        return {
            "total_candidates": sum(verification_counts.values()),
            "verification_status": verification_counts,
            "job_status": job_status_counts,
            "primary_category": {
                str(row["category"]): int(row["count"])
                for row in category_rows
            },
            "source_count": source_count,
        }

    @staticmethod
    def _campus_opportunity(job: dict[str, Any]) -> bool:
        """Require campus meaning, not an official-page fetch or a known date."""
        title = clean_text(job.get("title"))
        recruitment_type = str(job.get("recruitment_type") or "").casefold()
        if (
            re.search(r"社会招聘|社招|experienced\s+hir|lateral\s+hir", title, re.I)
            or recruitment_type in {"social", "experienced", "lateral"}
        ):
            return False
        text = " ".join(str(job.get(field) or "") for field in (
            "title", "description", "responsibilities", "requirements", "program_name",
        )) + " " + " ".join(str(value) for value in job.get("tags", []))
        if re.search(r"不(?:接受|面向|招收)应届|仅限社会招聘|非应届(?:生|毕业生)", text):
            return False
        if is_recruitment_program_listing(job):
            return True
        # Verified data already passed the existing source pipeline. New leads
        # need affirmative campus evidence, but unknown deadlines are fine.
        campus = bool(re.search(
            r"校园招聘|校招|秋招|春招|应届|毕业生|20\d{2}\s*届|campus|graduate|early[ -]career",
            text, re.I,
        )) or recruitment_type in {"campus", "autumn", "spring", "early"}
        if not campus and job.get("verification_status") != "verified":
            return False
        if recruitment_type == "internship" and not campus:
            return False
        if not is_actionable_recruitment_listing({**job, "requirements": text}):
            return False
        return bool(title and clean_text(job.get("company"))) and not bool(re.fullmatch(
            r"(?:20\d{2}[年届]?)?\s*(?:校园招聘|校招|秋招|春招|招聘公告|招聘信息|招聘)",
            title,
        ))

    @staticmethod
    def _opportunity_identity(
        job: dict[str, Any], company_aliases: dict[str, str],
    ) -> tuple[str, str, str]:
        company = normalized_key(job.get("company"))
        company = company_aliases.get(company, company)
        title = clean_text(job.get("title")).casefold()
        title = re.sub(r"20\d{2}\s*(?:[年届])?", "", title)
        title = re.sub(
            r"(?:秋季|春季)?校园招聘|(?:秋季|春季)?校招|campus\s+(?:recruitment|hiring)",
            "", title, flags=re.I,
        )
        role = normalized_key(title)
        aliases = {company, normalized_key(job.get("company"))}
        aliases.update(key for key, value in company_aliases.items() if value == company)
        for alias in sorted(aliases, key=len, reverse=True):
            if alias and role.startswith(alias) and len(role) > len(alias):
                role = role[len(alias):]
                break
        # Never collapse distinct roles just because they share a campaign URL.
        city = normalized_key(re.sub(r"市(?=$|[、,，/;；])", "", str(job.get("city") or "")))
        return company, role, city

    def _opportunity_rows(
        self, *, filters: dict[str, Any], public_url: Callable[[Any], str | None],
        company_aliases: dict[str, str],
    ) -> list[dict[str, Any]]:
        # Resolve identity/status across all provenance before filtering. A
        # source, date or verification filter must never hide an authoritative
        # closed copy and resurrect the same opportunity's stale open lead.
        query_filters = {**filters, "status": "all", "active_only": False, "source_id": None}
        match_clause, match_params = self._job_filter_clause(query_filters)
        clause = (
            "j.verification_status IN ('verified','pending','conflicted')"
            " AND (j.verification_status='verified' OR EXISTS ("
            "SELECT 1 FROM job_sources os JOIN monitor_sources oms ON oms.id=os.source_id "
            "WHERE os.job_id=j.id AND oms.trust_level='discovery'))"
        )
        order = {
            "closing": "j.closing_date IS NULL, j.closing_date, j.last_changed_at DESC, j.id",
            "opening": "j.opening_date IS NULL, j.opening_date DESC, j.last_changed_at DESC, j.id",
            "first_seen": "j.first_seen_at DESC, j.id",
            "company": "j.company COLLATE NOCASE, j.title COLLATE NOCASE, j.id",
            "changed": "j.last_changed_at DESC, j.id",
        }.get(filters.get("sort", "changed"), "j.last_changed_at DESC, j.id")
        with self._connect() as connection:
            matching_ids = {
                row["id"] for row in connection.execute(
                    f"SELECT j.id FROM radar_jobs j WHERE {match_clause}", match_params,
                ).fetchall()
            }
            rows = connection.execute(
                f"""
                SELECT j.*, p.program_name, p.recruitment_year, p.recruitment_type,
                    (SELECT e.event_type FROM radar_events e
                     WHERE e.entity_type='job' AND e.entity_id=j.id
                     ORDER BY e.id DESC LIMIT 1) AS latest_event_type,
                    (SELECT e.detected_at FROM radar_events e
                     WHERE e.entity_type='job' AND e.entity_id=j.id
                     ORDER BY e.id DESC LIMIT 1) AS latest_event_at
                FROM radar_jobs j LEFT JOIN recruitment_programs p ON p.id=j.program_id
                WHERE {clause} ORDER BY {order}
                """,
            ).fetchall()
            # One provenance query, not an extra query per scored job. Evidence
            # and source adapter/query configs are deliberately never loaded.
            source_rows = connection.execute(
                f"""
                SELECT js.job_id, js.source_id, ms.name, ms.source_type, ms.trust_level,
                    js.source_url, js.verification_role, js.discovered_at, js.last_seen_at, js.active
                FROM radar_jobs j JOIN job_sources js ON js.job_id=j.id
                JOIN monitor_sources ms ON ms.id=js.source_id WHERE {clause}
                ORDER BY js.verification_role DESC, js.source_id
                """,
            ).fetchall()
        source_map: dict[str, list[dict[str, Any]]] = {}
        for row in source_rows:
            source = dict(row)
            job_id = source.pop("job_id")
            source["active"] = bool(source["active"])
            source_map.setdefault(job_id, []).append(source)

        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for position, row in enumerate(rows):
            job = self._decode_job(row)
            if not self._campus_opportunity(job):
                continue
            application_url = public_url(job.get("application_url"))
            official_url = public_url(job.get("official_url"))
            if not application_url and not official_url:
                continue
            job["application_url"] = application_url or official_url
            job["official_url"] = official_url
            job["sources"] = source_map.get(job["id"], [])
            job["_sort_position"] = position
            year = job.get("recruitment_year")
            if not year:
                match = re.search(r"(?<!\d)(20\d{2})(?!\d)", str(job.get("title") or ""))
                year = match.group(1) if match else None
            job["_cohort"] = str(year or "")
            groups.setdefault(self._opportunity_identity(job, company_aliases), []).append(job)

        deduped = []
        for group in groups.values():
            known_years = {item["_cohort"] for item in group if item["_cohort"]}
            cohorts: dict[str, list[dict[str, Any]]] = {}
            for item in group:
                # An unknown cohort can join one unambiguous campaign, but
                # must not merge two explicitly different graduating classes.
                cohort = item["_cohort"] or (next(iter(known_years)) if len(known_years) == 1 else "")
                cohorts.setdefault(cohort, []).append(item)
            for members in cohorts.values():
                winner = max(members, key=lambda item: (
                    item.get("verification_status") == "verified",
                    str(item.get("last_changed_at") or ""),
                    item.get("verification_status") == "pending",
                    str(item["id"]),
                ))
                winner = dict(winner)
                provenance = {}
                for member in members:
                    for source in member["sources"]:
                        key = (source["source_id"], source.get("source_url"), source.get("verification_role"))
                        provenance[key] = source
                winner["sources"] = list(provenance.values())
                winner["_member_ids"] = {member["id"] for member in members}
                winner["_member_external_ids"] = {member["external_id"] for member in members}
                if winner["id"] not in matching_ids:
                    continue
                if filters.get("source_id") and not any(
                    source["source_id"] == filters["source_id"] for source in winner["sources"]
                ):
                    continue
                # An unconfirmed opening state is still a usable discovery.
                # Keep that distinction in the record, but include it in the
                # default pool unless it is closed/expired or retired by all
                # its sources. Explicit open/unknown/archive filters remain.
                status = filters.get("status", "active")
                if status == "active" and winner.get("status") not in {"open", "unknown"}:
                    continue
                if status and status not in {"active", "all"} and winner.get("status") != status:
                    continue
                closing_date = str(winner.get("closing_date") or "")
                if filters.get("active_only", status in {"active", "open"}) and (
                    closing_date and closing_date <= date.today().isoformat()
                ):
                    continue
                if status in {"active", "open"} and not any(source["active"] for source in winner["sources"]):
                    continue
                deduped.append(winner)
        return sorted(deduped, key=lambda item: item["_sort_position"])

    def list_opportunities(
        self, *, page: int = 1, page_size: int = 50, filters: dict[str, Any] | None = None,
        public_url: Callable[[Any], str | None], prepare: Callable[[dict[str, Any]], dict[str, Any]],
        company_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        filters = filters or {}
        rows = self._opportunity_rows(
            filters=filters, public_url=public_url, company_aliases=company_aliases or {},
        )
        # Scoring is profile dependent. Score the complete deduplicated match
        # set before tier filtering/pagination, never only the first 50 rows.
        items = [prepare(row) for row in rows]
        tier_counts = {key: 0 for key in (
            "T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3", "UNRANKED", "BELOW_PRIORITY",
        )}
        category_counts: dict[str, int] = {}
        for item in items:
            tier = item.get("tier_code")
            bucket = tier if tier in tier_counts else "BELOW_PRIORITY" if tier else "UNRANKED"
            item["tier_bucket"] = bucket
            tier_counts[bucket] += 1
            category = str(item.get("primary_category") or "uncategorized")
            category_counts[category] = category_counts.get(category, 0) + 1
        matching_total = len(items)
        if filters.get("tier_code"):
            items = [item for item in items if item["tier_bucket"] == filters["tier_code"]]
        verification = {key: 0 for key in ("pending", "verified", "conflicted", "rejected")}
        statuses = {key: 0 for key in ("open", "closed", "unknown")}
        for item in items:
            verification[str(item.get("verification_status") or "pending")] += 1
            statuses[str(item.get("status") or "unknown")] += 1
        offset = (page - 1) * page_size
        return {
            "items": items[offset:offset + page_size],
            "total": len(items), "page": page, "page_size": page_size,
            "stats": {
                "total_opportunities": len(items), "matching_total": matching_total,
                "verified_count": verification["verified"],
                "discovered_count": verification["pending"] + verification["conflicted"],
                "verification_status": verification, "job_status": statuses,
                "tier_counts": tier_counts, "category_counts": category_counts,
                "primary_category": category_counts,
                "source_count": len({
                    source["source_id"] for item in items for source in item.get("sources", [])
                }),
            },
        }

    def get_opportunity(
        self, job_id: str, *, public_url: Callable[[Any], str | None],
        company_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        for item in self._opportunity_rows(
            # The list explicitly supports closed/unknown archive filters.
            # Those results must remain inspectable without putting them back
            # in the default open pool; rejected/private/non-campus stay out.
            filters={"status": "all", "active_only": False}, public_url=public_url,
            company_aliases=company_aliases or {},
        ):
            if job_id in item["_member_ids"] or job_id in item["_member_external_ids"]:
                return item
        return None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.*, p.program_name, p.recruitment_year
                FROM radar_jobs j LEFT JOIN recruitment_programs p ON p.id=j.program_id
                WHERE j.id=? OR j.external_id=?
                """,
                (job_id, job_id),
            ).fetchone()
            if not row:
                return None
            item = self._decode_job(row)
            item["sources"] = self._job_sources(connection, item["id"])
            events = connection.execute(
                "SELECT * FROM radar_events WHERE entity_type='job' AND entity_id=? ORDER BY id DESC LIMIT 100",
                (item["id"],),
            ).fetchall()
            item["events"] = [self.decode_event(row) for row in events]
        return item

    def list_programs(self, *, page: int = 1, page_size: int = 50,
                      status: str = "open", q: str | None = None,
                      verification_status: str | None = None) -> dict[str, Any]:
        where = ["1=1"]
        params: list[Any] = []
        if status != "all":
            where.append("p.status=?")
            params.append(status)
        if verification_status:
            where.append("p.verification_status=?")
            params.append(verification_status)
        if q:
            where.append("(p.company LIKE ? OR p.program_name LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        clause = " AND ".join(where)
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM recruitment_programs p WHERE {clause}", params
            ).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT p.*, COUNT(j.id) AS job_count,
                       COUNT(DISTINCT CASE WHEN j.city!='' THEN j.city END) AS city_count
                FROM recruitment_programs p
                LEFT JOIN radar_jobs j ON j.program_id=p.id AND j.status='open'
                WHERE {clause} GROUP BY p.id
                ORDER BY p.last_changed_at DESC LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                source_rows = connection.execute(
                    """
                    SELECT ps.source_id, ms.name, ms.source_type, ps.source_url,
                           ps.verification_role, ps.evidence, ps.discovered_at, ps.last_seen_at
                    FROM program_sources ps JOIN monitor_sources ms ON ms.id=ps.source_id
                    WHERE ps.program_id=? ORDER BY ps.verification_role DESC
                    """, (item["id"],)
                ).fetchall()
                item["sources"] = [
                    {**dict(source_row), "evidence": _decode_json(source_row["evidence"], [])}
                    for source_row in source_rows
                ]
                items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def get_program(self, program_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, COUNT(j.id) AS job_count,
                       COUNT(DISTINCT CASE WHEN j.city!='' THEN j.city END) AS city_count
                FROM recruitment_programs p
                LEFT JOIN radar_jobs j ON j.program_id=p.id AND j.status='open'
                WHERE p.id=? OR p.external_id=? GROUP BY p.id
                """,
                (program_id, program_id),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            sources = connection.execute(
                """
                SELECT ps.source_id, ms.name, ms.source_type, ps.source_url,
                       ps.verification_role, ps.evidence, ps.discovered_at, ps.last_seen_at
                FROM program_sources ps JOIN monitor_sources ms ON ms.id=ps.source_id
                WHERE ps.program_id=? ORDER BY ps.verification_role DESC
                """,
                (item["id"],),
            ).fetchall()
            item["sources"] = [
                {**dict(source), "evidence": _decode_json(source["evidence"], [])}
                for source in sources
            ]
        item["jobs"] = self.list_jobs(
            page=1, page_size=100,
            filters={"status": "all", "program_id": item["id"], "active_only": False},
        )["items"]
        return item

    @staticmethod
    def decode_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["before_data"] = _decode_json(item.get("before_data"), None)
        item["after_data"] = _decode_json(item.get("after_data"), None)
        item["changed_fields"] = _decode_json(item.get("changed_fields"), [])
        return item

    @staticmethod
    def public_event(event: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Return a current verified entity signal, never a historical snapshot.

        ``before_data`` may contain discovery-only claims from before an entity was
        verified, while ``after_data`` is an immutable historical snapshot rather
        than the current public record.  The public feed therefore exposes only a
        small whitelist copied from the entity's current verified row.
        """
        entity_type = str(event.get("entity_type") or "")
        fields = (
            PUBLIC_JOB_EVENT_FIELDS
            if entity_type == "job"
            else PUBLIC_PROGRAM_EVENT_FIELDS
        )
        item = {
            key: event.get(key)
            for key in (
                "id", "entity_type", "entity_id", "external_id", "event_type",
                "detected_at", "source_id", "source_name",
            )
        }
        item.update({field: current.get(field) for field in fields})
        return item

    def list_events(self, *, after_event_id: int | None = None, limit: int = 50,
                    event_type: str | None = None,
                    public_verified_only: bool = False) -> dict[str, Any]:
        where = ["1=1"]
        params: list[Any] = []
        direction = "DESC"
        if after_event_id is not None:
            where.append("e.id>?")
            params.append(after_event_id)
            direction = "ASC"
        if event_type:
            where.append("e.event_type=?")
            params.append(event_type)
        if public_verified_only:
            where.append(
                "((e.entity_type='job' AND EXISTS ("
                "SELECT 1 FROM radar_jobs j WHERE j.id=e.entity_id "
                "AND j.verification_status='verified') "
                "AND json_extract(e.after_data, '$.verification_status')='verified') "
                "OR (e.entity_type='program' AND EXISTS ("
                "SELECT 1 FROM recruitment_programs p WHERE p.id=e.entity_id "
                "AND p.verification_status='verified') "
                "AND json_extract(e.after_data, '$.verification_status')='verified'))"
            )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.*, s.name AS source_name
                FROM radar_events e LEFT JOIN monitor_sources s ON s.id=e.source_id
                WHERE {' AND '.join(where)} ORDER BY e.id {direction} LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            latest = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM radar_events").fetchone()[0])
            items = []
            for row in rows:
                event = self.decode_event(row)
                if not public_verified_only:
                    items.append(event)
                    continue
                table = (
                    "radar_jobs"
                    if event["entity_type"] == "job"
                    else "recruitment_programs"
                )
                current = connection.execute(
                    f"SELECT * FROM {table} WHERE id=? AND verification_status='verified'",
                    (event["entity_id"],),
                ).fetchone()
                if current:
                    items.append(self.public_event(event, dict(current)))
        return {"items": items, "last_event_id": latest}

    def dashboard(self) -> dict[str, Any]:
        today = date.today()
        soon = (today + timedelta(days=7)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        active_run_types = self.active_run_types()
        with self._connect() as connection:
            event_counts = {
                row["event_type"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT event_type, COUNT(*) AS count FROM radar_events
                    WHERE detected_at>=? GROUP BY event_type
                    """, (recent,)
                ).fetchall()
            }
            job_counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
                    SUM(CASE WHEN verification_status='verified' THEN 1 ELSE 0 END) AS verified,
                    SUM(CASE WHEN verification_status='pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='open' AND closing_date>? AND closing_date<=? THEN 1 ELSE 0 END) AS closing_soon
                FROM radar_jobs
                """, (today.isoformat(), soon)
            ).fetchone()
            programs = int(connection.execute(
                "SELECT COUNT(*) FROM recruitment_programs WHERE status='open'"
            ).fetchone()[0])
            source_counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,
                    SUM(CASE WHEN enabled=1 AND status='healthy' THEN 1 ELSE 0 END) AS healthy,
                    SUM(CASE WHEN enabled=1 AND status IN ('error','discovery_limited') THEN 1 ELSE 0 END) AS errors
                FROM monitor_sources
                """
            ).fetchone()
            last_run = connection.execute(
                "SELECT * FROM radar_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            last_success = connection.execute(
                "SELECT finished_at FROM radar_runs WHERE status='success' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            latest_event_id = int(connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM radar_events"
            ).fetchone()[0])
            active_run_rows = connection.execute(
                """
                SELECT id, trigger_type, scan_type, started_at, status, source_ids
                FROM radar_runs WHERE status='running' ORDER BY started_at DESC
                """
            ).fetchall()
        active_runs = [
            {
                **dict(row),
                "source_ids": _decode_json(row["source_ids"], []),
            }
            for row in active_run_rows
        ]
        return {
            "counts": {
                "new": event_counts.get("NEW", 0),
                "updated": event_counts.get("UPDATED", 0),
                "closed": event_counts.get("CLOSED", 0),
                "reopened": event_counts.get("REOPENED", 0),
                "programs": programs,
                "closing_soon": int(job_counts["closing_soon"] or 0),
                "pending": int(job_counts["pending"] or 0),
                "verified": int(job_counts["verified"] or 0),
                "open_jobs": int(job_counts["open"] or 0),
                "total_jobs": int(job_counts["total"] or 0),
            },
            "sources": {
                "total": int(source_counts["total"] or 0),
                "enabled": int(source_counts["enabled"] or 0),
                "healthy": int(source_counts["healthy"] or 0),
                "errors": int(source_counts["errors"] or 0),
            },
            "last_scan": self.get_run(last_run["id"]) if last_run else None,
            "last_successful_scan": last_success["finished_at"] if last_success else None,
            "last_event_id": latest_event_id,
            "active_run_types": active_run_types,
            "active_runs": active_runs,
            "run_in_progress": bool(active_run_types),
        }
