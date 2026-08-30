"""Coverage completion and public metadata regressions without paid requests."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from fastapi.testclient import TestClient

from backend import database, main
from backend.future_radar.adapters import AdapterResult
from backend.future_radar.service import FutureRadarService


SOURCE_ID = "openai-public-web-search"


class StaticCoverageAdapter:
    def __init__(self, result):
        self.result = result

    def scan(self, _source):
        return deepcopy(self.result)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "settings", SimpleNamespace(
        database_path=tmp_path / "radar-coverage.db"
    ))
    database.init_db()
    radar = FutureRadarService(
        connect=database.connect,
        openai_api_key="test-key",
        ai_model="test-model",
        web_search_enabled=True,
    )
    radar.seed_registry()
    return radar


def sample_job(key):
    return {
        "external_id": key,
        "company": "示例科技",
        "title": f"2027 校园招聘 {key}",
        "city": "上海",
        "official_url": f"https://careers.example.com/campus/{key}",
        "status": "open",
        "verification_status": "pending",
        "primary_category": "internet_tech",
    }


def coverage(failed_count=5):
    return {
        "target_count": 205,
        "searched_count": 205 - failed_count,
        "failed_count": failed_count,
        "employers_with_candidates_count": 2,
        "batch_count": 30,
        "failed_batch_count": 1 if failed_count else 0,
        "coverage_percent": round((205 - failed_count) / 205 * 100, 1),
        "failed_employers": ["示例未完成企业"] if failed_count else [],
    }


@pytest.mark.parametrize("normalized_content", ["", "safe coverage summary"])
def test_partial_search_preserves_good_candidates_and_persists_coverage(service, normalized_content):
    result = AdapterResult(
        jobs=[sample_job("first"), sample_job("second")],
        content_hash="partial-batch-content",
        normalized_content=normalized_content,
        snapshot_complete=False,
        status="partial",
        message="PRIVATE_PROVIDER_DIAGNOSTIC_NOT_PUBLIC",
        coverage=coverage(),
        ai_calls=8,
    )
    service.adapter_factory = lambda _source: StaticCoverageAdapter(result)
    run = service.run(scan_type="deep", source_ids=[SOURCE_ID])

    assert run["status"] == "partial_success"
    assert run["sources_succeeded"] == 1
    assert run["sources_failed"] == 0
    assert run["new_jobs"] == 2
    assert run["errors"] == [{
        "source_id": SOURCE_ID,
        "code": "COMPANY_SEARCH_INCOMPLETE",
        "message": "本轮有 5 家企业的搜索未完成；已取得的候选已保留。",
    }]
    assert "PRIVATE_PROVIDER_DIAGNOSTIC" not in json.dumps(run)
    assert service.repository.list_jobs(filters={"discovery_source_only": True})["total"] == 2
    assert service.repository.list_jobs(filters={"verification_status": "verified"})["total"] == 0
    assert service.repository.get_source(SOURCE_ID)["status"] == "partial"
    snapshot = service.repository.latest_snapshot_metadata(SOURCE_ID)
    assert snapshot["metadata"]["status"] == "partial"
    assert snapshot["metadata"]["coverage"] == coverage()
    assert snapshot["metadata"]["jobs"] == 2


def test_failed_coverage_count_alone_cannot_be_mistaken_for_full_success(service):
    result = AdapterResult(
        jobs=[sample_job("partial-with-healthy-label")],
        content_hash="coverage-overrides-status",
        snapshot_complete=False,
        status="healthy",
        coverage=coverage(1),
    )
    service.adapter_factory = lambda _source: StaticCoverageAdapter(result)
    run = service.run(scan_type="deep", source_ids=[SOURCE_ID])
    assert run["status"] == "partial_success"
    assert run["new_jobs"] == 1
    assert run["errors"][0]["code"] == "COMPANY_SEARCH_INCOMPLETE"
    assert "1 家企业" in run["errors"][0]["message"]
    assert service.repository.get_source(SOURCE_ID)["status"] == "partial"


def test_partial_scan_does_not_close_jobs_missing_from_failed_batches(service):
    initial = AdapterResult(
        jobs=[sample_job("retained"), sample_job("unsearched")],
        content_hash="complete-before-partial",
        snapshot_complete=True,
        coverage=coverage(0),
    )
    service.adapter_factory = lambda _source: StaticCoverageAdapter(initial)
    assert service.run(scan_type="deep", source_ids=[SOURCE_ID])["new_jobs"] == 2
    partial = AdapterResult(
        jobs=[sample_job("retained")],
        content_hash="partial-missing-one",
        snapshot_complete=True,
        status="partial",
        coverage=coverage(1),
    )
    service.adapter_factory = lambda _source: StaticCoverageAdapter(partial)
    for _ in range(2):
        run = service.run(scan_type="deep", source_ids=[SOURCE_ID])
        assert run["status"] == "partial_success"
        assert run["closed_jobs"] == 0
    assert service.repository.get_job("unsearched")["status"] == "open"


def test_successful_search_retains_coverage_and_full_success_status(service):
    result = AdapterResult(
        jobs=[sample_job("complete")],
        content_hash="complete-coverage",
        snapshot_complete=False,
        status="healthy",
        coverage=coverage(0),
    )
    service.adapter_factory = lambda _source: StaticCoverageAdapter(result)
    run = service.run(scan_type="deep", source_ids=[SOURCE_ID])
    assert run["status"] == "success"
    assert run["new_jobs"] == 1
    assert run["errors"] == []
    assert service.repository.get_source(SOURCE_ID)["status"] == "healthy"
    assert service.repository.latest_snapshot_metadata(SOURCE_ID)["metadata"]["coverage"] == coverage(0)


def test_partial_source_without_coverage_uses_safe_generic_error(service):
    result = AdapterResult(
        jobs=[sample_job("partial-no-count")],
        content_hash="partial-no-coverage",
        snapshot_complete=False,
        status="partial",
        message="PRIVATE_PROVIDER_DIAGNOSTIC_NOT_PUBLIC",
    )
    service.adapter_factory = lambda _source: StaticCoverageAdapter(result)
    run = service.run(scan_type="deep", source_ids=[SOURCE_ID])
    assert run["status"] == "partial_success"
    assert run["errors"] == [{
        "source_id": SOURCE_ID,
        "code": "COMPANY_SEARCH_INCOMPLETE",
        "message": "该信源本轮仅部分完成；已取得的候选已保留。",
    }]


def test_search_update_api_exposes_scope_and_only_safe_coverage_metadata(service, monkeypatch):
    marker = "-".join(("12345678", "1234", "4123", "8123", "123456789abc"))
    secret = "sk" + "-proj-" + "NOT_A_REAL_KEY_TEST_VALUE_123456"
    private_coverage = {
        **coverage(),
        "failed_employers": [f"企业 test@example.com 13800138000 {marker} api_key={secret}"],
        "provider_diagnostic": "PRIVATE_PROVIDER_DIAGNOSTIC_NOT_PUBLIC",
        "source_thread_id": marker,
        "cookie": "PRIVATE_COOKIE_DO_NOT_COPY",
    }
    service.repository.save_snapshot(
        SOURCE_ID,
        "api-safe-coverage",
        "",
        {"coverage": private_coverage},
    )
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", service)
    with TestClient(main.app) as client:
        registration = client.post("/api/auth/register", json={
            "username": "coverage-api-user",
            "password": "correct-horse-123",
            "privacy_accepted": True,
        })
        assert registration.status_code == 201
        response = client.get("/api/future-radar/search-updates", headers={
            "Authorization": f"Bearer {registration.json()['access_token']}"
        })
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"]["category_count"] == 10
    assert payload["scope"]["list_entry_count"] == 218
    assert payload["scope"]["target_count"] == 205
    assert payload["scope"]["batch_count"] >= 26
    assert payload["coverage"]["target_count"] == 205
    assert payload["coverage"]["searched_count"] == 200
    assert payload["coverage"]["completed_at"]
    serialized = json.dumps(payload, ensure_ascii=False)
    for private in (
        marker, secret, "test@example.com", "13800138000", "PRIVATE_PROVIDER",
        "PRIVATE_COOKIE", "source_thread_id", "provider_diagnostic",
    ):
        assert private not in serialized
