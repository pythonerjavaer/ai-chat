import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_INGEST_TOKEN", "legacy-ingest-token")
os.environ.setdefault("FUTURE_RADAR_SYNC_TOKEN", "future-radar-sync-token")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from fastapi.testclient import TestClient

from backend import database, main
from backend.chatgpt_monitor_ingestion import ChatGPTMonitorIngestionService, RetryableIngestionBusy
from backend.future_radar.schemas import FrostFireSyncV1
from backend.future_radar.service import FutureRadarService


@pytest.fixture
def harness(tmp_path, monkeypatch):
    db_path = tmp_path / "chatgpt-monitor.db"
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=db_path))
    database.init_db()
    radar = FutureRadarService(
        connect=database.connect,
        openai_api_key="",
        ai_model="unused",
        web_search_enabled=False,
        max_workers=1,
    )
    service = ChatGPTMonitorIngestionService(radar=radar, connect=database.connect)
    return SimpleNamespace(radar=radar, service=service, db_path=db_path)


def payload(*, run_id="monitor-run-1", external_id="ats-2027-001", **changes):
    job = {
        "external_id": external_id,
        "company": "示例银行",
        "job_title": "2027校园招聘数据产品经理",
        "location": "上海",
        "application_url": f"https://careers.example.com/jobs/{external_id}",
        "source_url": f"https://careers.example.com/jobs/{external_id}",
        "source_type": "ats",
        "event_type": "NEW",
        "description": "负责校园招聘数据产品规划、业务分析和跨团队协作。",
        "eligibility": "面向2027届毕业生，专业不限。",
        "tier": "T0",
        "score": 100,
        "recommendation": "来源建议立即申请",
    }
    job.update(changes)
    return {
        "version": "FROSTFIRE_SYNC_V1",
        "source_id": "chatgpt-monitor-test",
        "source_thread_id": "private-chat-thread-id",
        "monitor_run_id": run_id,
        "generated_at": "2026-09-22T00:00:00Z",
        "monitor_name": "测试监控",
        "jobs": [job],
    }


def rows(table):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]


def test_new_job_and_identical_replay_are_idempotent(harness):
    first = harness.service.ingest(payload())
    second = harness.service.ingest(payload())
    assert first["jobs_created"] == 1
    assert second["idempotent_replay"] is True
    assert len(rows("radar_jobs")) == 1
    assert len(rows("monitor_ingestion_runs")) == 1
    assert len(rows("monitor_ingestion_items")) == 1
    assert "private-chat-thread-id" not in rows("monitor_ingestion_runs")[0]["raw_payload"]


def test_source_tier_is_audited_but_never_overrides_system_tier(harness):
    result = harness.service.ingest(payload())
    item = rows("monitor_ingestion_items")[0]
    assert result["rules_version"]
    assert item["rules_version"] == result["rules_version"]
    assert item["tier_score"] != 100
    assert item["final_tier"] != "T0"
    assert '"tier":"T0"' in item["raw_item"]
    assert rows("radar_jobs")[0]["source_ratings"] == "[]"


def test_same_external_id_deadline_change_creates_specific_event(harness):
    harness.service.ingest(payload(run_id="first", deadline="2026-10-01"))
    changed = harness.service.ingest(payload(run_id="second", deadline="2026-10-15"))
    assert changed["jobs_updated"] == 1
    events = rows("radar_events")
    assert events[-1]["event_type"] == "DEADLINE_CHANGED"
    assert "closing_date" in events[-1]["changed_fields"]


def test_same_url_with_changed_title_reuses_existing_job(harness):
    original = payload(run_id="first", external_id=None)
    original["jobs"][0].pop("external_id", None)
    harness.service.ingest(original)
    changed = deepcopy(original)
    changed["monitor_run_id"] = "second"
    changed["jobs"][0]["job_title"] = "2027校园招聘高级数据产品经理"
    result = harness.service.ingest(changed)
    assert result["jobs_updated"] == 1
    assert len(rows("radar_jobs")) == 1


def test_roundup_becomes_lead_and_t3_jobs_are_not_filtered(harness):
    roundup = payload(job_title="2027届各大银行秋招汇总表")
    lead = harness.service.ingest(roundup)
    assert lead["leads_created"] == 1
    assert lead["jobs_created"] == 0
    assert rows("monitor_ingestion_items")[0]["processing_status"] == "lead"
    assert len(rows("recruitment_monitor_leads")) == 1


def test_closed_and_reopened_events_update_existing_job(harness):
    harness.service.ingest(payload(run_id="open"))
    closed = harness.service.ingest(payload(run_id="closed", event_type="CLOSED"))
    reopened = harness.service.ingest(payload(run_id="reopened", event_type="REOPENED"))
    assert closed["closed"] == 0  # discovery sources cannot close verified/open facts
    assert reopened["reopened"] == 0
    assert len(rows("radar_jobs")) == 1


def test_invalid_payload_and_concurrent_duplicate_submission(harness):
    with pytest.raises(ValidationError):
        FrostFireSyncV1.model_validate({"version": "FROSTFIRE_SYNC_V1", "source_id": "bad", "jobs": [{"company": "X"}]})
    data = payload(run_id="concurrent")
    def submit_once():
        try:
            return harness.service.ingest(deepcopy(data))
        except RetryableIngestionBusy:
            return {"status": "busy"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit_once(), range(2)))
    assert len(rows("radar_jobs")) == 1
    assert any(result.get("idempotent_replay") or result.get("status") == "busy" for result in results)


def test_rest_fallback_requires_bearer_and_uses_shared_service(harness, monkeypatch):
    monkeypatch.setattr(main, "chatgpt_monitor_ingestion_service", harness.service)
    values = vars(main.settings).copy()
    values.update({
        "database_path": harness.db_path,
        "database_backend": "sqlite",
        "future_radar_sync_token": "future-radar-sync-token",
        "recruitment_ingest_token": "legacy-ingest-token",
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**values))
    with TestClient(main.app) as client:
        assert client.post("/api/integrations/chatgpt-monitor/sync", json=payload()).status_code == 401
        response = client.post(
            "/api/integrations/chatgpt-monitor/sync",
            headers={"Authorization": "Bearer future-radar-sync-token"},
            json=payload(run_id="rest"),
        )
    assert response.status_code == 200
    assert response.json()["jobs_created"] == 1



def test_success_updates_per_monitor_watermark(harness):
    result = harness.service.ingest(payload(run_id="watermark"))
    assert result["jobs_created"] == 1
    marks = rows("monitor_ingestion_watermarks")
    assert len(marks) == 1
    mark = marks[0]
    assert mark["source_id"] == "chatgpt-monitor-test"
    assert mark["last_successful_run_id"] == result["run_id"]
    assert mark["last_successful_event_id"] >= 1
    assert mark["recovery_status"] == "normal"
    assert mark["pending_backfill"] == 0


def test_retry_after_failed_run_reuses_bridge_id_and_does_not_duplicate(harness, monkeypatch):
    original_sync = harness.service.radar.sync
    attempts = {"count": 0}

    def flaky_sync(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("database unavailable")
        return original_sync(*args, **kwargs)

    monkeypatch.setattr(harness.service.radar, "sync", flaky_sync)
    data = payload(run_id="retryable-run")
    with pytest.raises(RuntimeError):
        harness.service.ingest(data, idempotency_key="bridge-retry-1")
    failed = rows("monitor_ingestion_runs")[0]
    assert failed["processing_status"] == "failed"
    marks = rows("monitor_ingestion_watermarks")
    assert marks[0]["recovery_status"] == "interrupted"
    assert marks[0]["pending_backfill"] == 1

    result = harness.service.ingest(data, idempotency_key="bridge-retry-1")
    assert result["jobs_created"] == 1
    assert len(rows("radar_jobs")) == 1
    assert len(rows("monitor_ingestion_runs")) == 1
    # Transport recovery does not prove the public-source interruption window
    # was covered. The automatic backfill coordinator clears this only after
    # every mapped official/ATS source succeeds.
    mark = rows("monitor_ingestion_watermarks")[0]
    assert mark["recovery_status"] == "backfill_pending"
    assert mark["pending_backfill"] == 1


def test_fresh_processing_replay_is_retryable_busy(harness):
    data = payload(run_id="busy-run")
    canonical = __import__("json").dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run, _ = harness.service._create_or_update_run(
        run_id="busy-id", key="chatgpt-monitor:chatgpt-monitor-test:busy-key",
        payload=data, payload_hash=__import__("hashlib").sha256(canonical.encode()).hexdigest(),
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE monitor_ingestion_runs SET processing_status='processing',updated_at=datetime('now') WHERE id=?",
            (run["id"],),
        )
    with pytest.raises(RetryableIngestionBusy):
        harness.service.ingest(data, idempotency_key="busy-key")


def test_stale_processing_run_is_reprocessed_without_duplicate(harness):
    data = payload(run_id="stale-run")
    canonical = __import__("json").dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = __import__("hashlib").sha256(canonical.encode()).hexdigest()
    run, _ = harness.service._create_or_update_run(
        run_id="stale-id", key="chatgpt-monitor:chatgpt-monitor-test:stale-key",
        payload=data, payload_hash=payload_hash,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE monitor_ingestion_runs SET processing_status='processing',updated_at='2026-09-20T00:00:00+00:00' WHERE id=?",
            (run["id"],),
        )
    result = harness.service.ingest(data, idempotency_key="stale-key")
    assert result["jobs_created"] == 1
    replay = harness.service.ingest(data, idempotency_key="stale-key")
    assert replay["idempotent_replay"] is True
    assert len(rows("radar_jobs")) == 1


def test_recovery_status_reports_runs_items_and_watermarks(harness):
    harness.service.ingest(payload(run_id="status-run"))
    status = harness.service.recovery_status()
    assert status["runs"]["success"] == 1
    assert status["items"]["job"] == 1
    assert status["watermarks"][0]["source_id"] == "chatgpt-monitor-test"
    assert status["latest_radar_event_id"] >= 1


def test_recovery_status_endpoint_requires_admin_token(harness, monkeypatch):
    harness.service.ingest(payload(run_id="status-api"))
    monkeypatch.setattr(main, "chatgpt_monitor_ingestion_service", harness.service)
    values = vars(main.settings).copy()
    values.update({
        "database_path": harness.db_path,
        "database_backend": "sqlite",
        "future_radar_sync_token": "future-radar-sync-token",
        "recruitment_ingest_token": "legacy-ingest-token",
        "admin_dashboard_token": "admin-token",
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**values))
    with TestClient(main.app) as client:
        assert client.get("/api/integrations/chatgpt-monitor/recovery/status").status_code == 401
        response = client.get(
            "/api/integrations/chatgpt-monitor/recovery/status",
            headers={"X-Admin-Token": "admin-token"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["runs"]["success"] == 1
    assert body["watermarks"][0]["source_id"] == "chatgpt-monitor-test"
