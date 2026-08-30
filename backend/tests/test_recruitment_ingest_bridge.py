"""Authorized legacy ingest should immediately refresh its local search pool."""

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_INGEST_TOKEN", "test-recruitment-ingest-token")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from fastapi.testclient import TestClient

from backend import database, main
from backend.future_radar.adapters import LegacyDatabaseAdapter
from backend.future_radar.service import FutureRadarService


BRIDGE_SOURCE = "legacy-search-discovery"
INGEST_HEADERS = {"X-Recruitment-Token": "test-recruitment-ingest-token"}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "settings", SimpleNamespace(
        database_path=tmp_path / "ingest-bridge.db"
    ))
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
        "recruitment_ingest_token": "test-recruitment-ingest-token",
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    scans = []

    def adapter_factory(source):
        # The endpoint must not invoke external adapters or a broad scan.
        assert source["id"] == BRIDGE_SOURCE
        scans.append(source["id"])
        return LegacyDatabaseAdapter()

    service = FutureRadarService(
        connect=database.connect,
        openai_api_key="test-key",
        ai_model="test-model",
        web_search_enabled=False,
        adapter_factory=adapter_factory,
    )
    monkeypatch.setattr(main, "future_radar_service", service)
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "pending", "temporarily_unreadable",
        {"opening_date": None, "closing_date": None},
    ))
    with TestClient(main.app) as client:
        registered = client.post("/api/auth/register", json={
            "username": "ingest-bridge-user",
            "password": "correct-horse-123",
            "privacy_accepted": True,
        })
        assert registered.status_code == 201
        bearer = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        yield SimpleNamespace(client=client, service=service, scans=scans, bearer=bearer)


def payload(key="pending-role"):
    return {
        "source_id": "chatgpt-radar-01",
        "jobs": [{
            "external_id": key,
            "company": "示例科技",
            "title": "2027 校园招聘数据分析岗",
            "city": "上海",
            "employer_type": "互联网企业",
            "industry": "科技",
            "official_url": f"https://careers.example.com/campus/{key}",
            "requirements": "面向应届毕业生。",
            "tags": ["校园招聘", "internet_tech"],
        }],
    }


def candidate_rows():
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT id, verification_status, promoted_job_id FROM recruitment_ingest_candidates"
        ).fetchall()]


def test_pending_ingest_is_immediately_visible_in_new_pool_without_external_scan(harness):
    response = harness.client.post(
        "/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload()
    )
    assert response.status_code == 200
    assert response.json()["pending"] == 1
    assert response.json()["search_updates_refresh"] == {"status": "success"}
    assert harness.scans == [BRIDGE_SOURCE]
    pool = harness.client.get("/api/future-radar/search-updates", headers=harness.bearer)
    assert pool.status_code == 200
    assert pool.json()["total"] == 1
    assert pool.json()["items"][0]["verification_status"] == "pending"
    assert harness.client.get(
        "/api/future-radar/jobs", headers=harness.bearer
    ).json()["total"] == 0
    runs = harness.service.repository.list_runs()["items"]
    assert len(runs) == 1
    assert runs[0]["trigger_type"] == "ingest_bridge"
    assert runs[0]["source_ids"] == [BRIDGE_SOURCE]
    assert runs[0]["ai_calls"] == 0


def test_ingest_authentication_and_verified_promotion_remain_intact(harness, monkeypatch):
    unauthorized = harness.client.post("/api/recruitment/ingest", json=payload())
    assert unauthorized.status_code == 401
    assert candidate_rows() == []
    assert harness.scans == []
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "verified", None,
        {"opening_date": "2026-08-01", "closing_date": "2099-09-01"},
    ))
    response = harness.client.post(
        "/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload("verified-role")
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["search_updates_refresh"]["status"] == "success"
    stored = candidate_rows()[0]
    assert stored["verification_status"] == "verified"
    assert stored["promoted_job_id"]
    assert any(job["id"] == stored["promoted_job_id"] for job in database.list_recruitment_jobs())


def test_active_quick_run_defers_bridge_without_duplicate_run_or_lost_ingest(harness):
    assert harness.service.repository.acquire_lock(
        "future-radar-run:quick", "another-quick-run", ttl_seconds=60
    )
    try:
        response = harness.client.post(
            "/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload()
        )
        assert response.status_code == 200
        assert response.json()["pending"] == 1
        assert response.json()["search_updates_refresh"] == {
            "status": "deferred", "code": "RADAR_RUN_BUSY"
        }
        assert len(candidate_rows()) == 1
        assert harness.scans == []
        assert harness.service.repository.list_runs()["total"] == 0
    finally:
        harness.service.repository.release_lock("future-radar-run:quick", "another-quick-run")
    resumed = harness.service.run(scan_type="quick", source_ids=[BRIDGE_SOURCE])
    assert resumed["new_jobs"] == 1


def test_active_source_lock_is_not_bypassed(harness):
    assert harness.service.repository.acquire_lock(
        f"future-radar-source:{BRIDGE_SOURCE}", "another-source-scan", ttl_seconds=60
    )
    try:
        response = harness.client.post(
            "/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload()
        )
        assert response.status_code == 200
        assert response.json()["search_updates_refresh"] == {
            "status": "deferred", "code": "BRIDGE_NOT_COMPLETED"
        }
        assert len(candidate_rows()) == 1
        assert harness.scans == []
        run = harness.service.repository.list_runs()["items"][0]
        assert run["status"] == "skipped"
        assert run["sources_skipped"] == 1
    finally:
        harness.service.repository.release_lock(
            f"future-radar-source:{BRIDGE_SOURCE}", "another-source-scan"
        )


def test_empty_heartbeat_does_not_run_the_bridge(harness):
    response = harness.client.post(
        "/api/recruitment/ingest", headers=INGEST_HEADERS,
        json={"source_id": "chatgpt-radar-01", "jobs": []},
    )
    assert response.status_code == 200
    assert response.json()["received"] == 0
    assert "search_updates_refresh" not in response.json()
    assert response.json()["event_ids"]
    assert harness.scans == []
    assert harness.service.repository.list_runs()["total"] == 0


def test_bridge_failure_never_turns_committed_ingest_into_failure(harness, monkeypatch):
    def fail(**_kwargs):
        raise RuntimeError("PRIVATE_PROVIDER_MESSAGE_DO_NOT_ECHO")

    monkeypatch.setattr(harness.service, "run", fail)
    response = harness.client.post(
        "/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload()
    )
    assert response.status_code == 200
    assert response.json()["pending"] == 1
    assert response.json()["search_updates_refresh"] == {
        "status": "deferred", "code": "BRIDGE_UNAVAILABLE"
    }
    assert "PRIVATE_PROVIDER_MESSAGE" not in response.text
    assert len(candidate_rows()) == 1
