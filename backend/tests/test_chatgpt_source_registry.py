"""Active bridge registration preserves retired sources without private mappings."""

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
from scripts.frostfire_chatgpt_sources import ACTIVE_CHATGPT_SOURCE_IDS as SCRIPT_SOURCES


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


def test_active_sources_are_logical_slots_without_private_metadata():
    sources = main.EXPECTED_CHATGPT_RADAR_SOURCES
    assert [source["source_id"] for source in sources] == [
        f"chatgpt-radar-{index:02d}" for index in (2, 7, 8, 9, 10, 11, 12, 13, 14)
    ]
    assert all(source["source_thread_id"] is None for source in sources)
    assert all(set(source) == {"source_id", "source_thread_id", "title"} for source in sources)
    assert tuple(source["source_id"] for source in sources) == SCRIPT_SOURCES


def test_seeding_active_six_keeps_retired_sources_and_pending_candidates(sync_db):
    old_sources = [
        {"source_id": f"chatgpt-radar-{index:02d}", "source_thread_id": None,
         "title": f"ChatGPT 监控 {index}"}
        for index in range(1, 7)
    ]
    database.ensure_recruitment_ingest_sources(old_sources)
    for source in old_sources:
        record_heartbeat(source)
    stored = database.upsert_recruitment_ingest_candidate(candidate("chatgpt-radar-05"))
    before = database.recruitment_sync_status(expected_source_count=9)

    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    after = database.recruitment_sync_status(expected_source_count=9)

    assert after["source_count"] == 14  # Retired history is not erased.
    assert after["expected_source_count"] == 9
    assert after["connected_source_count"] == 6
    old_by_id = {source["source_id"]: source for source in before["sources"]}
    new_by_id = {source["source_id"]: source for source in after["sources"]}
    assert all(new_by_id[source_id] == source for source_id, source in old_by_id.items())
    assert new_by_id["chatgpt-radar-11"]["status"] == "pending"
    assert new_by_id["chatgpt-radar-11"]["last_seen_at"] is None
    assert new_by_id["chatgpt-radar-11"]["source_ref"] is None
    assert before["recent_events"] == after["recent_events"]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM recruitment_ingest_candidates WHERE id = ?",
            (stored["id"],),
        ).fetchone()[0] == 1
    public = main.public_chatgpt_sync_status()
    assert public["expected_source_count"] == 9
    assert public["connected_source_count"] == 1
    assert public["status"] == "partial"
    assert public["inventory_total"] == 0  # Inactive history does not inflate active transport.
    assert public["reason_counts"] == {"pending": {}, "rejected": {}}


def test_new_workbook_source_heartbeat_is_required_before_all_report_synced(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES[:-1]:
        record_heartbeat(source)
    assert main.public_chatgpt_sync_status()["status"] == "partial"

    client = TestClient(main.app)
    headers = {"X-Recruitment-Token": "test-six-source-ingest-token"}
    response = client.post(
        "/api/recruitment/ingest",
        headers=headers,
        json={
            "jobs": [],
            "source_id": "chatgpt-radar-14",
            "source_updated_at": "2026-08-30T01:00:00Z",
        },
    )
    assert response.status_code == 200
    assert response.json()["received"] == 0
    assert response.json()["accepted"] == 0
    status = client.get("/api/recruitment/sync/status", headers=headers).json()
    assert status["expected_source_count"] == status["connected_source_count"] == 9
    workbook_source = next(source for source in status["sources"] if source["source_id"] == "chatgpt-radar-14")
    assert workbook_source["title"] == "ChatGPT 秋招双表监控"
    assert workbook_source["last_source_updated_at"] == "2026-08-30T01:00:00+00:00"
    assert workbook_source["source_ref"] is None
    assert main.public_chatgpt_sync_status()["status"] == "synced"
    assert client.get("/api/recruitment/sync/status").status_code == 401
    client.close()


def test_review_backlog_is_separate_from_transport_state(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES:
        record_heartbeat(source)
    pending = database.upsert_recruitment_ingest_candidate(
        candidate("chatgpt-radar-02")
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


def test_public_sync_status_reports_latest_chat_changes_without_model_work(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    database.record_recruitment_ingest_event(
        source_id="chatgpt-radar-02",
        source_thread_id=None,
        title="ChatGPT 监控 2",
        counts={
            "received": 9,
            "new": 4,
            "updated": 2,
            "duplicates": 3,
            "closed": 1,
            "source_screened": 5,
        },
        last_item_id="latest-item",
        last_source_updated_at="2026-09-21T03:00:00+00:00",
    )

    detailed = database.recruitment_sync_status(expected_source_count=9)
    assert detailed["new"] == 4
    assert detailed["updated"] == 2
    assert detailed["duplicates"] == 3
    public = main.public_chatgpt_sync_status()
    assert public["latest_ingest_counts"] == {
        "received": 9,
        "new": 4,
        "updated": 2,
        "duplicates": 3,
        "closed": 1,
        "source_screened": 5,
    }


def test_normal_rejection_does_not_become_transport_error(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for source in main.EXPECTED_CHATGPT_RADAR_SOURCES:
        record_heartbeat(source)
    stored = database.upsert_recruitment_ingest_candidate(
        candidate("chatgpt-radar-02")
    )
    database.set_recruitment_ingest_candidate_verification(
        stored["id"], "rejected", "not_campus"
    )
    database.record_recruitment_ingest_event(
        source_id="chatgpt-radar-02",
        source_thread_id=None,
        title="ChatGPT 监控 2",
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
            "WHERE source_id='chatgpt-radar-07'"
        )

    status = main.public_chatgpt_sync_status()

    assert status["status"] == status["transport_state"] == "error"
    assert status["verification_state"] == "complete"


def test_sixth_source_discards_thread_compatibility_field_and_marks_source_screened(sync_db, monkeypatch):
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
        source_id="chatgpt-radar-10",
        source_thread_id=private_placeholder,
        external_id="sixth-pending-job",
        company="六源测试集团",
        title="校园招聘分析师",
        city="上海",
        official_url="https://careers.example.com/jobs/sixth-pending-job",
    )])
    result = main.ingest_recruitment_jobs(request, None)
    assert result["source_screened"] == 1
    assert result["accepted"] == 0
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT source_thread_id, verification_status, promoted_job_id "
            "FROM recruitment_ingest_candidates WHERE source_id = ?",
            ("chatgpt-radar-10",),
        ).fetchone()
        assert tuple(stored) == (None, "source_screened", None)
        assert connection.execute("SELECT COUNT(*) FROM recruitment_jobs").fetchone()[0] == 0
    detailed = database.recruitment_sync_status(expected_source_count=9)
    assert private_placeholder not in str(detailed)
    assert len([source for source in detailed["sources"] if source["source_id"] == "chatgpt-radar-10"]) == 1


def test_sixth_source_shares_formal_job_identity_with_existing_sources():
    old = candidate("chatgpt-radar-05")
    new = candidate("chatgpt-radar-10", source_thread_id="discard-this-compatibility-value")
    assert new["source_thread_id"] is None
    assert old["id"] != new["id"]
    assert main._promoted_job(old)["id"] == main._promoted_job(new)["id"]


def test_browser_bridge_accepts_new_source_and_uses_an_independent_digest():
    message = {"source_id": "chatgpt-radar-10", "message_id": "logical-message-1", "rows": []}
    source_id, digest, rows = bridge.parse_browser_message(message)
    batches = bridge.build_batches(source_id, digest, rows)
    assert source_id == "chatgpt-radar-10"
    assert batches[0]["source_id"] == "chatgpt-radar-10"
    assert batches[0]["jobs"] == []
    old_digest = bridge.parse_browser_message({**message, "source_id": "chatgpt-radar-02"})[1]
    assert digest != old_digest
    assert "logical-message-1" not in str(batches)


def test_chatgpt_inventory_drilldown_has_exact_totals_and_stable_pages(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    database.ensure_recruitment_ingest_sources([
        {"source_id": "chatgpt-radar-01", "title": "Retired source"},
        {"source_id": "manual-review-fixture", "title": "Manual source"},
    ])
    expected = {"source_screened": [], "verified": [], "pending": [], "rejected": []}

    def store(source_id, suffix, verification_status, *, closed=False):
        stored = database.upsert_recruitment_ingest_candidate(candidate(
            source_id, external_id=suffix,
            title=f"校园招聘分析师 {suffix}",
            official_url=f"https://careers.example.com/jobs/{suffix}",
            status="closed" if closed else "open",
        ))
        database.set_recruitment_ingest_candidate_verification(
            stored["id"], verification_status, "not_campus" if verification_status == "rejected" else None,
        )
        return stored["id"]

    for status, count in (("source_screened", 2), ("verified", 3), ("pending", 4), ("rejected", 1)):
        for index in range(count):
            expected[status].append(store("chatgpt-radar-02", f"{status}-{index}", status))
        store("chatgpt-radar-07", f"closed-{status}", status, closed=True)
    store("chatgpt-radar-01", "retired", "pending")
    store("manual-review-fixture", "non-chatgpt", "pending")
    # An orphan does not contribute to registered-source inventories either.
    store("unregistered-source", "orphan", "pending")
    with database.connect() as connection:
        connection.execute(
            "UPDATE recruitment_ingest_candidates SET last_seen_at=?",
            ("2026-09-21T00:00:00+00:00",),
        )

    source_ids = tuple(main.EXPECTED_CHATGPT_SOURCE_IDS)
    inventory = main.public_chatgpt_sync_status()
    for label, stored_status in (
        ("source_screened", "source_screened"), ("accepted", "verified"),
        ("pending", "pending"), ("rejected", "rejected"),
    ):
        page = database.paginate_recruitment_ingest_review_candidates(
            statuses=(label,), source_ids=source_ids, limit=1,
        )
        assert page["total"] == inventory[f"inventory_{label}"] == len(expected[stored_status])
        assert len(page["items"]) == 1
        assert page["items"][0]["verification_status"] == stored_status
        assert page["has_more"] is (len(expected[stored_status]) > 1)

    all_statuses = ("source_screened", "accepted", "pending", "rejected")
    collected = []
    for offset in range(0, 10, 3):
        page = database.paginate_recruitment_ingest_review_candidates(
            statuses=all_statuses, source_ids=source_ids, limit=3, offset=offset,
        )
        assert page["total"] == inventory["inventory_total"] == 10
        assert (page["limit"], page["offset"]) == (3, offset)
        collected.extend(item["id"] for item in page["items"])
    assert collected == sorted((item for ids in expected.values() for item in ids), reverse=True)
    assert len(set(collected)) == 10
    assert page["has_more"] is False
    empty = database.paginate_recruitment_ingest_review_candidates(
        statuses=all_statuses, source_ids=source_ids, limit=3, offset=100,
    )
    assert empty["items"] == [] and empty["total"] == 10 and empty["has_more"] is False
    needs_review = database.paginate_recruitment_ingest_review_candidates(
        source_ids=source_ids, limit=2,
    )
    assert needs_review["total"] == inventory["inventory_pending"] + inventory["inventory_rejected"] == 5
    assert sum(inventory["reason_counts"]["rejected"].values()) == 1
    assert database.list_recruitment_ingest_review_candidates(
        statuses=all_statuses, source_ids=source_ids, limit=3, offset=3,
    ) == database.paginate_recruitment_ingest_review_candidates(
        statuses=all_statuses, source_ids=source_ids, limit=3, offset=3,
    )["items"]
    assert database.paginate_recruitment_ingest_review_candidates(source_ids=[])["total"] == 0


@pytest.mark.parametrize("arguments", [
    {"statuses": ()}, {"statuses": ("anything",)}, {"limit": 0}, {"limit": 201},
    {"offset": -1}, {"offset": 1.5}, {"source_ids": [f"source-{index}" for index in range(101)]},
])
def test_candidate_drilldown_rejects_invalid_scope_and_pagination(arguments):
    with pytest.raises(ValueError):
        database.paginate_recruitment_ingest_review_candidates(**arguments)


def test_clickable_signal_api_matches_inventory_without_leaking_source_identity(sync_db):
    database.ensure_recruitment_ingest_sources(main.EXPECTED_CHATGPT_RADAR_SOURCES)
    for index in range(3):
        item = database.upsert_recruitment_ingest_candidate(candidate(
            'chatgpt-radar-02', external_id=f'clickable-{index}',
            title=f'校园招聘分析师{index}', official_url=f'https://careers.example.com/jobs/{index}',
        ))
        with database.connect() as connection:
            connection.execute("UPDATE recruitment_ingest_candidates SET verification_status='rejected', verification_reason='not_campus' WHERE id=?", (item['id'],))
    main.app.dependency_overrides[main.current_user] = lambda: {'id': 123}
    try:
        with TestClient(main.app, raise_server_exceptions=True) as client:
            response = client.get('/api/future-radar/review-candidates?review_status=rejected&source_scope=chatgpt&limit=2&offset=0')
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['total'] == main.public_chatgpt_sync_status()['inventory_rejected'] == 3
            assert len(body['items']) == 2 and body['has_more']
            assert all(item['review_reason'] == '未识别到明确的校园招聘信息' for item in body['items'])
            assert 'source_key' not in response.text and 'source_thread_id' not in response.text
            assert 'chatgpt-radar-02' not in response.text
    finally:
        main.app.dependency_overrides.pop(main.current_user, None)
