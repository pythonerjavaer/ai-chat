"""Six-source bridge registration without any private conversation mapping."""

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from fastapi.testclient import TestClient

from backend import database, main
from scripts import frostfire_chatgpt_bridge as bridge


@pytest.fixture
def sync_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database, "settings", SimpleNamespace(database_path=tmp_path / "six-source.db")
    )
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(**{
            **vars(main.settings),
            "recruitment_ingest_token": "test-six-source-ingest-token",
        }),
    )
    database.init_db()


def candidate(source_id: str, **overrides) -> dict:
    item = main.RecruitmentIngestJob(**{
        "source_id": source_id,
        "external_id": "shared-campus-analyst",
        "company": "六源测试集团",
        "title": "校园招聘分析师",
        "city": "上海",
        "official_url": "https://careers.example.com/jobs/shared-campus-analyst",
        **overrides,
    })
    value, error = main._candidate_from_ingest_item(item)
    assert error is None
    return value


def record_heartbeat(source: dict) -> None:
    database.record_recruitment_ingest_event(
        source_id=source["source_id"],
        source_thread_id=None,
        title=source["title"],
        counts={"received": 0},
        last_item_id=None,
        last_source_updated_at="2026-08-30T00:00:00+00:00",
    )


def test_sixth_source_is_a_logical_slot_without_private_metadata():
    sources = main.EXPECTED_CHATGPT_RADAR_SOURCES
    assert [source["source_id"] for source in sources] == [
        f"chatgpt-radar-{index:02d}" for index in range(1, 7)
    ]
    assert all(source["source_thread_id"] is None for source in sources)
    assert all(set(source) == {"source_id", "source_thread_id", "title"} for source in sources)


def test_seeding_sixth_source_keeps_old_five_sources_and_pending_candidates(sync_db):
    old_sources = main.EXPECTED_CHATGPT_RADAR_SOURCES[:5]
    database.ensure_recruitment_ingest_sources(old_sources)
    for source in old_sources:
        record_heartbeat(source)
    stored = database.upsert_recruitment_ingest_candidate(candidate("chatgpt-radar-05"))
    before = database.recruitment_sync_status(expected_source_count=5)

    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    after = database.recruitment_sync_status(expected_source_count=6)

    assert after["source_count"] == after["expected_source_count"] == 6
    assert after["connected_source_count"] == 5
    old_by_id = {source["source_id"]: source for source in before["sources"]}
    new_by_id = {source["source_id"]: source for source in after["sources"]}
    assert all(new_by_id[source_id] == source for source_id, source in old_by_id.items())
    assert new_by_id["chatgpt-radar-06"]["status"] == "pending"
    assert new_by_id["chatgpt-radar-06"]["last_seen_at"] is None
    assert new_by_id["chatgpt-radar-06"]["source_ref"] is None
    assert before["recent_events"] == after["recent_events"]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recruitment_ingest_candidates WHERE id = ?",
            (stored["id"],),
        ).fetchone()[0] == 1
    public = main.public_chatgpt_sync_status()
    assert public["expected_source_count"] == 6
    assert public["connected_source_count"] == 5
    assert public["status"] == "partial"
    assert public["inventory_total"] == 1
    assert public["reason_counts"] == {"pending": {"other": 1}, "rejected": {}}


def test_sixth_source_heartbeat_is_required_before_all_sources_report_synced(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES[:5]:
        record_heartbeat(source)
    assert main.public_chatgpt_sync_status()["status"] == "partial"

    client = TestClient(main.app)
    headers = {"X-Recruitment-Token": "test-six-source-ingest-token"}
    response = client.post(
        "/api/recruitment/ingest",
        headers=headers,
        json={
            "jobs": [],
            "source_id": "chatgpt-radar-06",
            "source_updated_at": "2026-08-30T01:00:00Z",
        },
    )
    assert response.status_code == 200
    assert response.json()["received"] == 0
    assert response.json()["accepted"] == 0
    status = client.get("/api/recruitment/sync/status", headers=headers).json()
    assert status["expected_source_count"] == status["connected_source_count"] == 6
    sixth = next(source for source in status["sources"] if source["source_id"] == "chatgpt-radar-06")
    assert sixth["title"] == "ChatGPT 监控 6"
    assert sixth["last_source_updated_at"] == "2026-08-30T01:00:00+00:00"
    assert sixth["source_ref"] is None
    assert main.public_chatgpt_sync_status()["status"] == "synced"
    assert client.get("/api/recruitment/sync/status").status_code == 401
    client.close()


def test_review_backlog_is_separate_from_transport_state(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES:
        record_heartbeat(source)
    pending = database.upsert_recruitment_ingest_candidate(
        candidate("chatgpt-radar-01")
    )

    status = main.public_chatgpt_sync_status()

    assert status["status"] == status["transport_state"] == "synced"
    assert status["verification_state"] == "pending"
    assert status["inventory_pending"] == 1
    assert status["latest_verification_counts"] == {
        "accepted": 0, "pending": 0, "rejected": 0,
    }
    assert "pending" not in status and "rejected" not in status
    assert pending["verification_status"] == "pending"


def test_normal_rejection_does_not_become_transport_error(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES:
        record_heartbeat(source)
    stored = database.upsert_recruitment_ingest_candidate(
        candidate("chatgpt-radar-01")
    )
    database.set_recruitment_ingest_candidate_verification(
        stored["id"], "rejected", "not_campus"
    )
    database.record_recruitment_ingest_event(
        source_id="chatgpt-radar-01",
        source_thread_id=None,
        title="ChatGPT 监控 1",
        counts={"received": 1, "rejected": 1},
        last_item_id="shared-campus-analyst",
        last_source_updated_at="2026-09-03T00:00:00+00:00",
    )

    status = main.public_chatgpt_sync_status()

    assert status["status"] == status["transport_state"] == "synced"
    assert status["verification_state"] == "complete_with_rejections"
    assert status["latest_verification_counts"]["rejected"] == 1
    assert status["inventory_rejected"] == 1


def test_recent_source_error_remains_a_transport_error(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES:
        record_heartbeat(source)
    with database.connect() as connection:
        connection.execute(
            "UPDATE recruitment_ingest_sources SET status='error' "
            "WHERE source_id='chatgpt-radar-03'"
        )

    status = main.public_chatgpt_sync_status()

    assert status["status"] == status["transport_state"] == "error"
    assert status["verification_state"] == "complete"


def test_sixth_source_discards_thread_compatibility_field_and_keeps_pending_isolated(sync_db, monkeypatch):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    monkeypatch.setattr(
        main,
        "_verify_ingest_candidate",
        lambda _candidate: (
            "pending", "official_page_unavailable", {"opening_date": None, "closing_date": None}
        ),
    )
    private_placeholder = "private-compatibility-value-must-not-be-stored"
    request = main.RecruitmentIngestRequest(jobs=[main.RecruitmentIngestJob(
        source_id="chatgpt-radar-06",
        source_thread_id=private_placeholder,
        external_id="sixth-pending-job",
        company="六源测试集团",
        title="校园招聘分析师",
        city="上海",
        official_url="https://careers.example.com/jobs/sixth-pending-job",
    )])
    result = main.ingest_recruitment_jobs(request, None)
    assert result["pending"] == 1
    assert result["accepted"] == 0
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT source_thread_id, verification_status, promoted_job_id "
            "FROM recruitment_ingest_candidates WHERE source_id = ?",
            ("chatgpt-radar-06",),
        ).fetchone()
        assert tuple(stored) == (None, "pending", None)
        assert connection.execute("SELECT COUNT(*) FROM recruitment_jobs").fetchone()[0] == 0
    detailed = database.recruitment_sync_status(expected_source_count=6)
    assert private_placeholder not in str(detailed)
    assert len([source for source in detailed["sources"] if source["source_id"] == "chatgpt-radar-06"]) == 1


def test_sixth_source_shares_formal_job_identity_with_existing_sources():
    old = candidate("chatgpt-radar-05")
    new = candidate("chatgpt-radar-06", source_thread_id="discard-this-compatibility-value")
    assert new["source_thread_id"] is None
    assert old["id"] != new["id"]
    assert main._promoted_job(old)["id"] == main._promoted_job(new)["id"]


def test_browser_bridge_accepts_sixth_source_and_uses_an_independent_digest():
    message = {"source_id": "chatgpt-radar-06", "message_id": "logical-message-1", "rows": []}
    source_id, digest, rows = bridge.parse_browser_message(message)
    batches = bridge.build_batches(source_id, digest, rows)
    assert source_id == "chatgpt-radar-06"
    assert batches[0]["source_id"] == "chatgpt-radar-06"
    assert batches[0]["jobs"] == []
    old_digest = bridge.parse_browser_message({**message, "source_id": "chatgpt-radar-05"})[1]
    assert digest != old_digest
    assert "logical-message-1" not in str(batches)
