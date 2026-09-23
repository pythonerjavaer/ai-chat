from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import database
from backend.chatgpt_monitor_ingestion import ChatGPTMonitorIngestionService
from backend.future_radar.adapters import AdapterResult
from backend.future_radar.service import FutureRadarService
from backend.source_backfill import SourceBackfillCoordinator, is_replayable_public_source


class StaticAdapter:
    def __init__(self, result):
        self.result = result

    def scan(self, source):
        del source
        return deepcopy(self.result)


class FailingAdapter:
    def scan(self, source):
        del source
        raise RuntimeError("public endpoint unavailable")


def job():
    return {
        "external_id": "example-bank-2027-data",
        "company": "示例银行",
        "title": "2027校园招聘数据产品经理",
        "city": "上海",
        "official_url": "https://careers.example.com/jobs/example-bank-2027-data",
        "application_url": "https://careers.example.com/jobs/example-bank-2027-data",
        "description": "校园招聘数据产品岗位",
        "requirements": "面向2027届",
        "status": "open",
        "verification_status": "verified",
        "confidence_score": 0.95,
    }


def monitor_payload(run_id="recovery-run"):
    item = job()
    item.update(job_title=item.pop("title"), location=item.pop("city"), source_url=item["official_url"])
    return {
        "version": "FROSTFIRE_SYNC_V1",
        "source_id": "chatgpt-radar-02",
        "source_thread_id": "private-thread",
        "monitor_run_id": run_id,
        "generated_at": "2026-09-20T00:00:00Z",
        "monitor_name": "全行业秋招监控",
        "jobs": [item],
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    db_path = tmp_path / "backfill.db"
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=db_path))
    database.init_db()
    current_adapter = {"value": StaticAdapter(AdapterResult(jobs=[job()]))}
    radar = FutureRadarService(
        connect=database.connect, openai_api_key="", ai_model="unused",
        web_search_enabled=False, max_workers=1,
        adapter_factory=lambda source: current_adapter["value"],
    )
    radar.repository.create_source({
        "id": "official-example-bank", "name": "示例银行校招官网", "platform": "official_web",
        "company": "示例银行", "source_type": "official_html",
        "url": "https://careers.example.com/campus", "enabled": True, "trust_level": "verification",
        "verification_status": "verified", "adapter_config": {"adapter": "official_html"},
    })
    ingestion = ChatGPTMonitorIngestionService(radar=radar, connect=database.connect)
    coordinator = SourceBackfillCoordinator(
        connect=database.connect, radar=radar, bridge_settle_minutes=0,
    )
    ingestion.scope_recorder = coordinator.record_scope
    return SimpleNamespace(radar=radar, ingestion=ingestion, coordinator=coordinator,
                           current_adapter=current_adapter)


def table(name):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {name}").fetchall()]


def make_interruption_then_recover(harness, monkeypatch):
    original = harness.ingestion.radar.sync
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.ingestion.radar, "sync", flaky)
    with pytest.raises(RuntimeError):
        harness.ingestion.ingest(monitor_payload(), idempotency_key="bridge-recovery")
    harness.ingestion.ingest(monitor_payload(), idempotency_key="bridge-recovery")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE monitor_ingestion_runs SET received_at=?,updated_at=?", (old, old))
        connection.execute("UPDATE monitor_ingestion_watermarks SET interrupted_until=?,updated_at=?", (old, old))


def test_scope_is_exact_and_private_or_paid_sources_are_not_replayable(harness):
    harness.ingestion.ingest(monitor_payload(run_id="scope"))
    scopes = table("monitor_backfill_source_scopes")
    assert [(x["monitor_source_id"], x["public_source_id"], x["match_reason"]) for x in scopes] == [
        ("chatgpt-radar-02", "official-example-bank", "exact_company")
    ]
    assert is_replayable_public_source(harness.radar.repository.get_source("official-example-bank"))
    assert not is_replayable_public_source({
        "enabled": True, "source_type": "openai_web_search", "url": "https://example.com",
        "adapter_config": {"adapter": "openai_web_search"},
    })


def test_transport_recovery_queues_and_completes_public_backfill(harness, monkeypatch):
    make_interruption_then_recover(harness, monkeypatch)
    mark = table("monitor_ingestion_watermarks")[0]
    assert mark["pending_backfill"] == 1
    assert mark["recovery_status"] == "backfill_pending"
    result = harness.coordinator.run_once()
    assert result["status"] == "success"
    jobs = table("source_backfill_jobs")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "success"
    mark = table("monitor_ingestion_watermarks")[0]
    assert mark["pending_backfill"] == 0
    assert mark["recovery_status"] == "normal"
    assert len(table("radar_jobs")) == 1
    assert harness.coordinator.run_once()["processed"]["attempted"] == 0


def test_long_bridge_silence_infers_public_recovery_window(harness):
    harness.ingestion.ingest(monitor_payload(run_id="before-outage"), idempotency_key="before-outage")
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with database.connect() as connection:
        connection.execute(
            """UPDATE monitor_ingestion_watermarks SET last_successful_ingestion_at=?,
               last_received_at=?,updated_at=? WHERE source_id='chatgpt-radar-02'""",
            (old, old, old),
        )
    harness.ingestion.ingest(monitor_payload(run_id="after-outage"), idempotency_key="after-outage")
    mark = table("monitor_ingestion_watermarks")[0]
    assert mark["pending_backfill"] == 1
    assert mark["recovery_status"] == "backfill_pending"
    assert mark["interrupted_from"] == old
    assert harness.coordinator.run_once()["processed"]["succeeded"] == 1
    assert table("monitor_ingestion_watermarks")[0]["pending_backfill"] == 0


def test_failed_public_source_keeps_window_and_retries_with_backoff(harness, monkeypatch):
    make_interruption_then_recover(harness, monkeypatch)
    harness.current_adapter["value"] = FailingAdapter()
    result = harness.coordinator.run_once()
    assert result["processed"]["retryable"] == 1
    backfill = table("source_backfill_jobs")[0]
    assert backfill["status"] == "retryable"
    assert backfill["attempt_count"] == 1
    assert backfill["next_attempt_at"]
    mark = table("monitor_ingestion_watermarks")[0]
    assert mark["pending_backfill"] == 1
    assert mark["recovery_status"] == "backfill_retryable"


def test_private_only_monitor_is_never_marked_recovered(harness):
    now = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    harness.radar.repository.create_source({
        "id": "chatgpt-radar-09", "name": "私有监控", "platform": "external",
        "source_type": "manual", "enabled": True, "adapter_config": {"adapter": "manual"},
    })
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO monitor_ingestion_watermarks
               (source_id,recovery_status,interrupted_from,interrupted_until,pending_backfill,updated_at)
               VALUES(?,'interrupted',?,?,1,?)""", ("chatgpt-radar-09", now, now, now),
        )
    result = harness.coordinator.run_once()
    assert result["scheduled"]["unsupported"] == 1
    mark = [x for x in table("monitor_ingestion_watermarks") if x["source_id"] == "chatgpt-radar-09"][0]
    assert mark["pending_backfill"] == 1
    assert mark["recovery_status"] == "private_replay_unavailable"


def test_recovery_waits_for_bridge_settlement_after_database_health(harness):
    coordinator = SourceBackfillCoordinator(
        connect=database.connect, radar=harness.radar, bridge_settle_minutes=20,
    )
    assert coordinator.run_once()["status"] == "waiting_for_bridge_backlog"


@pytest.mark.parametrize(
    ("fields", "merged", "expected"),
    [
        (["city"], {"application_url": "https://careers.example.com/apply"}, "LOCATION_CHANGED"),
        (["region"], {"application_url": "https://careers.example.com/apply"}, "LOCATION_CHANGED"),
        (["application_url"], {"application_url": "https://careers.example.com/new"}, "APPLICATION_URL_CHANGED"),
        (["application_url"], {"application_url": ""}, "APPLICATION_DISABLED"),
    ],
)
def test_recovery_change_events_are_specific(fields, merged, expected):
    existing = {"status": "open", "verification_status": "verified"}
    candidate = {**existing, **merged}
    assert FutureRadarService._job_event_type(existing, candidate, fields) == expected


def test_explicit_application_disable_is_not_reduced_to_generic_closed():
    existing = {"status": "open", "verification_status": "verified"}
    merged = {
        **existing,
        "status": "closed",
        "_event_type_hint": "APPLICATION_DISABLED",
    }
    assert FutureRadarService._job_event_type(existing, merged, ["status"]) == "APPLICATION_DISABLED"
