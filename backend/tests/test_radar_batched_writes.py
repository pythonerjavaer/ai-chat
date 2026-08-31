"""Bounded/atomic Radar writes; only isolated SQLite and loopback PostgreSQL.

No browser, test login, network source adapter, production credentials, or AI.
The real legacy adapter reads synthetic local ingest rows; official verification
is mocked pending. PG cursor delays simulate RTT, never a paid external call.
"""

from __future__ import annotations

import copy
import hashlib
import sqlite3
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.tests.test_opportunity_scoring_cache import cache_database  # noqa: F401
from backend import database
from backend.future_radar.adapters import AdapterResult, LegacyDiscoveryDatabaseAdapter
from backend.future_radar.normalization import normalize_job
from backend.future_radar.service import FutureRadarService, RadarLeaseLost, RadarPartialWriteError


SOURCE = "legacy-search-discovery"


@pytest.fixture
def batch_service(cache_database, monkeypatch):
    def connect(*, timeout=5):
        connection = cache_database.connect()
        if isinstance(connection, sqlite3.Connection):
            connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
        else:
            connection.timeout = timeout
        return connection

    monkeypatch.setattr(database, "connect", connect)
    database.init_db(connection_factory=connect)
    service = FutureRadarService(
        connect=connect, openai_api_key="not-used", ai_model="not-used", web_search_enabled=False,
        adapter_factory=lambda source: LegacyDiscoveryDatabaseAdapter(),
    )
    service.repository.seed_sources([
        {"id": SOURCE, "name": "本地公开线索", "source_type": "other_public_source", "trust_level": "discovery",
         "adapter_config": {"adapter": "legacy_database", "discovery_only": True}},
        {"id": "official-test", "name": "示例官网", "source_type": "official_api", "trust_level": "verification",
         "verification_status": "verified"},
    ])
    return SimpleNamespace(service=service, connect=connect, backend=cache_database.backend)


def raw_job(key, **changes):
    return {
        "external_id": key, "company": "示例科技", "title": f"2027 校园招聘数据分析岗 {key}",
        "city": "上海", "region": "中国大陆", "employer_type": "互联网企业", "industry": "科技",
        "primary_category": "internet_tech", "status": "open", "verification_status": "pending",
        "official_url": f"https://careers.example.com/campus/{key}",
        "requirements": "面向2027届毕业生，掌握数据分析技能。", "tags": ["2027届", "校园招聘"], **changes,
    }


def process(harness, jobs, *, complete=False, source_id=SOURCE, **kwargs):
    return harness.service.process_result(
        source=harness.service.repository.get_source(source_id),
        result=AdapterResult(jobs=jobs, snapshot_complete=complete),
        run_id=f"isolated-{uuid.uuid4().hex}", **kwargs,
    )


def rows(harness, sql, params=()):
    with harness.connect() as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def counts(harness):
    return {table: rows(harness, f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
            for table in ("radar_jobs", "job_sources", "radar_events")}


def candidate(key):
    digest = hashlib.sha256(key.encode()).hexdigest()
    return {
        "id": f"candidate-{digest[:32]}", "dedupe_key": digest, "source_key": "chatgpt-radar-03",
        "source_id": "chatgpt-radar-03", "source_item_id": key, "external_id": key,
        "company": "示例科技", "employer_type": "互联网企业", "title": f"2027 校园招聘数据分析岗 {key}",
        "city": "上海", "industry": "科技", "official_url": f"https://careers.example.com/campus/{key}",
        "canonical_url": f"https://careers.example.com/campus/{key}", "source": "本地测试公开线索",
        "requirements": "面向2027届毕业生。", "tags": ["校园招聘"], "evidence": [],
        "incoming_status": "open", "payload_hash": digest,
    }


def test_repeated_external_ids_preserve_sequential_merges_events_and_identity(batch_service):
    first = raw_job("same-role", requirements="面向2027届毕业生；原要求。")
    changed = {**first, "requirements": "面向2027届毕业生；新要求。"}
    result = process(batch_service, [first, changed, changed])
    assert (result["new_jobs"], result["updated_jobs"], result["unchanged_jobs"]) == (1, 1, 1)
    assert counts(batch_service) == {"radar_jobs": 1, "job_sources": 1, "radar_events": 2}
    persisted = rows(batch_service, "SELECT * FROM radar_jobs")[0]
    assert persisted["requirements"] == normalize_job(changed)["requirements"]
    first_id, first_seen = persisted["id"], persisted["first_seen_at"]
    process(batch_service, [changed], source_id="official-test")
    discovery = {**changed, "title": "2027 校园招聘来源说法冲突", "verification_status": "pending"}
    process(batch_service, [discovery])
    persisted = rows(batch_service, "SELECT * FROM radar_jobs")[0]
    assert persisted["id"] == first_id and persisted["first_seen_at"] == first_seen
    assert persisted["title"] == normalize_job(changed)["title"] and persisted["verification_status"] == "verified"


def test_failed_batch_rolls_back_all_rows_before_isolated_fallback(batch_service, monkeypatch):
    service = batch_service.service
    process(batch_service, [raw_job("old-protected")])
    original_flush = service.repository.flush_job_batch
    original_upsert = service._upsert_job

    def fail_bulk(connection, mutations, **kwargs):
        original_flush(connection, mutations, **kwargs)
        raise sqlite3.IntegrityError("synthetic batch failure after row/source/event writes")

    def fail_one(connection, **kwargs):
        outcome = original_upsert(connection, **kwargs)
        if kwargs["item"]["external_id"] == "invalid-row":
            raise sqlite3.IntegrityError("synthetic isolated row failure")
        return outcome

    monkeypatch.setattr(service.repository, "flush_job_batch", fail_bulk)
    monkeypatch.setattr(service, "_upsert_job", fail_one)
    result = process(batch_service, [raw_job("good-a"), raw_job("invalid-row"), raw_job("good-b")], complete=True)
    assert result["status"] == "partial_success" and result["new_jobs"] == 2
    assert len(result["errors"]) == 1
    assert counts(batch_service) == {"radar_jobs": 3, "job_sources": 3, "radar_events": 3}
    assert not rows(batch_service, "SELECT id FROM radar_jobs WHERE external_id='invalid-row'")
    assert rows(batch_service, "SELECT js.active, js.missing_successes FROM job_sources js JOIN radar_jobs j ON j.id=js.job_id WHERE j.external_id='old-protected'") == [
        {"active": 1, "missing_successes": 0},
    ]


def test_operational_failure_preserves_committed_counters_and_never_closes_missing(batch_service, monkeypatch):
    from backend.future_radar import service as service_module

    service = batch_service.service
    process(batch_service, [raw_job("keep-existing")])
    monkeypatch.setattr(service_module, "RESULT_WRITE_BATCH_SIZE", 2)
    original = service._upsert_job_batch
    batch_calls = []

    def fail_second(connection, **kwargs):
        batch_calls.append(1)
        if len(batch_calls) == 2:
            raise sqlite3.OperationalError("synthetic interrupted connection")
        return original(connection, **kwargs)

    monkeypatch.setattr(service, "_upsert_job_batch", fail_second)
    with pytest.raises(RadarPartialWriteError) as failure:
        process(batch_service, [raw_job(str(index)) for index in range(5)], complete=True)
    assert failure.value.committed_summary["new_jobs"] == 2
    assert counts(batch_service) == {"radar_jobs": 3, "job_sources": 3, "radar_events": 3}
    assert rows(batch_service, "SELECT DISTINCT active, missing_successes FROM job_sources") == [{"active": 1, "missing_successes": 0}]


def test_takeover_between_batches_stops_old_worker_and_keeps_real_run_counts(batch_service, monkeypatch):
    from backend.future_radar import service as service_module

    service = batch_service.service
    process(batch_service, [raw_job("keep-existing")])
    monkeypatch.setattr(service_module, "RESULT_WRITE_BATCH_SIZE", 2)
    observed = AdapterResult(jobs=[raw_job(str(index)) for index in range(5)], snapshot_complete=True)
    service.adapter_factory = lambda source: SimpleNamespace(scan=lambda current: copy.deepcopy(observed))
    original_transaction = service._result_transaction
    count = []

    @contextmanager
    def steal_after_committed_batch(guard, summary):
        with original_transaction(guard, summary) as connection:
            yield connection
        count.append(1)
        if len(count) == 1:
            with batch_service.connect() as other:
                other.execute(
                    "UPDATE radar_locks SET owner=?, expires_at=? WHERE lock_name=?",
                    ("replacement-worker", (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                     f"future-radar-source:{SOURCE}"),
                )

    monkeypatch.setattr(service, "_result_transaction", steal_after_committed_batch)
    run = service.run(scan_type="quick", source_ids=[SOURCE])
    assert run["status"] == "failed" and run["sources_failed"] == 1 and run["new_jobs"] == 2
    assert counts(batch_service) == {"radar_jobs": 3, "job_sources": 3, "radar_events": 3}
    assert rows(batch_service, "SELECT DISTINCT active, missing_successes FROM job_sources") == [{"active": 1, "missing_successes": 0}]
    assert rows(batch_service, "SELECT owner FROM radar_locks WHERE lock_name=?", (f"future-radar-source:{SOURCE}",)) == [{"owner": "replacement-worker"}]
    assert service.repository.get_source(SOURCE)["status"] == "error"


def test_complete_missing_retirement_is_batched_and_other_active_source_protects_job(batch_service, monkeypatch):
    from backend.future_radar import service as service_module

    monkeypatch.setattr(service_module, "RESULT_WRITE_BATCH_SIZE", 5)
    jobs = [raw_job(f"missing-{index}") for index in range(31)]
    process(batch_service, jobs)
    process(batch_service, [jobs[0]], source_id="official-test")
    process(batch_service, [], complete=True)
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM radar_jobs WHERE status='open'")[0]["count"] == 31
    second = process(batch_service, [], complete=True)
    assert second["closed_jobs"] == 30
    assert rows(batch_service, "SELECT external_id FROM radar_jobs WHERE status='open'") == [{"external_id": "missing-0"}]
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM job_sources WHERE source_id=? AND active=1", (SOURCE,))[0]["count"] == 0
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM radar_events WHERE event_type='CLOSED'")[0]["count"] == 30


def test_scoped_bridge_does_not_advance_full_snapshot_due_or_health_baseline(batch_service):
    service = batch_service.service
    database.upsert_recruitment_ingest_candidate(candidate("baseline"))
    assert service.run(scan_type="quick", source_ids=[SOURCE])["new_jobs"] == 1
    with batch_service.connect() as connection:
        connection.execute(
            "UPDATE monitor_sources SET last_checked_at='2000-01-01T00:00:00+00:00', "
            "last_success_at='2000-01-01T00:00:00+00:00', interval_minutes=120 WHERE id=?", (SOURCE,),
        )
    registry = rows(batch_service, "SELECT * FROM monitor_sources WHERE id=?", (SOURCE,))
    snapshots = rows(batch_service, "SELECT * FROM radar_source_snapshots WHERE source_id=? ORDER BY id", (SOURCE,))
    new = candidate("small-projection")
    database.upsert_recruitment_ingest_candidate(new)
    for _ in range(2):
        result = service.run(scan_type="quick", source_ids=[SOURCE], bridge_candidate_ids=[new["id"]])
        assert result["status"] == "success"
    assert rows(batch_service, "SELECT * FROM monitor_sources WHERE id=?", (SOURCE,)) == registry
    assert rows(batch_service, "SELECT * FROM radar_source_snapshots WHERE source_id=? ORDER BY id", (SOURCE,)) == snapshots
    assert SOURCE in {item["id"] for item in service.repository.due_sources()}

    def fail_scan(source):
        raise RuntimeError("synthetic local projection failure")

    original_adapter = service.adapter_factory
    service.adapter_factory = lambda source: SimpleNamespace(scan=fail_scan)
    failure = service.run(scan_type="quick", source_ids=[SOURCE], bridge_candidate_ids=[new["id"]])
    assert failure["status"] == "failed" and failure["sources_failed"] == 1
    assert rows(batch_service, "SELECT * FROM monitor_sources WHERE id=?", (SOURCE,)) == registry
    assert rows(batch_service, "SELECT * FROM radar_source_snapshots WHERE source_id=? ORDER BY id", (SOURCE,)) == snapshots
    assert SOURCE in {item["id"] for item in service.repository.due_sources()}
    service.adapter_factory = original_adapter
    full = service.run(scan_type="quick", source_ids=[SOURCE])
    assert full["status"] == "success" and full["unchanged_jobs"] == 2
    assert SOURCE not in {item["id"] for item in service.repository.due_sources()}
    assert len(rows(batch_service, "SELECT * FROM radar_source_snapshots WHERE source_id=?", (SOURCE,))) == len(snapshots) + 1


def test_structure_failure_marks_source_partial_without_false_company_coverage_error(batch_service):
    service = batch_service.service
    service.adapter_factory = lambda source: SimpleNamespace(scan=lambda source: AdapterResult(
        jobs=[raw_job("valid-row"), {"company": "示例科技"}],
        content_hash="synthetic-partial-hash", normalized_content="public fixture", snapshot_complete=True,
    ))
    result = service.run(scan_type="quick", source_ids=[SOURCE])
    assert result["status"] == "partial_success" and result["new_jobs"] == 1
    assert service.repository.get_source(SOURCE)["status"] == "partial"
    assert {error["code"] for error in result["errors"]} == {"JOB_REJECTED"}
    assert service.repository.latest_snapshot_metadata(SOURCE)["metadata"]["status"] == "partial"


def test_real_pg_rtt_batches_allow_concurrent_ten_candidate_ingest(batch_service, monkeypatch):
    if batch_service.backend != "postgres":
        pytest.skip("Actual PostgreSQL pipeline/RTT and advisory-lock contention test")
    import psycopg
    from backend import main

    service = batch_service.service
    for index in range(75):
        database.upsert_recruitment_ingest_candidate(candidate(f"existing-{index}"))
    baseline = service.run(scan_type="quick", source_ids=[SOURCE])
    assert baseline["new_jobs"] == 75
    local = threading.local()
    batch_started = threading.Event()
    round_trips = Counter()
    original_execute = psycopg.Cursor.execute
    original_many = psycopg.Cursor.executemany
    original_process = service.process_result
    original_batch = service._upsert_job_batch

    def delayed_execute(cursor, query, params=None, **kwargs):
        if getattr(local, "slow", False):
            round_trips["execute"] += 1
            time.sleep(0.1)
        return original_execute(cursor, query, params, **kwargs)

    def delayed_many(cursor, query, params_seq, **kwargs):
        if getattr(local, "slow", False):
            round_trips["pipeline_flush"] += 1
            time.sleep(0.1)
        return original_many(cursor, query, params_seq, **kwargs)

    def processing(**kwargs):
        local.slow = True
        try:
            return original_process(**kwargs)
        finally:
            local.slow = False

    def in_batch(connection, **kwargs):
        # This runs after the current batch acquired its real PG write lock.
        batch_started.set()
        return original_batch(connection, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", delayed_execute)
    monkeypatch.setattr(psycopg.Cursor, "executemany", delayed_many)
    monkeypatch.setattr(service, "process_result", processing)
    monkeypatch.setattr(service, "_upsert_job_batch", in_batch)
    monkeypatch.setattr(main, "future_radar_service", service)
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda stored: (
        "pending", "offline_fixture_not_verified", {"opening_date": None, "closing_date": None},
    ))
    original_connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *, timeout=3: original_connect(timeout=timeout))
    payload = main.RecruitmentIngestRequest(source_id="chatgpt-radar-03", jobs=[
        {"source_item_id": f"new-{index}", "external_id": f"new-{index}",
         "company": "示例科技", "title": f"2027 校园招聘数据分析岗 new-{index}",
         "city": "上海", "employer_type": "互联网企业", "industry": "科技",
         "official_url": f"https://careers.example.com/campus/new-{index}",
         "requirements": "面向2027届毕业生。", "tags": ["校园招聘"]}
        for index in range(10)
    ])
    with ThreadPoolExecutor(max_workers=2) as executor:
        scanning = executor.submit(service.run, scan_type="quick", source_ids=[SOURCE])
        assert batch_started.wait(3)
        started = time.perf_counter()
        # Real app ingest/database code, but no HTTP/auth/official fetch. Every
        # acquisition must fit a 3s backend lock limit, not wait for the whole
        # 75-row source (the old loop needs about 300+ RTT commands).
        response = main.ingest_recruitment_jobs(payload, None)
        ingest_elapsed = time.perf_counter() - started
        complete = scanning.result(timeout=20)
    assert complete["status"] == "success" and complete["unchanged_jobs"] == 75
    assert response["received"] == response["new"] == response["pending"] == 10
    assert ingest_elapsed < 15
    assert round_trips["execute"] + round_trips["pipeline_flush"] < 90
    # A normal run may already have completed before ingest's small bridge;
    # otherwise explicitly finish the deferred ten-ID deterministic projection.
    local.slow = False
    monkeypatch.setattr(service, "process_result", original_process)
    ids = [row["id"] for row in rows(batch_service, "SELECT id FROM recruitment_ingest_candidates WHERE source_item_id LIKE 'new-%'")]
    replay = service.run(scan_type="quick", source_ids=[SOURCE], bridge_candidate_ids=ids)
    assert replay["status"] == "success"
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM radar_jobs")[0]["count"] == 85
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM job_sources")[0]["count"] == 85
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM radar_events WHERE event_type='NEW'")[0]["count"] == 85
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM radar_locks")[0]["count"] == 0
    print(f"isolated-pg: 75-row unchanged scan with 100ms RTT; commands={dict(round_trips)}; concurrent ten-row ingest={ingest_elapsed:.2f}s; SQL lock budget=3s")


def test_real_pg_large_2701_snapshot_is_bounded_complete_and_pipelined(batch_service, monkeypatch):
    if batch_service.backend != "postgres":
        pytest.skip("Actual PostgreSQL executemany pipeline command count")
    import psycopg

    service = batch_service.service
    payload = AdapterResult(jobs=[raw_job(f"large-{index}") for index in range(2701)], snapshot_complete=True)
    service.adapter_factory = lambda source: SimpleNamespace(scan=lambda current: copy.deepcopy(payload))
    local = threading.local()
    commands = Counter()
    original_execute = psycopg.Cursor.execute
    original_many = psycopg.Cursor.executemany
    original_process = service.process_result
    original_batch = service._upsert_job_batch
    batch_sizes = []

    def counted_execute(cursor, query, params=None, **kwargs):
        if getattr(local, "processing", False):
            commands["execute"] += 1
        return original_execute(cursor, query, params, **kwargs)

    def counted_many(cursor, query, params_seq, **kwargs):
        if getattr(local, "processing", False):
            commands["pipeline_flush"] += 1
        return original_many(cursor, query, params_seq, **kwargs)

    def processing(**kwargs):
        local.processing = True
        try:
            return original_process(**kwargs)
        finally:
            local.processing = False

    def batch(connection, **kwargs):
        batch_sizes.append(len(kwargs["batch"]))
        return original_batch(connection, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counted_execute)
    monkeypatch.setattr(psycopg.Cursor, "executemany", counted_many)
    monkeypatch.setattr(service, "process_result", processing)
    monkeypatch.setattr(service, "_upsert_job_batch", batch)
    started = time.perf_counter()
    first = service.run(scan_type="quick", source_ids=[SOURCE])
    first_seconds, first_commands = time.perf_counter() - started, dict(commands)
    assert first["status"] == "success" and first["new_jobs"] == 2701
    assert max(batch_sizes) == 25 and len(batch_sizes) == 109
    assert counts(batch_service) == {"radar_jobs": 2701, "job_sources": 2701, "radar_events": 2701}
    commands.clear()
    batch_sizes.clear()
    started = time.perf_counter()
    unchanged = service.run(scan_type="quick", source_ids=[SOURCE])
    unchanged_seconds = time.perf_counter() - started
    assert unchanged["status"] == "success" and unchanged["unchanged_jobs"] == 2701
    assert unchanged["new_jobs"] == unchanged["updated_jobs"] == unchanged["closed_jobs"] == 0
    assert max(batch_sizes) == 25 and len(batch_sizes) == 109
    assert counts(batch_service) == {"radar_jobs": 2701, "job_sources": 2701, "radar_events": 2701}
    assert sum(commands.values()) < 1300
    assert rows(batch_service, "SELECT COUNT(*) AS count FROM radar_locks")[0]["count"] == 0
    print(
        f"isolated-pg 2701 jobs: new={first_seconds:.2f}s commands={first_commands}; "
        f"unchanged={unchanged_seconds:.2f}s commands={dict(commands)}; 109 bounded transactions max25rows; "
        "no simulated delay in this throughput measurement"
    )
