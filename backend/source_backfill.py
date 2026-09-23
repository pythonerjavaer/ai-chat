"""Automatic, bounded recovery scans for public official/ATS sources.

Private ChatGPT results cannot be replayed here.  The coordinator learns only
the public source overlap already observed for each monitor and rechecks that
bounded scope after durable ingestion has recovered.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from .future_radar.normalization import clean_text
from .future_radar.service import FutureRadarService, RadarRunBusy


logger = logging.getLogger(__name__)
BACKFILL_INTERVAL_SECONDS = 15 * 60
BRIDGE_SETTLE_MINUTES = 20
BACKFILL_LOCK_TTL_SECONDS = 12 * 60
MAX_JOBS_PER_PASS = 4
_PUBLIC_SOURCE_TYPES = frozenset({"official_html", "official_api", "ats", "rss", "atom"})
_BLOCKED_ADAPTERS = frozenset({
    "manual", "legacy_database", "discovery_limited", "openai_web_search",
    "wechat_web_search", "wechat_public", "sogou_wechat",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _host(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _adapter(source: dict[str, Any]) -> str:
    return str((source.get("adapter_config") or {}).get("adapter") or source.get("source_type") or "").casefold()


def is_replayable_public_source(source: dict[str, Any]) -> bool:
    """Only deterministic public sources; no private/paid/search providers."""
    return bool(
        source.get("enabled")
        and source.get("source_type") in _PUBLIC_SOURCE_TYPES
        and _adapter(source) not in _BLOCKED_ADAPTERS
        and (source.get("url") or "").startswith("https://")
    )


class SourceBackfillCoordinator:
    def __init__(
        self,
        *,
        connect: Callable[[], Any],
        radar: FutureRadarService,
        bridge_settle_minutes: int = BRIDGE_SETTLE_MINUTES,
    ):
        self.connect = connect
        self.radar = radar
        self.bridge_settle_minutes = max(0, int(bridge_settle_minutes))
        # A newly started process may itself be the first sign that the
        # database recovered.  Give the existing five-minute Sheet trigger a
        # full settlement window before independent public-source recovery.
        self._healthy_since: datetime | None = None

    def database_healthy(self) -> bool:
        try:
            with self.connect() as connection:
                healthy = int(connection.execute("SELECT 1 AS healthy").fetchone()["healthy"]) == 1
            if healthy and self._healthy_since is None:
                self._healthy_since = _now()
            return healthy
        except Exception:
            # PostgreSQL/Supabase drivers do not share one database exception.
            # A failed health probe is expected during an outage and should
            # reset the recovery quiet-period clock without killing the app.
            self._healthy_since = None
            return False

    def record_scope(self, monitor_source_id: str, jobs: list[dict[str, Any]]) -> int:
        """Learn an exact public overlap from already-ingested monitor evidence."""
        companies = {clean_text(job.get("company"), limit=160).casefold() for job in jobs}
        companies.discard("")
        hosts = {_host(job.get("official_url") or job.get("application_url")) for job in jobs}
        hosts.discard("")
        now = _iso()
        matched: dict[str, str] = {}
        for source in self.radar.repository.list_sources(enabled=True):
            if not is_replayable_public_source(source):
                continue
            source_company = clean_text(source.get("company"), limit=160).casefold()
            source_host = _host(source.get("url"))
            if source_company and source_company in companies:
                matched[source["id"]] = "exact_company"
            elif source_host and source_host in hosts:
                matched[source["id"]] = "exact_official_host"
        if not matched:
            return 0
        with self.connect() as connection:
            for public_source_id, reason in matched.items():
                connection.execute(
                    """INSERT INTO monitor_backfill_source_scopes
                       (monitor_source_id,public_source_id,match_reason,first_observed_at,last_observed_at)
                       VALUES(?,?,?,?,?) ON CONFLICT(monitor_source_id,public_source_id) DO UPDATE SET
                       match_reason=excluded.match_reason,last_observed_at=excluded.last_observed_at""",
                    (monitor_source_id, public_source_id, reason, now, now),
                )
        return len(matched)

    def rebuild_known_scopes(self, monitor_source_id: str | None = None) -> int:
        """Backfill scope mappings from durable job provenance after deployment."""
        where, params = "js.source_id LIKE 'chatgpt-radar-%'", []
        if monitor_source_id:
            where += " AND js.source_id=?"
            params.append(monitor_source_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT js.source_id,j.company,j.official_url,j.application_url
                     FROM job_sources js JOIN radar_jobs j ON j.id=js.job_id
                     WHERE {where} AND js.active=1""", params,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["source_id"]), []).append(dict(row))
        return sum(self.record_scope(source_id, jobs) for source_id, jobs in grouped.items())

    def bridge_backlog_settled(self) -> bool:
        """Wait for Apps Script retries to arrive before starting public recovery.

        Sheet-only PENDING rows are not readable by the service.  The existing
        five-minute Apps Script trigger owns them; this service waits a full
        twenty-minute quiet period and also refuses to overlap a received or
        processing backend run.
        """
        settle_delta = timedelta(minutes=self.bridge_settle_minutes)
        if self._healthy_since is None or _now() - self._healthy_since < settle_delta:
            return False
        quiet_before = _iso(_now() - settle_delta)
        with self.connect() as connection:
            active = connection.execute(
                """SELECT COUNT(*) AS n FROM monitor_ingestion_runs
                   WHERE processing_status IN ('received','processing')"""
            ).fetchone()["n"]
            recent = connection.execute(
                """SELECT COUNT(*) AS n FROM monitor_ingestion_runs
                   WHERE received_at>?""", (quiet_before,),
            ).fetchone()["n"]
        return not int(active) and not int(recent)

    def schedule_pending_windows(self) -> dict[str, int]:
        now = _iso()
        with self.connect() as connection:
            pending = connection.execute(
                """SELECT source_id,interrupted_from,interrupted_until,last_received_at
                   FROM monitor_ingestion_watermarks WHERE pending_backfill=1
                   ORDER BY updated_at LIMIT 100"""
            ).fetchall()
        jobs_created = 0
        unsupported = 0
        for watermark in pending:
            source_id = str(watermark["source_id"])
            self.rebuild_known_scopes(source_id)
            with self.connect() as connection:
                scopes = connection.execute(
                    """SELECT s.public_source_id FROM monitor_backfill_source_scopes s
                       JOIN monitor_sources ms ON ms.id=s.public_source_id
                       WHERE s.monitor_source_id=? AND ms.enabled=1""", (source_id,),
                ).fetchall()
            if not scopes:
                unsupported += 1
                with self.connect() as connection:
                    connection.execute(
                        """UPDATE monitor_ingestion_watermarks SET recovery_status='private_replay_unavailable',
                           updated_at=? WHERE source_id=? AND pending_backfill=1""", (now, source_id),
                    )
                continue
            window_start = str(watermark["interrupted_from"] or watermark["last_received_at"] or now)
            window_end = str(watermark["interrupted_until"] or now)
            with self.connect() as connection:
                for scope in scopes:
                    cursor = connection.execute(
                        """INSERT INTO source_backfill_jobs
                           (id,monitor_source_id,public_source_id,window_start,window_end,status,
                            attempt_count,next_attempt_at,result,created_at,updated_at)
                           VALUES(?,?,?,?,?,'queued',0,?,'{}',?,?)
                           ON CONFLICT(monitor_source_id,public_source_id,window_start,window_end) DO NOTHING""",
                        (str(uuid.uuid4()), source_id, scope["public_source_id"], window_start,
                         window_end, now, now, now),
                    )
                    jobs_created += max(0, int(cursor.rowcount or 0))
                connection.execute(
                    """UPDATE monitor_ingestion_watermarks SET recovery_status='backfill_queued',updated_at=?
                       WHERE source_id=? AND pending_backfill=1""", (now, source_id),
                )
        return {"created": jobs_created, "unsupported": unsupported}

    def _retry(self, job: dict[str, Any], reason: str) -> None:
        attempts = int(job["attempt_count"]) + 1
        minutes = min(360, 15 * (2 ** min(attempts - 1, 5)))
        next_attempt = _iso(_now() + timedelta(minutes=minutes))
        with self.connect() as connection:
            connection.execute(
                """UPDATE source_backfill_jobs SET status='retryable',attempt_count=?,next_attempt_at=?,
                   last_error=?,updated_at=? WHERE id=?""",
                (attempts, next_attempt, clean_text(reason, limit=240), _iso(), job["id"]),
            )
            connection.execute(
                """UPDATE monitor_ingestion_watermarks SET recovery_status='backfill_retryable',updated_at=?
                   WHERE source_id=?""", (_iso(), job["monitor_source_id"]),
            )

    def _reconcile_monitor(self, monitor_source_id: str, window_start: str, window_end: str) -> None:
        """Clear only when every job in the same interruption window succeeded."""
        with self.connect() as connection:
            pending = connection.execute(
                """SELECT COUNT(*) AS n FROM source_backfill_jobs WHERE monitor_source_id=?
                   AND window_start=? AND window_end=? AND status!='success'""",
                (monitor_source_id, window_start, window_end),
            ).fetchone()["n"]
            if int(pending):
                return
            event_row = connection.execute("SELECT COALESCE(MAX(id),0) AS cursor FROM radar_events").fetchone()
            connection.execute(
                """UPDATE monitor_ingestion_watermarks SET pending_backfill=0,recovery_status='normal',
                   interrupted_from=NULL,interrupted_until=NULL,last_successful_event_id=?,updated_at=?
                   WHERE source_id=? AND interrupted_from=? AND interrupted_until=?""",
                (int(event_row["cursor"]), _iso(), monitor_source_id, window_start, window_end),
            )

    def interrupt_stale_jobs(self) -> int:
        stale_before = _iso(_now() - timedelta(minutes=30))
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE source_backfill_jobs SET status='retryable',next_attempt_at=?,
                   last_error='Backfill worker was interrupted before completion.',updated_at=?
                   WHERE status='running' AND updated_at<?""", (_iso(), _iso(), stale_before),
            )
        return max(0, int(cursor.rowcount or 0))

    def run_due_jobs(self) -> dict[str, int]:
        now = _iso()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM source_backfill_jobs WHERE status IN ('queued','retryable')
                   AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   ORDER BY created_at LIMIT ?""", (now, MAX_JOBS_PER_PASS),
            ).fetchall()
        succeeded = failed = 0
        for raw in rows:
            job = dict(raw)
            source = self.radar.repository.get_source(job["public_source_id"])
            if not source or not is_replayable_public_source(source):
                self._retry(job, "Public source is disabled or no longer replayable.")
                failed += 1
                continue
            with self.connect() as connection:
                connection.execute(
                    "UPDATE source_backfill_jobs SET status='running',started_at=?,updated_at=? WHERE id=?",
                    (now, now, job["id"]),
                )
            try:
                result = self.radar.run(
                    trigger_type="recovery_backfill", source_ids=[job["public_source_id"]],
                    scan_type="scheduled", force=True,
                )
                if result.get("status") != "success" or int(result.get("sources_failed") or 0):
                    errors = result.get("errors") or []
                    raise RuntimeError((errors[0].get("message") if errors else None) or "Public source scan was incomplete.")
                with self.connect() as connection:
                    connection.execute(
                        """UPDATE source_backfill_jobs SET status='success',attempt_count=attempt_count+1,
                           radar_run_id=?,result=?,last_error=NULL,finished_at=?,updated_at=? WHERE id=?""",
                        (result.get("id"), json.dumps({
                            key: result.get(key) for key in (
                                "new_jobs", "updated_jobs", "closed_jobs", "reopened_jobs", "unchanged_jobs",
                            )
                        }, ensure_ascii=False), _iso(), _iso(), job["id"]),
                    )
                self._reconcile_monitor(
                    job["monitor_source_id"], job["window_start"], job["window_end"],
                )
                succeeded += 1
            except RadarRunBusy:
                self._retry(job, "Future Radar scan mutex is busy.")
                failed += 1
            except Exception as exc:
                logger.warning(
                    "Source backfill retry monitor=%s public_source=%s error_type=%s",
                    job["monitor_source_id"], job["public_source_id"], type(exc).__name__,
                )
                self._retry(job, "Public source was temporarily unavailable.")
                failed += 1
        return {"attempted": len(rows), "succeeded": succeeded, "retryable": failed}

    def run_once(self) -> dict[str, Any]:
        if not self.database_healthy():
            return {"status": "database_unavailable"}
        if not self.bridge_backlog_settled():
            return {"status": "waiting_for_bridge_backlog"}
        owner = str(uuid.uuid4())
        lock = "source-backfill-orchestrator"
        if not self.radar.repository.acquire_lock(lock, owner, ttl_seconds=BACKFILL_LOCK_TTL_SECONDS):
            return {"status": "already_running"}
        try:
            self.interrupt_stale_jobs()
            scheduled = self.schedule_pending_windows()
            processed = self.run_due_jobs()
            return {"status": "success", "scheduled": scheduled, "processed": processed}
        finally:
            self.radar.repository.release_lock(lock, owner)

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            jobs = connection.execute(
                """SELECT status,COUNT(*) AS count FROM source_backfill_jobs GROUP BY status"""
            ).fetchall()
            pending = connection.execute(
                """SELECT source_id,recovery_status,interrupted_from,interrupted_until,pending_backfill
                   FROM monitor_ingestion_watermarks WHERE pending_backfill=1 ORDER BY updated_at DESC LIMIT 100"""
            ).fetchall()
        return {"jobs": {str(x["status"]): int(x["count"]) for x in jobs},
                "pending_windows": [dict(x) for x in pending],
                "automatic_interval_seconds": BACKFILL_INTERVAL_SECONDS,
                "bridge_settle_minutes": self.bridge_settle_minutes,
                "database_healthy_since": _iso(self._healthy_since) if self._healthy_since else None,
                "private_monitor_replay": False}
