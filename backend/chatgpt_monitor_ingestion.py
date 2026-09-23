"""Controlled ChatGPT Monitor ingestion into the existing Future Radar pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .future_radar.normalization import (
    canonicalize_url,
    clean_text,
    normalize_job,
    stable_digest,
)
from .future_radar.service import FutureRadarService, SyncConflict
from .recruitment import SCORING_VERSION, score_job


logger = logging.getLogger(__name__)

_CAMPUS_MARKERS = (
    "2027", "27届", "校园招聘", "校招", "秋招", "春招", "应届",
    "graduate programme", "graduate program", "management trainee", "管培",
)
_ADVICE_MARKERS = ("攻略", "怎么选", "备考", "经验", "面经", "笔经", "交流群")
_ROUNDUP_MARKERS = ("汇总", "合集", "盘点", "多家", "各大", "一览", "秋招爆了")
_STALE_RUN_AFTER = timedelta(minutes=20)


class RetryableIngestionBusy(RuntimeError):
    """The same monitor payload is already being processed and should be retried later."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _thread_ref(value: Any) -> str | None:
    raw = clean_text(value, limit=180)
    if not raw:
        return None
    return "thread-sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _redact_thread_ids(value: Any) -> Any:
    """Keep replayable recruitment data without persisting private chat identifiers."""
    if isinstance(value, dict):
        return {
            key: (_thread_ref(item) if key == "source_thread_id" else _redact_thread_ids(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_thread_ids(item) for item in value]
    return value


class ChatGPTMonitorIngestionService:
    """Validate, audit, filter and hand off to Future Radar's existing sync service."""

    def __init__(self, *, radar: FutureRadarService, connect: Callable[[], Any]):
        self.radar = radar
        self.connect = connect
        self.scope_recorder: Callable[[str, list[dict[str, Any]]], int] | None = None

    def _existing_external_id_for_url(self, url: str | None) -> str | None:
        if not url:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """SELECT external_id FROM radar_jobs
                   WHERE application_url=? OR official_url=?
                   ORDER BY last_seen_at DESC LIMIT 1""",
                (url, url),
            ).fetchone()
        return str(row["external_id"]) if row else None

    def _existing_job(self, external_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self.radar.repository.find_job(connection, external_id)

    def _prepare_job(self, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        item = dict(raw)
        item["title"] = item.get("title") or item.get("job_title") or ""
        item["city"] = item.get("city") or item.get("location") or ""
        item["closing_date"] = item.get("closing_date") or item.get("deadline")
        item["requirements"] = item.get("requirements") or item.get("eligibility") or ""
        item["description"] = item.get("description") or item.get("raw_text") or ""
        item["official_url"] = item.get("official_url") or item.get("source_url")
        item["application_url"] = item.get("application_url") or item.get("official_url")
        event_type = clean_text(item.get("event_type") or "NEW", limit=32).upper()
        if event_type in {"CLOSED", "APPLICATION_DISABLED"}:
            item["status"] = "closed"
        elif event_type == "REOPENED":
            item["status"] = "open"

        # Source ratings are retained in the raw audit row only. They cannot
        # become the authoritative Future Radar tier for this ingestion path.
        provisional = {
            "tier": item.pop("tier", None),
            "score": item.pop("score", None),
            "recommendation": item.pop("recommendation", None),
            "source_rating": item.pop("source_rating", None),
        }
        metadata = dict(item.pop("metadata", {}) or {})
        metadata["provisional_rating"] = provisional
        metadata["source_event_type"] = event_type
        metadata["source_type"] = item.pop("source_type", None)
        metadata["graduation_window"] = item.pop("graduation_window", None)
        metadata["detected_at"] = item.pop("detected_at", None)
        metadata["had_explicit_external_id"] = bool(item.get("external_id"))
        for alias in (
            "job_title", "location", "deadline", "source_url", "raw_text",
            "eligibility", "program_name",
        ):
            item.pop(alias, None)

        candidate_url = canonicalize_url(item.get("application_url") or item.get("official_url"))
        if not item.get("external_id"):
            item["external_id"] = self._existing_external_id_for_url(candidate_url)
        if not item.get("external_id"):
            cohort = metadata.get("graduation_window") or ""
            item["external_id"] = stable_digest(
                item.get("company"), item.get("title"), item.get("city"),
                item.get("program_external_id"), cohort, prefix="monitor-job",
            )
        item["verification_status"] = "pending"
        item["confidence_score"] = min(float(item.get("confidence_score") or 0), 0.7)
        return item, metadata

    def _hard_filter(
        self, item: dict[str, Any], metadata: dict[str, Any], existing: dict[str, Any] | None,
    ) -> tuple[str, str | None]:
        event_type = str(metadata.get("source_event_type") or "NEW")
        text = " ".join(str(item.get(key) or "") for key in (
            "company", "title", "description", "requirements",
        )).casefold()
        if metadata.get("in_scope") is False:
            return "filtered", "outside_configured_scope"
        years = set(re.findall(r"20\d{2}", " ".join((
            text, str(metadata.get("graduation_window") or ""),
        ))))
        if years and "2027" not in years and event_type not in {"CLOSED", "APPLICATION_DISABLED"}:
            return "filtered", "wrong_graduation_cohort"
        if any(marker in text for marker in _ADVICE_MARKERS):
            return "lead", "advice_or_preparation_content"
        if any(marker in text for marker in _ROUNDUP_MARKERS):
            return "lead", "roundup_not_specific_opportunity"
        if not any(marker in text for marker in _CAMPUS_MARKERS):
            return "lead", "campus_scope_needs_verification"
        if item.get("status") == "closed" and existing is None:
            return "lead", "closed_without_existing_job"
        if event_type in {"CLOSED", "APPLICATION_DISABLED", "REOPENED"} and existing is None:
            return "lead", "change_event_without_existing_job"
        if not (
            item.get("official_url") or item.get("application_url")
            or metadata.get("had_explicit_external_id")
        ):
            return "lead", "missing_locatable_job_or_program"
        return "job", None

    def _create_or_update_run(
        self, *, run_id: str, key: str, payload: dict[str, Any], payload_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        now = _utc_now()
        source_id = str(payload["source_id"])
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO monitor_ingestion_runs
                   (id,idempotency_key,source_id,source_thread_ref,monitor_run_id,monitor_name,
                    generated_at,received_at,payload_hash,raw_payload,processing_status,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'received',?,?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (
                    run_id, key, source_id, _thread_ref(payload.get("source_thread_id")),
                    payload.get("monitor_run_id"), payload.get("monitor_name"),
                    str(payload.get("generated_at") or "") or None, now, payload_hash,
                    _json(_redact_thread_ids(payload)), now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM monitor_ingestion_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
        result = dict(row)
        if result["payload_hash"] != payload_hash:
            raise SyncConflict("Idempotency key was already used for a different payload.")
        return result, result["id"] == run_id

    def _save_item(
        self, *, run_id: str, item_key: str, fingerprint: str, raw: dict[str, Any],
    ) -> str:
        item_id = str(uuid.uuid4())
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO monitor_ingestion_items
                   (id,run_id,item_key,fingerprint,raw_item,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,item_key) DO NOTHING""",
                (item_id, run_id, item_key, fingerprint, _json(_redact_thread_ids(raw)), now, now),
            )
            row = connection.execute(
                "SELECT id FROM monitor_ingestion_items WHERE run_id=? AND item_key=?",
                (run_id, item_key),
            ).fetchone()
        return str(row["id"])

    def _save_lead(
        self, *, source_id: str, item_key: str, item: dict[str, Any],
        metadata: dict[str, Any], reason: str,
    ) -> str:
        lead_id = stable_digest(source_id, item_key, prefix="monitor-lead", length=32)
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO recruitment_monitor_leads
                   (id,source_id,source_item_key,company,title,source_url,status,reason,
                    metadata,created_at,updated_at)
                   VALUES (?,?,?,?,?,?, 'needs_verification',?,?,?,?)
                   ON CONFLICT(source_id,source_item_key) DO UPDATE SET
                     company=excluded.company,title=excluded.title,
                     source_url=excluded.source_url,reason=excluded.reason,
                     metadata=excluded.metadata,updated_at=excluded.updated_at""",
                (
                    lead_id, source_id, item_key, item.get("company") or "",
                    item.get("title") or "", item.get("application_url") or item.get("official_url"),
                    reason, _json(metadata), now, now,
                ),
            )
        return lead_id

    def _finish_item(
        self, item_id: str, *, status: str, error: str | None = None,
        job: dict[str, Any] | None = None, lead_id: str | None = None,
        score: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        score = score or {}
        reasons = [*(score.get("positive_reasons") or []), *(score.get("negative_reasons") or [])]
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_ingestion_items SET processing_status=?,processing_error=?,
                   result_job_id=?,result_lead_id=?,final_tier=?,tier_score=?,tier_reasons=?,
                   evaluated_at=?,rules_version=?,updated_at=? WHERE id=?""",
                (
                    status, error, job.get("id") if job else None, lead_id,
                    score.get("system_tier_code", score.get("tier_code")),
                    score.get("system_job_score", score.get("job_score")), _json(reasons[:8]),
                    now if score else None, score.get("scoring_version") if score else None,
                    now, item_id,
                ),
            )

    def _finish_run(self, run_id: str, *, status: str, result: dict[str, Any], error: str | None, started: float) -> None:
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_ingestion_runs SET processing_status=?,processing_error=?,
                   result=?,processing_time_ms=?,updated_at=? WHERE id=?""",
                (status, error, _json(result), round((time.perf_counter() - started) * 1000), now, run_id),
            )

    def _run_is_stale(self, run: dict[str, Any]) -> bool:
        updated = _parse_time(run.get("updated_at"))
        if updated is None:
            return True
        return datetime.now(timezone.utc) - updated > _STALE_RUN_AFTER

    def _latest_event_id(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id),0) AS cursor FROM radar_events").fetchone()
        return int(row["cursor"] if row else 0)

    def _update_watermark(
        self, *, payload: dict[str, Any], run_id: str, response: dict[str, Any], received_at: str,
    ) -> None:
        now = _utc_now()
        source_id = str(payload["source_id"])
        duplicate_suppressed = int(response.get("duplicates") or 0)
        with self.connect() as connection:
            prior = connection.execute(
                "SELECT pending_backfill,interrupted_from FROM monitor_ingestion_watermarks WHERE source_id=?",
                (source_id,),
            ).fetchone()
            preserve_backfill = bool(prior and prior["pending_backfill"])
            interrupted_from = prior["interrupted_from"] if preserve_backfill else None
            event_row = connection.execute(
                "SELECT COALESCE(MAX(id),0) AS cursor FROM radar_events"
            ).fetchone()
            event_id = int(event_row["cursor"] if event_row else 0)
            connection.execute(
                """INSERT INTO monitor_ingestion_watermarks
                   (source_id,source_thread_ref,monitor_name,last_successful_ingestion_at,
                    last_successful_run_id,last_successful_event_id,last_received_at,
                    recovery_status,interrupted_from,interrupted_until,pending_backfill,
                    duplicate_suppressed,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     source_thread_ref=excluded.source_thread_ref,
                     monitor_name=excluded.monitor_name,
                     last_successful_ingestion_at=excluded.last_successful_ingestion_at,
                     last_successful_run_id=excluded.last_successful_run_id,
                     last_successful_event_id=excluded.last_successful_event_id,
                     last_received_at=excluded.last_received_at,
                     recovery_status=excluded.recovery_status,
                     interrupted_from=excluded.interrupted_from,
                     interrupted_until=excluded.interrupted_until,
                     pending_backfill=excluded.pending_backfill,
                     duplicate_suppressed=monitor_ingestion_watermarks.duplicate_suppressed + excluded.duplicate_suppressed,
                     updated_at=excluded.updated_at""",
                (
                    source_id, _thread_ref(payload.get("source_thread_id")), payload.get("monitor_name"),
                    payload.get("generated_at") or now, run_id, event_id, received_at,
                    "backfill_pending" if preserve_backfill else "normal",
                    interrupted_from, now if preserve_backfill else None,
                    int(preserve_backfill), duplicate_suppressed, now,
                ),
            )

    def _mark_interruption(self, *, payload: dict[str, Any], run_id: str | None, error: str) -> None:
        now = _utc_now()
        source_id = str(payload.get("source_id") or "")
        if not source_id:
            return
        if not self.radar.repository.get_source(source_id):
            self.radar.repository.create_source({
                "id": source_id,
                "name": clean_text(payload.get("monitor_name") or source_id, limit=160),
                "platform": "external",
                "source_type": "manual",
                "enabled": True,
                "priority": 50,
                "trust_level": "discovery",
                "interval_minutes": 1_440,
                "adapter_config": {"adapter": "manual"},
                "status": "pending",
                "verification_status": "unverified",
            })
        with self.connect() as connection:
            prior = connection.execute(
                "SELECT last_successful_ingestion_at, interrupted_from FROM monitor_ingestion_watermarks WHERE source_id=?",
                (source_id,),
            ).fetchone()
            interrupted_from = None
            if prior:
                interrupted_from = prior["interrupted_from"] or prior["last_successful_ingestion_at"]
            interrupted_from = interrupted_from or payload.get("generated_at") or now
            connection.execute(
                """INSERT INTO monitor_ingestion_watermarks
                   (source_id,source_thread_ref,monitor_name,last_successful_ingestion_at,
                    last_successful_run_id,last_successful_event_id,last_received_at,
                    recovery_status,interrupted_from,interrupted_until,pending_backfill,
                    duplicate_suppressed,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     source_thread_ref=COALESCE(excluded.source_thread_ref, monitor_ingestion_watermarks.source_thread_ref),
                     monitor_name=COALESCE(excluded.monitor_name, monitor_ingestion_watermarks.monitor_name),
                     recovery_status='interrupted',
                     interrupted_from=COALESCE(monitor_ingestion_watermarks.interrupted_from, excluded.interrupted_from),
                     interrupted_until=excluded.interrupted_until,
                     pending_backfill=1,
                     last_received_at=excluded.last_received_at,
                     updated_at=excluded.updated_at""",
                (
                    source_id, _thread_ref(payload.get("source_thread_id")), payload.get("monitor_name"),
                    None, run_id, 0, now, "interrupted", interrupted_from, now, 1, 0, now,
                ),
            )

    def recovery_status(self) -> dict[str, Any]:
        with self.connect() as connection:
            summary_rows = connection.execute(
                """SELECT processing_status, COUNT(*) AS count
                   FROM monitor_ingestion_runs GROUP BY processing_status"""
            ).fetchall()
            item_rows = connection.execute(
                """SELECT processing_status, COUNT(*) AS count
                   FROM monitor_ingestion_items GROUP BY processing_status"""
            ).fetchall()
            watermarks = [dict(row) for row in connection.execute(
                """SELECT * FROM monitor_ingestion_watermarks
                   ORDER BY updated_at DESC LIMIT 100"""
            ).fetchall()]
            latest_event = connection.execute(
                "SELECT COALESCE(MAX(id),0) AS cursor FROM radar_events"
            ).fetchone()
        return {
            "runs": {str(row["processing_status"]): int(row["count"]) for row in summary_rows},
            "items": {str(row["processing_status"]): int(row["count"]) for row in item_rows},
            "watermarks": watermarks,
            "latest_radar_event_id": int(latest_event["cursor"] if latest_event else 0),
        }

    def ingest(self, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        canonical = _json(payload)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        source_id = clean_text(payload.get("source_id"), limit=64)
        run_key = (
            clean_text(idempotency_key, limit=240)
            or clean_text(payload.get("monitor_run_id"), limit=180)
            or clean_text(payload.get("batch_id"), limit=180)
            or payload_hash
        )
        key = f"chatgpt-monitor:{source_id}:{run_key}"
        proposed_run_id = str(uuid.uuid4())
        run, created = self._create_or_update_run(
            run_id=proposed_run_id, key=key, payload=payload, payload_hash=payload_hash,
        )
        if not created:
            previous = json.loads(run.get("result") or "{}")
            if previous and run.get("processing_status") in {"success", "partial"}:
                return {**previous, "idempotent_replay": True}
            if run.get("processing_status") in {"received", "processing"} and not self._run_is_stale(run):
                raise RetryableIngestionBusy("same bridge payload is still processing; retry later")
            # A stale or failed run with no durable result is safe to replay because
            # monitor_ingestion_items, radar_sync_batches and radar_jobs are all
            # protected by stable keys/idempotency constraints.
            run_id = str(run["id"])
        else:
            run_id = proposed_run_id
        received_at = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_ingestion_runs SET processing_status='processing',processing_error=NULL,
                   updated_at=? WHERE id=?""",
                (received_at, run_id),
            )

        jobs_for_sync: list[dict[str, Any]] = []
        item_rows: dict[str, str] = {}
        leads_created = 0
        filtered = 0
        errors: list[dict[str, str]] = []
        try:
            for index, raw in enumerate(payload.get("jobs") or []):
                raw = dict(raw)
                item_key = clean_text(raw.get("external_id"), limit=180) or stable_digest(
                    source_id, index, raw.get("company"), raw.get("title") or raw.get("job_title"),
                    prefix="monitor-item",
                )
                fingerprint = hashlib.sha256(_json(raw).encode("utf-8")).hexdigest()
                item_id = self._save_item(
                    run_id=run_id, item_key=item_key, fingerprint=fingerprint, raw=raw,
                )
                try:
                    item, metadata = self._prepare_job(raw)
                    existing = self._existing_job(str(item["external_id"]))
                    decision, reason = self._hard_filter(item, metadata, existing)
                    if decision != "job":
                        filtered += 1
                        lead_id = None
                        if decision == "lead":
                            lead_id = self._save_lead(
                                source_id=source_id, item_key=item_key, item=item,
                                metadata=metadata, reason=reason or "needs_verification",
                            )
                            leads_created += 1
                        self._finish_item(
                            item_id, status=decision, error=reason, lead_id=lead_id,
                        )
                        continue
                    normalized = normalize_job(item)
                    jobs_for_sync.append(item)
                    item_rows[normalized["external_id"]] = item_id
                except Exception as exc:
                    filtered += 1
                    error = clean_text(exc, limit=240) or type(exc).__name__
                    errors.append({"item_key": item_key, "error": error})
                    self._finish_item(item_id, status="failed", error=error)

            sync_payload = {
                "version": "FROSTFIRE_SYNC_V1",
                "batch_id": key,
                "source_id": source_id,
                "source_name": payload.get("monitor_name") or payload.get("source") or payload.get("source_name"),
                "observed_at": payload.get("generated_at") or payload.get("observed_at"),
                "snapshot_complete": bool(payload.get("snapshot_complete", False)),
                "programs": list(payload.get("programs") or []),
                "jobs": jobs_for_sync,
                "articles": list(payload.get("articles") or []),
            }
            radar_result = self.radar.sync(sync_payload, idempotency_key=key)
            if self.scope_recorder and jobs_for_sync:
                # This records only exact company/public-host overlaps already
                # observed in a successful monitor run. It never broadens the
                # monitor to unrelated Future Radar sources.
                self.scope_recorder(source_id, jobs_for_sync)
            counts = radar_result.get("counts") or {}
            tier_counts: dict[str, int] = {}
            for external_id, item_id in item_rows.items():
                job = self._existing_job(external_id)
                if not job:
                    self._finish_item(item_id, status="failed", error="job_not_persisted")
                    errors.append({"item_key": external_id, "error": "job_not_persisted"})
                    continue
                scored = score_job(job, {})
                tier = scored.get("system_tier_code", scored.get("tier_code"))
                if tier:
                    tier_counts[str(tier)] = tier_counts.get(str(tier), 0) + 1
                self._finish_item(item_id, status="job", job=job, score=scored)

            response = {
                "run_id": run_id,
                "status": "success" if not errors else "partial",
                "received": len(payload.get("jobs") or []),
                "new": int(counts.get("new_jobs") or 0),
                "updated": int(counts.get("updated_jobs") or 0),
                "duplicates": int(counts.get("unchanged_jobs") or 0),
                "filtered": filtered,
                "leads_created": leads_created,
                "jobs_created": int(counts.get("new_jobs") or 0),
                "jobs_updated": int(counts.get("updated_jobs") or 0),
                "closed": int(counts.get("closed_jobs") or 0),
                "reopened": int(counts.get("reopened_jobs") or 0),
                "tier_counts": tier_counts,
                "rules_version": SCORING_VERSION,
                "errors": errors,
                "idempotent_replay": False,
            }
            self._finish_run(
                run_id, status=response["status"], result=response,
                error=None if not errors else f"{len(errors)} item(s) failed", started=started,
            )
            self._update_watermark(
                payload=payload, run_id=run_id, response=response, received_at=received_at,
            )
            logger.info(
                "chatgpt_monitor_sync monitor_run_id=%s source_thread_ref=%s received=%s "
                "created=%s updated=%s duplicate=%s filtered=%s failed=%s processing_time_ms=%s",
                payload.get("monitor_run_id"), _thread_ref(payload.get("source_thread_id")),
                response["received"], response["jobs_created"], response["jobs_updated"],
                response["duplicates"], response["filtered"], len(errors),
                round((time.perf_counter() - started) * 1000),
            )
            return response
        except Exception as exc:
            error = clean_text(exc, limit=300) or type(exc).__name__
            try:
                self._finish_run(run_id, status="failed", result={}, error=error, started=started)
                self._mark_interruption(payload=payload, run_id=run_id, error=error)
            except Exception:
                logger.exception("failed to record monitor ingestion interruption")
            raise
