"""Future Radar orchestration, diffing, verification and idempotent sync."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from .adapters import (
    AdapterResult,
    DiscoveryLimitedError,
    SourceAdapter,
    adapter_for_source,
)
from .normalization import (
    SEMANTIC_JOB_FIELDS,
    SEMANTIC_PROGRAM_FIELDS,
    canonicalize_url,
    changed_fields,
    clean_text,
    normalize_job,
    normalize_program,
    semantic_hash,
    stable_digest,
)
from .repository import RadarRepository, utc_now
from .seeds import initial_sources


logger = logging.getLogger(__name__)


class RadarRunBusy(RuntimeError):
    pass


class SyncConflict(ValueError):
    pass


class FutureRadarService:
    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        openai_api_key: str,
        ai_model: str,
        web_search_enabled: bool,
        close_confirmations: int = 2,
        max_workers: int = 4,
        adapter_factory: Callable[[dict[str, Any]], SourceAdapter] | None = None,
    ) -> None:
        self.repository = RadarRepository(connect)
        self.openai_api_key = openai_api_key
        self.ai_model = ai_model
        self.web_search_enabled = web_search_enabled
        self.close_confirmations = max(2, min(10, int(close_confirmations)))
        self.max_workers = max(1, min(8, int(max_workers)))
        self.adapter_factory = adapter_factory

    def seed_registry(self) -> None:
        self.repository.seed_sources(
            initial_sources(web_search_enabled=self.web_search_enabled)
        )

    def _adapter(self, source: dict[str, Any]) -> SourceAdapter:
        if self.adapter_factory:
            return self.adapter_factory(source)
        return adapter_for_source(
            source,
            repository=self.repository,
            openai_api_key=self.openai_api_key,
            ai_model=self.ai_model,
        )

    @staticmethod
    def _empty_summary(status: str = "running") -> dict[str, Any]:
        return {
            "status": status,
            "sources_checked": 0,
            "sources_succeeded": 0,
            "sources_failed": 0,
            "programs_discovered": 0,
            "new_jobs": 0,
            "updated_jobs": 0,
            "closed_jobs": 0,
            "reopened_jobs": 0,
            "unchanged_jobs": 0,
            "articles_discovered": 0,
            "ai_calls": 0,
            "model_tokens_used": 0,
            "errors": [],
        }

    def run(
        self,
        *,
        trigger_type: str = "scheduled",
        source_ids: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        owner = str(uuid.uuid4())
        if not self.repository.acquire_lock("future-radar-run", owner, ttl_seconds=30 * 60):
            raise RadarRunBusy("A Future Radar run is already active.")
        try:
            sources = (
                [source for source_id in source_ids or []
                 if (source := self.repository.get_source(source_id)) and (source["enabled"] or force)]
                if source_ids
                else self.repository.due_sources()
            )
            run = self.repository.create_run(trigger_type, [source["id"] for source in sources])
            summary = self._empty_summary()
            if not sources:
                summary["status"] = "success"
                return self.repository.finish_run(run["id"], summary)

            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(sources))) as executor:
                futures = {
                    executor.submit(self._scan_source, source, run["id"]): source
                    for source in sources
                }
                for future in as_completed(futures):
                    source = futures[future]
                    summary["sources_checked"] += 1
                    try:
                        result = future.result()
                    except DiscoveryLimitedError as exc:
                        self.repository.update_source_limited(source["id"], str(exc))
                        summary["sources_failed"] += 1
                        summary["errors"].append({
                            "source_id": source["id"],
                            "code": "DISCOVERY_LIMITED",
                            "message": str(exc),
                        })
                        continue
                    except Exception as exc:
                        logger.exception(
                            "Future Radar source failed run_id=%s source_id=%s adapter=%s",
                            run["id"],
                            source["id"],
                            source.get("adapter_config", {}).get("adapter") or source.get("source_type"),
                        )
                        self.repository.update_source_error(source["id"], str(exc))
                        summary["sources_failed"] += 1
                        summary["errors"].append({
                            "source_id": source["id"],
                            "code": "SOURCE_FAILED",
                            "message": clean_text(str(exc), limit=300),
                        })
                        continue
                    summary["sources_succeeded"] += 1
                    for key in (
                        "programs_discovered", "new_jobs", "updated_jobs", "closed_jobs",
                        "reopened_jobs", "unchanged_jobs", "articles_discovered", "ai_calls",
                        "model_tokens_used",
                    ):
                        summary[key] += int(result.get(key, 0))
                    summary["errors"].extend(result.get("errors", []))

            if summary["sources_succeeded"] and summary["sources_failed"]:
                summary["status"] = "partial_success"
            elif summary["sources_failed"]:
                summary["status"] = "failed"
            else:
                summary["status"] = "success"
            return self.repository.finish_run(run["id"], summary)
        finally:
            self.repository.release_lock("future-radar-run", owner)

    def _scan_source(self, source: dict[str, Any], run_id: str) -> dict[str, Any]:
        started = time.monotonic()
        owner = str(uuid.uuid4())
        lock_name = f"future-radar-source:{source['id']}"
        if not self.repository.acquire_lock(lock_name, owner, ttl_seconds=20 * 60):
            raise RadarRunBusy(f"Source {source['id']} is already running.")
        try:
            result = self._adapter(source).scan(source)
            processed = self.process_result(source=source, result=result, run_id=run_id)
            if result.normalized_content and result.content_hash:
                self.repository.save_snapshot(
                    source["id"], result.content_hash, result.normalized_content,
                    {"status": result.status, "jobs": len(result.jobs), "programs": len(result.programs)},
                )
            self.repository.update_source_success(
                source["id"], content_hash=result.content_hash,
                status="healthy" if result.status in {"healthy", "idle"} else result.status,
            )
            logger.info(
                "Future Radar source completed run_id=%s source_id=%s adapter=%s "
                "duration_ms=%d programs=%d jobs=%d articles=%d status=%s",
                run_id,
                source["id"],
                source.get("adapter_config", {}).get("adapter") or source.get("source_type"),
                int((time.monotonic() - started) * 1_000),
                len(result.programs),
                len(result.jobs),
                len(result.articles),
                result.status,
            )
            return processed
        finally:
            self.repository.release_lock(lock_name, owner)

    def process_result(
        self,
        *,
        source: dict[str, Any],
        result: AdapterResult,
        run_id: str,
    ) -> dict[str, Any]:
        counts = self._empty_summary(status="success")
        counts["ai_calls"] = result.ai_calls
        counts["model_tokens_used"] = result.model_tokens_used
        now = utc_now()

        # An unchanged semantic page is a successful observation, not a new
        # snapshot.  Touch linked entities so no absence counter is advanced.
        if result.content_hash and result.content_hash == source.get("last_content_hash"):
            with self.repository.transaction() as connection:
                job_rows = connection.execute(
                    "SELECT job_id FROM job_sources WHERE source_id=? AND active=1",
                    (source["id"],),
                ).fetchall()
                for row in job_rows:
                    self.repository.touch_job(connection, str(row["job_id"]), now)
                    connection.execute(
                        "UPDATE job_sources SET last_seen_at=?, missing_successes=0 WHERE job_id=? AND source_id=?",
                        (now, row["job_id"], source["id"]),
                    )
                program_rows = connection.execute(
                    "SELECT program_id FROM program_sources WHERE source_id=? AND active=1",
                    (source["id"],),
                ).fetchall()
                for row in program_rows:
                    self.repository.touch_program(connection, str(row["program_id"]), now)
                    connection.execute(
                        "UPDATE program_sources SET last_seen_at=?, missing_successes=0 WHERE program_id=? AND source_id=?",
                        (now, row["program_id"], source["id"]),
                    )
            counts["unchanged_jobs"] = len(job_rows)
            return counts

        verification_role = self._verification_role(source)
        program_ids_by_external: dict[str, str] = {}
        seen_program_ids: set[str] = set()
        seen_job_ids: set[str] = set()
        with self.repository.transaction() as connection:
            for raw in result.programs:
                try:
                    item = normalize_program(raw)
                    if verification_role == "verification" and item.get("official_url"):
                        item["verification_status"] = "verified"
                        item["confidence_score"] = max(item["confidence_score"], 0.9)
                        item["content_hash"] = semantic_hash(item, SEMANTIC_PROGRAM_FIELDS)
                    elif item["verification_status"] == "verified":
                        item["verification_status"] = "pending"
                        item["confidence_score"] = min(item["confidence_score"], 0.7)
                        item["content_hash"] = semantic_hash(item, SEMANTIC_PROGRAM_FIELDS)
                    program, event = self._upsert_program(
                        connection, item=item, source=source, verification_role=verification_role,
                        run_id=run_id, now=now,
                    )
                    program_ids_by_external[item["external_id"]] = program["id"]
                    seen_program_ids.add(program["id"])
                    if event == "PROGRAM_DISCOVERED":
                        counts["programs_discovered"] += 1
                except Exception as exc:
                    counts["errors"].append({
                        "source_id": source["id"], "code": "PROGRAM_REJECTED",
                        "message": clean_text(str(exc), limit=240),
                    })

            for raw in result.jobs:
                try:
                    item = normalize_job(raw)
                    if verification_role == "verification" and item.get("official_url"):
                        item["verification_status"] = "verified"
                        item["confidence_score"] = max(item["confidence_score"], 0.9)
                        item["content_hash"] = semantic_hash(item, SEMANTIC_JOB_FIELDS)
                    elif item["verification_status"] == "verified":
                        item["verification_status"] = "pending"
                        item["confidence_score"] = min(item["confidence_score"], 0.7)
                        item["content_hash"] = semantic_hash(item, SEMANTIC_JOB_FIELDS)
                    program_id = item.get("program_id")
                    if item.get("program_external_id"):
                        program_id = program_ids_by_external.get(item["program_external_id"])
                        if not program_id:
                            existing_program = self.repository.find_program(
                                connection, item["program_external_id"]
                            )
                            program_id = existing_program["id"] if existing_program else None
                    job, event = self._upsert_job(
                        connection, item=item, program_id=program_id, source=source,
                        verification_role=verification_role, run_id=run_id, now=now,
                    )
                    seen_job_ids.add(job["id"])
                    if event == "NEW":
                        counts["new_jobs"] += 1
                    elif event == "UPDATED" or event == "VERIFIED":
                        counts["updated_jobs"] += 1
                    elif event == "CLOSED":
                        counts["closed_jobs"] += 1
                    elif event == "REOPENED":
                        counts["reopened_jobs"] += 1
                    else:
                        counts["unchanged_jobs"] += 1
                except Exception as exc:
                    counts["errors"].append({
                        "source_id": source["id"], "code": "JOB_REJECTED",
                        "message": clean_text(str(exc), limit=240),
                    })

            for raw in result.articles:
                try:
                    article = self._normalize_article(raw, source_id=source["id"])
                    article_id, is_new, changed = self.repository.upsert_article(
                        connection, article, source_id=source["id"], now=now
                    )
                    counts["articles_discovered"] += int(changed)
                    if changed:
                        event_type = "ARTICLE_DISCOVERED" if is_new else "ARTICLE_UPDATED"
                        self.repository.insert_event(
                            connection,
                            run_id=run_id,
                            entity_type="article",
                            entity_id=article_id,
                            external_id=article["article_external_id"],
                            event_type=event_type,
                            before=None,
                            after=article,
                            fields=[
                                "article_title", "article_url", "publish_time",
                                "is_recruitment", "recruitment_year", "classification",
                            ],
                            source_id=source["id"],
                            now=now,
                        )
                except Exception as exc:
                    counts["errors"].append({
                        "source_id": source["id"], "code": "ARTICLE_REJECTED",
                        "message": clean_text(str(exc), limit=240),
                    })

            if result.snapshot_complete:
                self.repository.process_missing_programs(
                    connection,
                    source=source,
                    seen_program_ids=seen_program_ids,
                    threshold=int(source.get("adapter_config", {}).get(
                        "close_confirmations", self.close_confirmations
                    )),
                    run_id=run_id,
                    now=now,
                )
                counts["closed_jobs"] += self.repository.process_missing_jobs(
                    connection,
                    source=source,
                    seen_job_ids=seen_job_ids,
                    threshold=int(source.get("adapter_config", {}).get(
                        "close_confirmations", self.close_confirmations
                    )),
                    run_id=run_id,
                    now=now,
                )
        return counts

    @staticmethod
    def _verification_role(source: dict[str, Any]) -> str:
        if source.get("trust_level") == "verification":
            return "verification"
        if (
            source.get("source_type") in {"official_html", "official_api", "ats"}
            and source.get("verification_status") == "verified"
        ):
            return "verification"
        return "discovery"

    def _upsert_program(
        self,
        connection: sqlite3.Connection,
        *,
        item: dict[str, Any],
        source: dict[str, Any],
        verification_role: str,
        run_id: str,
        now: str,
    ) -> tuple[dict[str, Any], str | None]:
        existing = self.repository.find_program(connection, item["external_id"])
        event: str | None = None
        if not existing:
            program = self.repository.insert_program(
                connection, item, source_id=source["id"], now=now
            )
            event = "PROGRAM_DISCOVERED"
            self.repository.insert_event(
                connection, run_id=run_id, entity_type="program", entity_id=program["id"],
                external_id=program["external_id"], event_type=event, before=None,
                after=program, fields=list(SEMANTIC_PROGRAM_FIELDS), source_id=source["id"], now=now,
            )
        else:
            merged = self._merge_verified(existing, item)
            merged["content_hash"] = semantic_hash(merged, SEMANTIC_PROGRAM_FIELDS)
            fields = changed_fields(existing, merged, SEMANTIC_PROGRAM_FIELDS)
            if not fields:
                self.repository.touch_program(connection, existing["id"], now)
                program = self.repository.find_program(connection, item["external_id"]) or existing
            else:
                if existing["status"] == "closed" and merged["status"] == "open":
                    event = "PROGRAM_REOPENED"
                elif existing["verification_status"] != "verified" and merged["verification_status"] == "verified":
                    event = "PROGRAM_VERIFIED"
                else:
                    event = "PROGRAM_UPDATED"
                program = self.repository.update_program(
                    connection, existing["id"], merged, source_id=source["id"], now=now
                )
                self.repository.insert_event(
                    connection, run_id=run_id, entity_type="program", entity_id=program["id"],
                    external_id=program["external_id"], event_type=event, before=existing,
                    after=program, fields=fields, source_id=source["id"], now=now,
                )
        self.repository.link_program_source(
            connection, program_id=program["id"], source=source,
            source_url=item.get("official_url") or source.get("url"),
            verification_role=verification_role, now=now,
            evidence=list(item.get("evidence") or []),
        )
        return program, event

    def _upsert_job(
        self,
        connection: sqlite3.Connection,
        *,
        item: dict[str, Any],
        program_id: str | None,
        source: dict[str, Any],
        verification_role: str,
        run_id: str,
        now: str,
    ) -> tuple[dict[str, Any], str | None]:
        existing = self.repository.find_job(connection, item["external_id"])
        event: str | None = None
        if not existing:
            job = self.repository.insert_job(
                connection, item, source_id=source["id"], program_id=program_id, now=now
            )
            event = "NEW"
            self.repository.insert_event(
                connection, run_id=run_id, entity_type="job", entity_id=job["id"],
                external_id=job["external_id"], event_type=event, before=None, after=job,
                fields=list(SEMANTIC_JOB_FIELDS), source_id=source["id"], now=now,
            )
        else:
            merged = self._merge_verified(existing, item)
            # A discovery mirror alone cannot close a previously open job.
            if item["status"] == "closed" and verification_role != "verification":
                merged["status"] = existing["status"]
            merged["content_hash"] = semantic_hash(merged, SEMANTIC_JOB_FIELDS)
            fields = changed_fields(existing, merged, SEMANTIC_JOB_FIELDS)
            if not fields and (program_id is None or program_id == existing.get("program_id")):
                self.repository.touch_job(connection, existing["id"], now)
                job = self.repository.find_job(connection, item["external_id"]) or existing
            else:
                if existing["status"] == "closed" and merged["status"] == "open":
                    event = "REOPENED"
                elif existing["status"] != "closed" and merged["status"] == "closed":
                    event = "CLOSED"
                elif existing["verification_status"] != "verified" and merged["verification_status"] == "verified":
                    event = "VERIFIED"
                else:
                    event = "UPDATED"
                job = self.repository.update_job(
                    connection, existing["id"], merged, source_id=source["id"],
                    program_id=program_id or existing.get("program_id"), now=now,
                )
                self.repository.insert_event(
                    connection, run_id=run_id, entity_type="job", entity_id=job["id"],
                    external_id=job["external_id"], event_type=event, before=existing,
                    after=job, fields=fields or ["program_id"], source_id=source["id"], now=now,
                )
        self.repository.link_job_source(
            connection, job_id=job["id"], source=source,
            source_url=item.get("official_url") or source.get("url"),
            verification_role=verification_role, now=now,
            evidence=list(item.get("evidence") or []),
        )
        return job, event

    @staticmethod
    def _merge_verified(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(incoming)
        if existing.get("verification_status") == "verified" and incoming.get("verification_status") != "verified":
            merged["verification_status"] = "verified"
            merged["confidence_score"] = max(
                float(existing.get("confidence_score") or 0),
                float(incoming.get("confidence_score") or 0),
            )
        for field in ("official_url", "application_url", "opening_date", "closing_date"):
            if not merged.get(field) and existing.get(field):
                merged[field] = existing[field]
        return merged

    @staticmethod
    def _normalize_article(raw: dict[str, Any], *, source_id: str) -> dict[str, Any]:
        title = clean_text(raw.get("article_title") or raw.get("title"), limit=300)
        if not title:
            raise ValueError("article_title is required")
        article_url = canonicalize_url(raw.get("article_url") or raw.get("url"))
        external = clean_text(raw.get("article_external_id"), limit=180)
        if not external:
            external = stable_digest(source_id, article_url or title, prefix="article")
        excerpt = clean_text(raw.get("raw_excerpt") or raw.get("excerpt"), limit=1_500)
        content_hash = hashlib.sha256(
            json.dumps({"title": title, "url": article_url, "excerpt": excerpt},
                       ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        year = raw.get("recruitment_year")
        try:
            year = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        return {
            "article_external_id": external,
            "publisher": clean_text(raw.get("publisher"), limit=160),
            "article_title": title,
            "article_url": article_url,
            "publish_time": clean_text(raw.get("publish_time"), limit=80) or None,
            "content_hash": content_hash,
            "raw_excerpt": excerpt,
            "is_recruitment": bool(raw.get("is_recruitment")),
            "recruitment_year": year,
            "classification": clean_text(raw.get("classification") or "unknown", limit=80),
        }

    def sync(self, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        if payload.get("version") != "FROSTFIRE_SYNC_V1":
            raise ValueError("Unsupported sync version.")
        source_id = clean_text(payload.get("source_id"), limit=64)
        if not source_id:
            raise ValueError("source_id is required.")
        source = self.repository.get_source(source_id)
        if not source:
            source = self.repository.create_source({
                "id": source_id,
                "name": clean_text(payload.get("source_name") or source_id, limit=160),
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
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        batch_key = idempotency_key or clean_text(payload.get("batch_id"), limit=180) or payload_hash
        existing = self.repository.sync_batch(batch_key)
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise SyncConflict("Idempotency key was already used for a different payload.")
            return {**existing["result"], "idempotent_replay": True}

        run = self.repository.create_run("sync", [source_id])
        result = AdapterResult(
            programs=list(payload.get("programs") or []),
            jobs=list(payload.get("jobs") or []),
            articles=list(payload.get("articles") or []),
            content_hash=payload_hash,
            snapshot_complete=bool(payload.get("snapshot_complete", False)),
        )
        counts = self.process_result(source=source, result=result, run_id=run["id"])
        summary = self._empty_summary(status="success")
        summary["sources_checked"] = 1
        summary["sources_succeeded"] = 1
        for key in (
            "programs_discovered", "new_jobs", "updated_jobs", "closed_jobs",
            "reopened_jobs", "unchanged_jobs", "articles_discovered", "ai_calls",
            "model_tokens_used",
        ):
            summary[key] = counts[key]
        summary["errors"] = counts["errors"]
        finished = self.repository.finish_run(run["id"], summary)
        self.repository.update_source_success(source_id, content_hash=payload_hash)
        response = {"run": finished, "counts": counts, "idempotent_replay": False}
        self.repository.save_sync_batch(
            key=batch_key, payload_hash=payload_hash, source_id=source_id,
            run_id=run["id"], result=response,
        )
        return response
