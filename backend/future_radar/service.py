"""Future Radar orchestration, diffing, verification and idempotent sync."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
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
    normalize_tags,
    normalize_taxonomy_tags,
    semantic_hash,
    stable_digest,
)
from .repository import (
    RUN_LOCK_TTL_SECONDS,
    SOURCE_LOCK_TTL_SECONDS,
    RadarRepository,
    utc_now,
)
from .seeds import initial_sources


logger = logging.getLogger(__name__)
RESULT_WRITE_BATCH_SIZE = 25


def _safe_source_failure(
    source: dict[str, Any], exc: Exception
) -> tuple[str, str]:
    """Map provider/network failures to stable public diagnostics.

    Radar run records and source health are visible to every signed-in user.
    Provider responses can contain request identifiers or other operational
    detail, so the original exception is used only for classification and is
    never persisted or returned by the API.
    """
    adapter = str(
        source.get("adapter_config", {}).get("adapter")
        or source.get("source_type")
        or ""
    ).casefold()
    source_type = str(source.get("source_type") or "").casefold()
    platform = str(source.get("platform") or "").casefold()
    fingerprint = f"{type(exc).__name__} {exc}".casefold()
    if (
        adapter in {"openai_web_search", "wechat_web_search"}
        or source_type == "openai_web_search"
        or platform == "openai"
    ):
        if any(marker in fingerprint for marker in (
            "credit_balance_exhausted", "insufficient_quota", "billing",
            "credit balance", "quota exceeded",
        )):
            return (
                "AI_CREDITS_EXHAUSTED",
                "AI 补漏额度暂不可用；确定性官网信源仍会继续扫描。",
            )
        if any(marker in fingerprint for marker in (
            "rate limit", "ratelimit", "too many requests", "429",
        )):
            return (
                "AI_RATE_LIMITED",
                "AI 补漏当前受到频率限制；确定性官网信源仍会继续扫描。",
            )
        return (
            "AI_PROVIDER_UNAVAILABLE",
            "AI 补漏暂时不可用；确定性官网信源仍会继续扫描。",
        )
    if adapter in {"official_html", "rss", "atom"}:
        return "SOURCE_UNAVAILABLE", "公开信源暂时无法访问，稍后会自动重试。"
    return "SOURCE_FAILED", "该信源本轮扫描未完成，稍后会自动重试。"


class RadarRunBusy(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        scan_type: str = "scheduled",
        lock_type: str = "run",
    ) -> None:
        super().__init__(message)
        self.scan_type = scan_type
        self.lock_type = lock_type


class RadarSourceBusy(RuntimeError):
    def __init__(self, source_id: str) -> None:
        super().__init__(f"Source {source_id} is already running.")
        self.source_id = source_id


class RadarLeaseLost(RuntimeError):
    pass


class RadarPartialWriteError(sqlite3.OperationalError):
    """An interrupted source may already have committed safe, complete batches."""

    def __init__(self, summary: dict[str, Any]) -> None:
        super().__init__("Future Radar processing stopped after committed batches.")
        # Only the fixed public counters/diagnostics, never SQL or provider text.
        self.committed_summary = json.loads(json.dumps(summary))


class _LeaseHeartbeat:
    """Keep one database lease alive for the complete protected operation."""

    def __init__(
        self,
        repository: RadarRepository,
        *,
        lock_name: str,
        owner: str,
        ttl_seconds: int,
    ) -> None:
        self.repository = repository
        self.lock_name = lock_name
        self.owner = owner
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.interval_seconds = max(
            0.05, min(30.0, self.ttl_seconds / 4)
        )
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._maintain,
            name="future-radar-lease-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _maintain(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.repository.renew_lock(
                    self.lock_name,
                    self.owner,
                    self.ttl_seconds,
                )
            except Exception as exc:
                # Database contention is normally transient and the heartbeat
                # has several attempts before the lease expires.
                logger.warning(
                    "Future Radar lease heartbeat retry lock_type=%s error_type=%s",
                    "source" if ":source:" in self.lock_name else "run",
                    type(exc).__name__,
                )
                continue
            if not renewed:
                self._lost.set()
                return

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise RadarLeaseLost("Future Radar database lease ownership was lost.")

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval_seconds + 0.5))


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
        run_lock_ttl_seconds: int = RUN_LOCK_TTL_SECONDS,
        source_lock_ttl_seconds: int = SOURCE_LOCK_TTL_SECONDS,
    ) -> None:
        self.repository = RadarRepository(connect)
        self.openai_api_key = openai_api_key
        self.ai_model = ai_model
        self.web_search_enabled = web_search_enabled
        self.close_confirmations = max(2, min(10, int(close_confirmations)))
        self.max_workers = max(1, min(8, int(max_workers)))
        self.adapter_factory = adapter_factory
        self.run_lock_ttl_seconds = max(1, int(run_lock_ttl_seconds))
        self.source_lock_ttl_seconds = max(1, int(source_lock_ttl_seconds))

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
            "sources_skipped": 0,
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
        scan_type: str = "scheduled",
        force: bool = False,
        bridge_candidate_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_scan_type = str(scan_type or "scheduled").strip().casefold()
        if normalized_scan_type not in {"scheduled", "quick", "deep"}:
            raise ValueError("scan_type must be 'scheduled', 'quick', or 'deep'.")
        if bridge_candidate_ids is not None and (
            not isinstance(bridge_candidate_ids, list) or len(bridge_candidate_ids) > 10
            or any(not isinstance(value, str) or not re.fullmatch(r"candidate-[0-9a-f]{32}", value)
                   for value in bridge_candidate_ids)
        ):
            raise ValueError("bridge_candidate_ids must contain at most ten candidate IDs.")
        owner = str(uuid.uuid4())
        run_lock_name = f"future-radar-run:{normalized_scan_type}"
        if not self.repository.acquire_lock(
            run_lock_name, owner, ttl_seconds=self.run_lock_ttl_seconds
        ):
            raise RadarRunBusy(
                f"A {normalized_scan_type} Future Radar run is already active.",
                scan_type=normalized_scan_type,
            )
        run_lease = _LeaseHeartbeat(
            self.repository,
            lock_name=run_lock_name,
            owner=owner,
            ttl_seconds=self.run_lock_ttl_seconds,
        )
        run_lease.start()
        run: dict[str, Any] | None = None
        summary = self._empty_summary()
        partially_completed_sources = 0
        try:
            if normalized_scan_type == "scheduled":
                sources = (
                    [source for source_id in source_ids or []
                     if (source := self.repository.get_source(source_id))
                     and (source["enabled"] or force)]
                    if source_ids
                    else self.repository.due_sources()
                )
            else:
                sources = self.repository.manual_scan_sources(
                    normalized_scan_type,
                    source_ids=source_ids,
                    force=force,
                )
            if bridge_candidate_ids is not None:
                # Private orchestration hint only: never rewrite the registry.
                # A normal Quick Scan without this hint still sees all rows.
                selected = list(dict.fromkeys(bridge_candidate_ids))
                sources = [
                    {**source, "adapter_config": {
                        **source.get("adapter_config", {}), "candidate_ids": selected,
                    }} if source["id"] == "legacy-search-discovery" else source
                    for source in sources
                ]
            run = self.repository.create_run(
                trigger_type,
                [source["id"] for source in sources],
                scan_type=normalized_scan_type,
                force=force,
                run_id=owner,
            )
            run_lease.ensure_owned()
            if not sources:
                summary["status"] = "success"
                return self.repository.finish_run(run["id"], summary)

            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(sources))) as executor:
                futures = {
                    executor.submit(
                        self._scan_source,
                        source,
                        run["id"],
                        normalized_scan_type,
                        force,
                    ): source
                    for source in sources
                }
                for future in as_completed(futures):
                    source = futures[future]
                    summary["sources_checked"] += 1
                    try:
                        result = future.result()
                    except RadarSourceBusy:
                        summary["sources_skipped"] += 1
                        summary["errors"].append({
                            "source_id": source["id"],
                            "code": "SOURCE_BUSY",
                            "message": "该信源已有扫描任务正在运行，本轮已跳过。",
                        })
                        continue
                    except DiscoveryLimitedError:
                        message = "该信源尚未配置可合法访问的公开入口。"
                        if not self._is_scoped_bridge(source):
                            self.repository.update_source_limited(source["id"], message)
                        summary["sources_failed"] += 1
                        summary["errors"].append({
                            "source_id": source["id"],
                            "code": "DISCOVERY_LIMITED",
                            "message": message,
                        })
                        continue
                    except Exception as exc:
                        code, message = _safe_source_failure(source, exc)
                        logger.warning(
                            "Future Radar source failed run_id=%s source_id=%s "
                            "adapter=%s failure_code=%s error_type=%s",
                            run["id"],
                            source["id"],
                            source.get("adapter_config", {}).get("adapter") or source.get("source_type"),
                            code,
                            type(exc).__name__,
                        )
                        if not self._is_scoped_bridge(source):
                            self.repository.update_source_error(source["id"], message)
                        summary["sources_failed"] += 1
                        if isinstance(exc, RadarPartialWriteError):
                            committed = exc.committed_summary
                            for key in (
                                "programs_discovered", "new_jobs", "updated_jobs", "closed_jobs",
                                "reopened_jobs", "unchanged_jobs", "articles_discovered", "ai_calls",
                                "model_tokens_used",
                            ):
                                summary[key] += int(committed.get(key, 0))
                            summary["errors"].extend(committed.get("errors", []))
                        summary["errors"].append({
                            "source_id": source["id"],
                            "code": code,
                            "message": message,
                        })
                        continue
                    summary["sources_succeeded"] += 1
                    if result.get("status") == "partial_success":
                        partially_completed_sources += 1
                    for key in (
                        "programs_discovered", "new_jobs", "updated_jobs", "closed_jobs",
                        "reopened_jobs", "unchanged_jobs", "articles_discovered", "ai_calls",
                        "model_tokens_used",
                    ):
                        summary[key] += int(result.get(key, 0))
                    summary["errors"].extend(result.get("errors", []))

            run_lease.ensure_owned()
            if summary["sources_succeeded"] and (
                summary["sources_failed"] or summary["sources_skipped"]
                or partially_completed_sources
            ):
                summary["status"] = "partial_success"
            elif summary["sources_failed"]:
                summary["status"] = "failed"
            elif summary["sources_skipped"]:
                summary["status"] = "skipped"
            else:
                summary["status"] = "success"
            return self.repository.finish_run(run["id"], summary)
        except Exception:
            # A run row must never remain permanently RUNNING merely because
            # orchestration itself failed after creation.  Individual source
            # failures are handled above and do not reach this block.
            if run:
                stored = self.repository.get_run(run["id"])
                if stored and stored.get("status") == "running":
                    summary["status"] = "failed"
                    summary["errors"].append({
                        "source_id": "",
                        "code": "RUN_FAILED",
                        "message": "Future Radar orchestration did not complete.",
                    })
                    try:
                        self.repository.finish_run(run["id"], summary)
                    except Exception:
                        logger.exception(
                            "Future Radar failed to finalize aborted run_id=%s",
                            run["id"],
                        )
            raise
        finally:
            run_lease.stop()
            self.repository.release_lock(run_lock_name, owner)

    @staticmethod
    def _is_scoped_bridge(source: dict[str, Any]) -> bool:
        return source.get("id") == "legacy-search-discovery" and "candidate_ids" in source.get("adapter_config", {})

    def _scan_source(
        self,
        source: dict[str, Any],
        run_id: str,
        scan_type: str = "scheduled",
        force: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        owner = str(uuid.uuid4())
        lock_name = f"future-radar-source:{source['id']}"
        if not self.repository.acquire_lock(
            lock_name, owner, ttl_seconds=self.source_lock_ttl_seconds
        ):
            raise RadarSourceBusy(str(source["id"]))
        source_lease = _LeaseHeartbeat(
            self.repository,
            lock_name=lock_name,
            owner=owner,
            ttl_seconds=self.source_lock_ttl_seconds,
        )
        source_lease.start()
        try:
            scan_source = {
                **source,
                "adapter_config": {
                    **source.get("adapter_config", {}),
                    # A transient orchestration flag: never stored in the
                    # source registry or returned by the API.
                    # Manual Deep Scan is an explicit request for fresh
                    # discovery.  It therefore re-runs optional AI extraction
                    # even when the public page content hash is unchanged.
                    # Scheduled scans may still reuse deterministic extraction
                    # results; business/entity hashes continue to prevent
                    # duplicate jobs and events in every mode.
                    "_force_refresh": bool(force or scan_type == "deep"),
                },
            }
            if scan_type == "quick":
                # Quick Scan must stay deterministic even when an operator has
                # enabled optional AI extraction for a known HTML source.
                scan_source = {
                    **scan_source,
                    "adapter_config": {
                        **scan_source.get("adapter_config", {}),
                        "ai_extract": False,
                    },
                }
            result = self._adapter(scan_source).scan(scan_source)
            source_lease.ensure_owned()
            try:
                failed_companies = max(0, int(result.coverage.get("failed_count", 0)))
            except (TypeError, ValueError, OverflowError):
                failed_companies = 0
            partially_completed = (
                result.status in {"partial", "partial_success"}
                or failed_companies > 0
            )
            source_status = "partial" if partially_completed else result.status
            if partially_completed:
                # Unsearched employer batches are not evidence that their
                # previous jobs disappeared, even if an adapter forgot to
                # clear the complete-snapshot default.
                result.snapshot_complete = False
            def lease_guard(connection):
                source_lease.ensure_owned()
                row = connection.execute(
                    "SELECT owner, expires_at FROM radar_locks WHERE lock_name=?", (lock_name,),
                ).fetchone()
                if row is None or row["owner"] != owner or row["expires_at"] <= utc_now():
                    raise RadarLeaseLost("Future Radar database lease ownership was lost.")

            processed = self.process_result(
                source=source, result=result, run_id=run_id, lease_guard=lease_guard,
            )
            if processed.get("status") == "partial_success":
                source_status = "partial"
            source_lease.ensure_owned()
            if partially_completed:
                # Good observations have already been committed. A failed
                # employer batch must not either discard them or make this
                # source/run appear fully successful. Never echo a provider's
                # diagnostics, prompts or credentials in the public run.
                processed["status"] = "partial_success"
                processed["errors"].append({
                    "source_id": source["id"],
                    "code": "COMPANY_SEARCH_INCOMPLETE",
                    "message": (
                        f"本轮有 {failed_companies} 家企业的搜索未完成；已取得的候选已保留。"
                        if failed_companies
                        else "该信源本轮仅部分完成；已取得的候选已保留。"
                    ),
                })
            # A small ingest projection is not a full source scan. Keep its
            # actual outcome in the Run/events, without replacing the full
            # snapshot or advancing scheduler due-time/error/health baselines.
            # Otherwise repeated imports can indefinitely postpone previously
            # deferred candidates that only the regular full scan will see.
            scoped_bridge = self._is_scoped_bridge(source)
            if not scoped_bridge and result.content_hash and (result.normalized_content or result.coverage):
                self.repository.save_snapshot(
                    source["id"], result.content_hash, result.normalized_content,
                    {
                        "status": source_status,
                        "jobs": len(result.jobs),
                        "programs": len(result.programs),
                        "coverage": result.coverage,
                    },
                )
            if not scoped_bridge:
                self.repository.update_source_success(
                    source["id"], content_hash=result.content_hash,
                    status="healthy" if source_status in {"healthy", "idle"} else source_status,
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
                source_status,
            )
            return processed
        finally:
            source_lease.stop()
            self.repository.release_lock(lock_name, owner)

    @contextmanager
    def _result_transaction(self, lease_guard, summary):
        try:
            with self.repository.transaction() as connection:
                if lease_guard is not None:
                    lease_guard(connection)
                yield connection
        except (sqlite3.OperationalError, RadarLeaseLost) as exc:
            if any(summary.get(key, 0) for key in (
                "programs_discovered", "new_jobs", "updated_jobs", "closed_jobs",
                "reopened_jobs", "unchanged_jobs", "articles_discovered",
            )):
                raise RadarPartialWriteError(summary) from exc
            raise

    def process_result(
        self,
        *,
        source: dict[str, Any],
        result: AdapterResult,
        run_id: str,
        lease_guard: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        counts = self._empty_summary(status="success")
        counts["ai_calls"] = result.ai_calls
        counts["model_tokens_used"] = result.model_tokens_used
        now = utc_now()
        verification_role = self._verification_role(source)
        mixed_verification_source = source.get("adapter_config", {}).get("adapter") == "legacy_database"
        program_ids_by_external: dict[str, str] = {}
        seen_program_ids: set[str] = set()
        seen_job_ids: set[str] = set()
        program_failed = False
        job_failed = False

        def rejected(kind: str, exc: Exception) -> None:
            logger.info("Future Radar rejected an item source_id=%s kind=%s error_type=%s",
                        source["id"], kind, type(exc).__name__)
            counts["errors"].append({
                "source_id": source["id"], "code": f"{kind}_REJECTED",
                "message": "候选项目未通过结构或安全校验。" if kind == "PROGRAM"
                else "候选岗位未通过结构或安全校验。" if kind == "JOB"
                else "候选文章未通过结构或安全校验。",
            })
            counts["status"] = "partial_success"

        for raw in result.programs:
            try:
                item = normalize_program(raw)
                if (verification_role == "verification" and item.get("official_url")
                        and (not mixed_verification_source or item.get("verification_status") == "verified")):
                    item["verification_status"] = "verified"
                    item["confidence_score"] = max(item["confidence_score"], 0.9)
                elif item["verification_status"] == "verified":
                    item["verification_status"] = "pending"
                    item["confidence_score"] = min(item["confidence_score"], 0.7)
                item["content_hash"] = semantic_hash(item, SEMANTIC_PROGRAM_FIELDS)
                # A program and its event/provenance remain atomic. No source
                # holds the schema-wide compatibility lock for its full scan.
                with self._result_transaction(lease_guard, counts) as connection:
                    program, event = self._upsert_program(
                        connection, item=item, source=source, verification_role=verification_role,
                        run_id=run_id, now=now,
                    )
                program_ids_by_external[item["external_id"]] = program["id"]
                seen_program_ids.add(program["id"])
                counts["programs_discovered"] += int(event == "PROGRAM_DISCOVERED")
            except (sqlite3.OperationalError, RadarLeaseLost):
                raise
            except Exception as exc:
                program_failed = True
                rejected("PROGRAM", exc)

        def count_jobs(outcomes):
            for job, event in outcomes:
                seen_job_ids.add(job["id"])
                key = {
                    "NEW": "new_jobs", "UPDATED": "updated_jobs", "VERIFIED": "updated_jobs",
                    "CLOSED": "closed_jobs", "REOPENED": "reopened_jobs",
                }.get(event, "unchanged_jobs")
                counts[key] += 1

        # Normalize without an open write transaction; then prefetch/diff and
        # pipeline a fixed-size batch. Each committed batch is immediately
        # visible and queued ingests/lease heartbeats can take the write lock.
        for offset in range(0, len(result.jobs), RESULT_WRITE_BATCH_SIZE):
            batch = []
            for raw in result.jobs[offset:offset + RESULT_WRITE_BATCH_SIZE]:
                try:
                    item = normalize_job(raw)
                    role = verification_role
                    if item["external_id"] in result.verified_job_external_ids and item.get("official_url"):
                        role = "verification"
                    if (role == "verification" and item.get("official_url")
                            and (not mixed_verification_source or item.get("verification_status") == "verified")):
                        item["verification_status"] = "verified"
                        item["confidence_score"] = max(item["confidence_score"], 0.9)
                    elif item["verification_status"] == "verified":
                        item["verification_status"] = "pending"
                        item["confidence_score"] = min(item["confidence_score"], 0.7)
                    item["content_hash"] = semantic_hash(item, SEMANTIC_JOB_FIELDS)
                    batch.append((item, role))
                except Exception as exc:
                    job_failed = True
                    rejected("JOB", exc)
            if not batch:
                continue
            try:
                with self._result_transaction(lease_guard, counts) as connection:
                    outcomes = self._upsert_job_batch(
                        connection, batch=batch, source=source, run_id=run_id, now=now,
                        program_ids_by_external=program_ids_by_external,
                    )
                count_jobs(outcomes)
            except (sqlite3.OperationalError, RadarLeaseLost):
                # A broken connection/lock timeout is an operational failure,
                # not 25 fabricated invalid jobs or 25 repeated long retries.
                raise
            except Exception:
                # Roll back the whole failed batch first. Isolate a malformed
                # row without losing valid neighbours or committing half a job.
                for item, role in batch:
                    try:
                        with self._result_transaction(lease_guard, counts) as connection:
                            program_id = self._resolve_program_id(
                                connection, item, program_ids_by_external,
                            )
                            outcome = self._upsert_job(
                                connection, item=item, program_id=program_id, source=source,
                                verification_role=role, run_id=run_id, now=now,
                            )
                        count_jobs([outcome])
                    except (sqlite3.OperationalError, RadarLeaseLost):
                        raise
                    except Exception as exc:
                        job_failed = True
                        rejected("JOB", exc)

        for raw in result.articles:
            try:
                article = self._normalize_article(raw, source_id=source["id"])
                with self._result_transaction(lease_guard, counts) as connection:
                    article_id, is_new, changed = self.repository.upsert_article(
                        connection, article, source_id=source["id"], now=now,
                    )
                    if changed:
                        self.repository.insert_event(
                            connection, run_id=run_id, entity_type="article", entity_id=article_id,
                            external_id=article["article_external_id"],
                            event_type="ARTICLE_DISCOVERED" if is_new else "ARTICLE_UPDATED",
                            before=None, after=article,
                            fields=["article_title", "article_url", "publish_time", "is_recruitment",
                                    "recruitment_year", "classification"],
                            source_id=source["id"], now=now,
                        )
                counts["articles_discovered"] += int(changed)
            except (sqlite3.OperationalError, RadarLeaseLost):
                raise
            except Exception as exc:
                rejected("ARTICLE", exc)

        # A partial/failed observation is not evidence of disappearance.
        # Retire missing links only after all relevant items were committed,
        # and also release the write lock between bounded retirement batches.
        explicit_retirements = sorted(getattr(result, "retired_job_external_ids", set()))
        for offset in range(0, len(explicit_retirements), RESULT_WRITE_BATCH_SIZE):
            with self._result_transaction(lease_guard, counts) as connection:
                counts["closed_jobs"] += self.repository.retire_source_observations(
                    connection, source_id=source["id"],
                    external_ids=explicit_retirements[offset:offset + RESULT_WRITE_BATCH_SIZE],
                    run_id=run_id, now=now,
                )
        threshold = int(source.get("adapter_config", {}).get("close_confirmations", self.close_confirmations))
        if result.snapshot_complete and not program_failed:
            missing = self.repository.missing_entity_ids("program", source["id"], seen_program_ids)
            for program_id in missing:
                with self._result_transaction(lease_guard, counts) as connection:
                    self.repository.process_missing_programs(
                        connection, source=source, seen_program_ids=seen_program_ids,
                        threshold=threshold, run_id=run_id, now=now, only_ids=[program_id],
                    )
        if result.snapshot_complete and not job_failed:
            missing = self.repository.missing_entity_ids("job", source["id"], seen_job_ids)
            for offset in range(0, len(missing), RESULT_WRITE_BATCH_SIZE):
                with self._result_transaction(lease_guard, counts) as connection:
                    counts["closed_jobs"] += self.repository.process_missing_jobs(
                        connection, source=source, seen_job_ids=seen_job_ids,
                        threshold=threshold, run_id=run_id, now=now,
                        only_ids=missing[offset:offset + RESULT_WRITE_BATCH_SIZE],
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
            merged = self._merge_verified(
                existing, item, incoming_role=verification_role
            )
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

    def _resolve_program_id(self, connection, item, program_ids_by_external):
        external_id = item.get("program_external_id")
        if not external_id:
            return item.get("program_id")
        program_id = program_ids_by_external.get(external_id)
        if not program_id:
            existing = self.repository.find_program(connection, external_id)
            program_id = existing["id"] if existing else None
        return program_id

    def _upsert_job_batch(
        self, connection, *, batch, source, run_id, now, program_ids_by_external,
    ):
        existing_by_external = self.repository.find_jobs(
            connection, list(dict.fromkeys(item["external_id"] for item, _ in batch)),
        )
        program_ids = dict(program_ids_by_external)
        unknown_programs = list({item["program_external_id"] for item, _ in batch
                                 if item.get("program_external_id") not in program_ids
                                 and item.get("program_external_id")})
        if unknown_programs:
            program_ids.update({row["external_id"]: row["id"] for row in connection.execute(
                "SELECT external_id, id FROM recruitment_programs WHERE external_id IN ("
                + ",".join("?" for _ in unknown_programs) + ")", unknown_programs,
            ).fetchall()})
        mutations = []
        for item, role in batch:
            existing = existing_by_external.get(item["external_id"])
            program_id = (program_ids.get(item["program_external_id"])
                          if item.get("program_external_id") else item.get("program_id"))
            event = None
            fields = []
            if existing is None:
                row = self.repository.new_job_record(
                    item, job_id=str(uuid.uuid4()), program_id=program_id, source_id=source["id"], now=now,
                )
                kind, event, fields = "insert", "NEW", list(SEMANTIC_JOB_FIELDS)
            else:
                merged = self._merge_verified(existing, item, incoming_role=role)
                if item["status"] == "closed" and role != "verification":
                    merged["status"] = existing["status"]
                merged["content_hash"] = semantic_hash(merged, SEMANTIC_JOB_FIELDS)
                fields = changed_fields(existing, merged, SEMANTIC_JOB_FIELDS)
                if not fields and (program_id is None or program_id == existing.get("program_id")):
                    kind = "touch"
                    row = {**existing, "last_seen_at": now, "missing_successes": 0, "updated_at": now}
                else:
                    kind = "update"
                    if existing["status"] == "closed" and merged["status"] == "open":
                        event = "REOPENED"
                    elif existing["status"] != "closed" and merged["status"] == "closed":
                        event = "CLOSED"
                    elif existing["verification_status"] != "verified" and merged["verification_status"] == "verified":
                        event = "VERIFIED"
                    else:
                        event = "UPDATED"
                    fields = fields or ["program_id"]
                    row = self.repository.new_job_record(
                        merged, job_id=existing["id"], program_id=program_id or existing.get("program_id"),
                        source_id=source["id"], now=now, existing=existing,
                    )
            mutations.append({
                "kind": kind, "row": row, "before": existing, "event": event, "fields": fields,
                "verification_role": role, "source_url": item.get("official_url") or source.get("url"),
                "evidence": list(item.get("evidence") or []),
            })
            # Repeated external IDs in a single observation retain sequential
            # merge semantics, while the actual SQL writes are pipelined.
            existing_by_external[item["external_id"]] = row
        self.repository.flush_job_batch(connection, mutations, source=source, run_id=run_id, now=now)
        return [(mutation["row"], mutation["event"]) for mutation in mutations]

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
            merged = self._merge_verified(
                existing, item, incoming_role=verification_role
            )
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
    def _merge_verified(
        existing: dict[str, Any],
        incoming: dict[str, Any],
        *,
        incoming_role: str = "discovery",
    ) -> dict[str, Any]:
        merged = dict(incoming)
        existing_verified = existing.get("verification_status") == "verified"
        if existing_verified and incoming.get("verification_status") != "verified":
            merged["verification_status"] = "verified"
            merged["confidence_score"] = max(
                float(existing.get("confidence_score") or 0),
                float(incoming.get("confidence_score") or 0),
            )

        def empty(value: Any) -> bool:
            return value is None or value == "" or value == [] or value == {}

        # Normalization materializes omitted optional fields as empty strings or
        # lists.  An incomplete observation must enrich a record, not erase
        # facts already collected from another source.
        preserve_when_empty = (
            "city", "region", "employer_type", "industry", "primary_category",
            "organization_category", "industry_tags", "role_tags", "official_url",
            "application_url", "opening_date", "closing_date", "description",
            "responsibilities", "requirements", "tags",
        )
        for field in preserve_when_empty:
            if empty(merged.get(field)) and not empty(existing.get(field)):
                merged[field] = existing[field]

        def list_value(value: Any) -> list[Any]:
            return list(value) if isinstance(value, (list, tuple, set)) else []

        # Human-facing source tags accumulate provenance and review markers.
        # Prefer the authoritative observation first when the 30-tag cap is hit.
        if "tags" in existing or "tags" in incoming:
            existing_tags = list_value(existing.get("tags"))
            incoming_tags = list_value(incoming.get("tags"))
            ordered_tags = (
                [*incoming_tags, *existing_tags]
                if incoming_role == "verification"
                else [*existing_tags, *incoming_tags]
            )
            merged["tags"] = normalize_tags(ordered_tags)

        protected_fields = (
            "company", "title", "city", "region", "employer_type", "industry",
            "primary_category", "organization_category", "industry_tags", "role_tags",
            "official_url", "application_url", "opening_date", "closing_date", "status",
            "description", "responsibilities", "requirements",
        )
        protected_from_discovery = existing_verified and incoming_role != "verification"
        if protected_from_discovery:
            for field in protected_fields:
                if not empty(existing.get(field)):
                    merged[field] = existing[field]

        # Machine tags are unordered taxonomy sets.  Verification sources may
        # complement one another; a discovery source cannot dilute an already
        # verified non-empty taxonomy.
        for field in ("industry_tags", "role_tags"):
            existing_values = list_value(existing.get(field))
            incoming_values = list_value(incoming.get(field))
            if protected_from_discovery and existing_values:
                merged[field] = normalize_taxonomy_tags(existing_values)
            else:
                merged[field] = normalize_taxonomy_tags(
                    [*existing_values, *incoming_values]
                )
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

        run = self.repository.create_run(
            "sync", [source_id], scan_type="sync", force=False
        )
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
