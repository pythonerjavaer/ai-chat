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
    scan_scopes = []

    def adapter_factory(source):
        # The endpoint must not invoke external adapters or a broad scan.
        assert source["id"] == BRIDGE_SOURCE
        scans.append(source["id"])
        scan_scopes.append(source["adapter_config"].get("candidate_ids"))
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
        yield SimpleNamespace(client=client, service=service, scans=scans, scan_scopes=scan_scopes, bearer=bearer)


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
    main_pool = harness.client.get(
        "/api/future-radar/opportunities", headers=harness.bearer
    )
    assert main_pool.status_code == 200
    assert main_pool.json()["total"] == 1
    assert main_pool.json()["items"][0]["verification_status"] == "pending"
    assert main_pool.json()["items"][0]["available_in_main_pool"] is True
    assert main_pool.json()["items"][0]["officially_verified"] is False
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


def test_ingest_bridge_passes_only_current_batch_ids_and_does_not_retouch_old_pool(harness):
    first = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload("first-role"))
    assert first.status_code == 200
    first_id = candidate_rows()[0]["id"]
    with database.connect() as connection:
        old_row = dict(connection.execute("SELECT * FROM radar_jobs").fetchone())
        old_link = dict(connection.execute("SELECT * FROM job_sources").fetchone())
    second = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=payload("second-role"))
    assert second.status_code == 200 and second.json()["search_updates_refresh"]["status"] == "success"
    second_id = next(row["id"] for row in candidate_rows() if row["id"] != first_id)
    assert harness.scan_scopes == [[first_id], [second_id]]
    with database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM radar_jobs WHERE id=?", (old_row["id"],)).fetchone()) == old_row
        assert dict(connection.execute("SELECT * FROM job_sources WHERE job_id=?", (old_row["id"],)).fetchone()) == old_link
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 2
    assert "candidate_ids" not in harness.service.repository.get_source(BRIDGE_SOURCE)["adapter_config"]


def test_bridge_scope_includes_duplicate_updated_stale_rejected_and_closed_candidates(harness, monkeypatch):
    current = {**payload(), "source_updated_at": "2026-08-30T10:00:00Z"}
    requests = [current, current]
    updated = {**current, "jobs": [{**current["jobs"][0], "requirements": "面向2027届应届毕业生。"}]}
    requests.extend([updated, {**updated, "source_updated_at": "2026-08-29T10:00:00Z"}])
    for request in requests:
        response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=request)
        assert response.status_code == 200
    assert response.json()["stale"] == 1
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "rejected", "not_campus", {"opening_date": None, "closing_date": None},
    ))
    rejected = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=updated)
    assert rejected.status_code == 200 and rejected.json()["rejected"] == 1
    closed_request = {**updated, "jobs": [{**updated["jobs"][0], "status": "closed"}]}
    closed = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=closed_request)
    assert closed.status_code == 200 and closed.json()["closed"] == 1
    candidate_id = candidate_rows()[0]["id"]
    assert harness.scan_scopes == [[candidate_id]] * 6


def test_ingest_transport_limit_rejects_more_than_one_hundred_before_creating_a_bridge(harness):
    request = {"source_id": "chatgpt-radar-01", "jobs": [payload(f"role-{index}")["jobs"][0] for index in range(101)]}
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=request)
    assert response.status_code == 422
    assert harness.scans == [] and candidate_rows() == []


def test_full_hundred_job_chunk_and_following_chunk_are_both_fully_projected(harness):
    for start, count in ((0, 100), (100, 13)):
        jobs = [{**payload(f"bulk-role-{index}")["jobs"][0],
                 "title": f"2027 校园招聘数据分析岗 方向{index}"}
                for index in range(start, start + count)]
        response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json={
            "source_id": "chatgpt-radar-01", "jobs": jobs,
        })
        assert response.status_code == 200, response.text
        assert response.json()["received"] == response.json()["pending"] == count
        assert response.json()["search_updates_refresh"] == {"status": "success"}
        assert len(harness.scan_scopes[-1]) == count
    assert len(candidate_rows()) == 113
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 113
    pool = harness.client.get("/api/future-radar/opportunities", headers=harness.bearer, params={
        "priority_only": "false", "balanced_only": "false", "page_size": 100,
    })
    assert pool.status_code == 200 and pool.json()["total"] == 113
