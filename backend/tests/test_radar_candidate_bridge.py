"""Regression tests for the local historical-search discovery projection."""

import hashlib
import json
import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from backend import database
from backend.future_radar.adapters import LegacyDatabaseAdapter
from backend.future_radar.service import FutureRadarService
from backend.recruitment_search import WEB_SEARCH_SOURCE


BRIDGE_SOURCE = "legacy-search-discovery"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "settings", SimpleNamespace(
        database_path=tmp_path / "candidate-bridge.db"
    ))
    database.init_db()
    radar = FutureRadarService(
        connect=database.connect,
        openai_api_key="test-key",
        ai_model="test-model",
        web_search_enabled=False,
    )
    radar.seed_registry()
    return radar


def insert_ingest(key: str, **overrides):
    digest = hashlib.sha256(key.encode()).hexdigest()
    candidate = {
        "id": f"candidate-{digest[:32]}",
        "dedupe_key": digest,
        "source_key": "chatgpt-radar-01",
        "source_id": "chatgpt-radar-01",
        "source_item_id": key,
        "external_id": key,
        "source_updated_at": "2026-08-29T01:00:00+00:00",
        "company": "示例科技",
        "employer_type": "互联网企业",
        "title": "2027 校园招聘数据分析岗",
        "city": "上海",
        "industry": "科技",
        "official_url": f"https://careers.example.com/campus/{key}",
        "canonical_url": f"https://careers.example.com/campus/{key}",
        "source": "公开同步候选",
        "requirements": "面向应届毕业生。",
        "tags": ["校园招聘", "internet_tech"],
        "evidence": [],
        "incoming_status": "open",
        "payload_hash": hashlib.sha256(f"{key}-v1".encode()).hexdigest(),
        **overrides,
    }
    return database.upsert_recruitment_ingest_candidate(candidate)


def insert_web_job(key: str, **overrides):
    job = {
        "id": key,
        "company": "示例科技",
        "employer_type": "互联网企业",
        "title": "2027 校园招聘工程师",
        "city": "上海",
        "industry": "科技",
        "url": f"https://careers.example.com/campus/{key}",
        "source": WEB_SEARCH_SOURCE,
        "requirements": "应届毕业生。",
        "tags": ["校园招聘", "AI网页搜索", "internet_tech", "待官方核验"],
        "status": "open",
        **overrides,
    }
    database.upsert_recruitment_jobs([job])
    return job


def scan_bridge(service):
    return service.run(scan_type="quick", source_ids=[BRIDGE_SOURCE])


def test_bridge_is_quick_and_imports_old_web_and_ingest_candidates_without_promoting(service):
    insert_web_job("historical-web-search")
    insert_ingest("pending-role")
    rejected = insert_ingest("rejected-role")
    database.set_recruitment_ingest_candidate_verification(
        rejected["id"], "rejected", "title_not_confirmed"
    )
    source = service.repository.get_source(BRIDGE_SOURCE)
    assert source["trust_level"] == "discovery"
    assert BRIDGE_SOURCE in {
        item["id"] for item in service.repository.manual_scan_sources("quick")
    }
    assert BRIDGE_SOURCE not in {
        item["id"] for item in service.repository.manual_scan_sources("deep")
    }

    run = scan_bridge(service)
    assert run["new_jobs"] == 3
    pool = service.repository.list_jobs(filters={
        "discovery_source_only": True,
        "status": "open",
    })
    assert pool["total"] == 3
    assert {item["verification_status"] for item in pool["items"]} == {"pending", "rejected"}
    assert all(
        any(source["source_id"] == BRIDGE_SOURCE for source in item["sources"])
        for item in pool["items"]
    )
    assert service.repository.list_jobs(filters={
        "verification_status": "verified", "status": "all"
    })["total"] == 0


def test_bridge_hash_and_ids_are_stable_and_next_quick_scan_sees_updates(service):
    original = insert_ingest("same-role")
    first = scan_bridge(service)
    assert first["new_jobs"] == 1
    source = service.repository.get_source(BRIDGE_SOURCE)
    first_result = LegacyDatabaseAdapter().scan(source)
    first_id = first_result.jobs[0]["external_id"]

    # A replay may advance its transport cursor, but creates no new job/event.
    insert_ingest("same-role", source_updated_at="2026-08-29T02:00:00+00:00")
    second_result = LegacyDatabaseAdapter().scan(source)
    assert first_result.content_hash == second_result.content_hash
    assert first_id == second_result.jobs[0]["external_id"]
    assert json.loads(second_result.normalized_content)["observed_cursor"]
    replay = scan_bridge(service)
    assert replay["new_jobs"] == 0
    assert replay["updated_jobs"] == 0

    # Verification updates do not update the old table's last_seen_at. Full
    # local projection must still notice them instead of trusting a timestamp.
    database.set_recruitment_ingest_candidate_verification(
        original["id"], "rejected", "unconfirmed"
    )
    rejected = scan_bridge(service)
    assert rejected["updated_jobs"] == 1
    assert service.repository.get_job(first_id)["verification_status"] == "rejected"
    insert_ingest("new-role")
    assert scan_bridge(service)["new_jobs"] == 1


def test_candidate_identity_aligns_with_future_promotion_and_official_verification(service):
    insert_ingest("promoted-role")
    assert scan_bridge(service)["new_jobs"] == 1
    identity = "external:示例科技:promoted-role"
    promoted_id = f"monitor-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    assert service.repository.get_job(promoted_id)["verification_status"] == "pending"

    insert_web_job(
        promoted_id,
        source="已核验同步来源",
        tags=["校园招聘", "链接已验证", "标题已验证"],
    )
    official = service.run(
        scan_type="quick", source_ids=["legacy-recruitment-pipeline"]
    )
    assert official["new_jobs"] == 0
    assert official["updated_jobs"] == 1
    assert scan_bridge(service)["new_jobs"] == 0
    pool = service.repository.list_jobs(filters={"discovery_source_only": True})
    assert pool["total"] == 1
    assert pool["items"][0]["verification_status"] == "verified"
    assert len(pool["items"][0]["sources"]) == 2


def test_bridge_links_previously_migrated_web_rows_without_duplicate_or_downgrade(service):
    insert_web_job(
        "known-web-job", tags=["校园招聘", "AI网页搜索", "链接已验证", "标题已验证"]
    )
    assert service.run(
        scan_type="quick", source_ids=["legacy-recruitment-pipeline"]
    )["new_jobs"] == 1
    assert service.repository.list_jobs(filters={"discovery_source_only": True})["total"] == 0
    assert scan_bridge(service)["new_jobs"] == 0
    pool = service.repository.list_jobs(filters={"discovery_source_only": True})
    assert pool["total"] == 1
    assert pool["items"][0]["verification_status"] == "verified"


def test_bridge_removes_closed_candidates_and_never_exposes_expired_jobs(service):
    current = insert_ingest("closing-role")
    insert_ingest("already-expired", closing_date=(date.today() - timedelta(days=1)).isoformat())
    insert_web_job("old-expired-web", closing_date=(date.today() - timedelta(days=1)).isoformat())
    assert scan_bridge(service)["new_jobs"] == 1
    database.set_recruitment_ingest_candidate_verification(
        current["id"], "closed", "expired"
    )
    closed = scan_bridge(service)
    assert closed["closed_jobs"] == 1
    assert service.repository.list_jobs(filters={"discovery_source_only": True})["total"] == 0


def test_bridge_redacts_sensitive_text_and_drops_private_or_credential_urls(service):
    marker = "-".join(("12345678", "1234", "4123", "8123", "123456789abc"))
    secret = "sk" + "-proj-" + "NOT_A_REAL_KEY_TEST_VALUE_123456"
    insert_ingest(
        "safe-role",
        source_thread_id=marker,
        source=f"Private source {marker}",
        evidence=["PRIVATE-EVIDENCE-DO-NOT-COPY"],
        requirements=f"应届生 test@example.com 13800138000 api_key={secret} {marker}",
        tags=[f"private {marker}", secret, "internet_tech"],
    )
    insert_ingest(
        "private-link",
        canonical_url=f"https://chatgpt.com/c/{marker}",
        official_url=f"https://chatgpt.com/c/{marker}",
    )
    insert_ingest(
        "credential-link",
        canonical_url="https://careers.example.com/job?token=NOT_REAL_TEST_TOKEN",
        official_url="https://careers.example.com/job?token=NOT_REAL_TEST_TOKEN",
    )
    result = LegacyDatabaseAdapter().scan(service.repository.get_source(BRIDGE_SOURCE))
    serialized = json.dumps(result.jobs, ensure_ascii=False) + result.normalized_content
    for private in (
        marker, secret, "test@example.com", "13800138000", "PRIVATE-EVIDENCE",
        "source_thread_id", "chatgpt.com", "NOT_REAL_TEST_TOKEN",
    ):
        assert private not in serialized
    assert sum(job["official_url"] is None for job in result.jobs) == 2
    assert result.verified_job_external_ids == set()
    assert {job["verification_status"] for job in result.jobs} == {"pending"}


def test_bridge_does_not_promote_an_old_verified_ingest_claim_by_itself(service):
    original = insert_ingest("claim-only")
    database.set_recruitment_ingest_candidate_verification(
        original["id"], "verified", None
    )
    assert scan_bridge(service)["new_jobs"] == 1
    assert service.repository.list_jobs(filters={
        "verification_status": "verified", "status": "all"
    })["total"] == 0


def scoped_source(service, ids):
    source = service.repository.get_source(BRIDGE_SOURCE)
    return {**source, "adapter_config": {**source["adapter_config"], "candidate_ids": ids}}


def test_scoped_bridge_reads_only_ten_candidates_and_never_the_legacy_table(service, monkeypatch):
    rows = [insert_ingest(f"batch-role-{index}") for index in range(12)]
    insert_web_job("unrelated-web-role")
    statements = []
    original_connect = database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    source = scoped_source(service, [row["id"] for row in rows[:10]])
    monkeypatch.setattr(database, "connect", traced_connect)
    result = LegacyDatabaseAdapter().scan(source)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(result.jobs) == 10
    assert result.snapshot_complete is False
    assert len(reads) == 1
    assert "FROM recruitment_ingest_candidates" in reads[0]
    assert "WHERE id IN (" in reads[0]
    assert "FROM recruitment_jobs" not in reads[0]
    assert {job["official_url"] for job in result.jobs} == {
        f"https://careers.example.com/campus/batch-role-{index}" for index in range(10)
    }


@pytest.mark.parametrize("ids", [
    None, "", "candidate-" + "a" * 32, {}, [None], [True], [3],
    ["candidate-bad"], ["candidate-" + "A" * 32], ["' OR 1=1 --"],
    ["candidate-" + format(index, "032x") for index in range(11)],
])
def test_invalid_scoped_ids_never_fall_back_to_full_pool(service, monkeypatch, ids):
    source = scoped_source(service, ids)
    monkeypatch.setattr(database, "connect", lambda: pytest.fail("invalid scope opened the database"))
    with pytest.raises(ValueError, match="candidate_ids"):
        LegacyDatabaseAdapter().scan(source)


def test_empty_scoped_ids_are_an_empty_partial_snapshot_without_database_reads(service, monkeypatch):
    source = scoped_source(service, [])
    monkeypatch.setattr(database, "connect", lambda: pytest.fail("empty scope opened the database"))
    result = LegacyDatabaseAdapter().scan(source)
    assert result.jobs == []
    assert result.snapshot_complete is False
    assert result.verified_job_external_ids == set()


def test_unknown_scoped_id_does_not_scan_other_candidates(service):
    insert_ingest("unrelated-role")
    source = scoped_source(service, ["candidate-" + "0" * 32])
    result = LegacyDatabaseAdapter().scan(source)
    assert result.jobs == [] and result.snapshot_complete is False


def test_incremental_bridge_preserves_older_jobs_and_normal_quick_stays_full(service):
    insert_ingest("older-role")
    assert scan_bridge(service)["new_jobs"] == 1
    with database.connect() as connection:
        old_row = dict(connection.execute("SELECT * FROM radar_jobs").fetchone())
        old_link = dict(connection.execute("SELECT * FROM job_sources").fetchone())
    new = insert_ingest("new-batch-role")
    insert_web_job("not-in-this-batch")
    for _ in range(3):
        run = service.run(
            scan_type="quick", source_ids=[BRIDGE_SOURCE],
            bridge_candidate_ids=[new["id"], new["id"]],
        )
        assert run["status"] == "success" and run["closed_jobs"] == 0
    with database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM radar_jobs WHERE id=?", (old_row["id"],)).fetchone()) == old_row
        assert dict(connection.execute("SELECT * FROM job_sources WHERE job_id=?", (old_row["id"],)).fetchone()) == old_link
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 2
    assert "candidate_ids" not in service.repository.get_source(BRIDGE_SOURCE)["adapter_config"]
    # No filter persists into the ordinary full Quick Scan.
    assert scan_bridge(service)["new_jobs"] == 1
    full_result = LegacyDatabaseAdapter().scan(service.repository.get_source(BRIDGE_SOURCE))
    assert full_result.snapshot_complete is True and len(full_result.jobs) == 3


def test_sixth_source_scoped_identity_matches_full_bridge_before_and_after_promotion(service):
    item = insert_ingest("sixth-current-role", source_id="chatgpt-radar-06", source_key="chatgpt-radar-06")
    source = scoped_source(service, [item["id"]])
    before = LegacyDatabaseAdapter().scan(source)
    expected = "monitor-" + hashlib.sha256("external:示例科技:sixth-current-role".encode()).hexdigest()[:24]
    assert before.jobs[0]["external_id"] == expected
    insert_web_job(expected)
    database.set_recruitment_ingest_candidate_verification(item["id"], "verified", None, expected)
    after = LegacyDatabaseAdapter().scan(source)
    full = LegacyDatabaseAdapter().scan(service.repository.get_source(BRIDGE_SOURCE))
    assert len(after.jobs) == len(full.jobs) == 1
    assert after.jobs[0]["external_id"] == full.jobs[0]["external_id"] == expected
    assert after.content_hash == before.content_hash == full.content_hash
    assert after.jobs[0]["verification_status"] == "pending"


@pytest.mark.parametrize("disposition", ["closed", "rejected", "expired"])
@pytest.mark.parametrize("other_official_source", [False, True])
def test_scoped_withdrawal_retires_only_explicit_ids_and_preserves_other_sources(service, disposition, other_official_source):
    item = insert_ingest("withdrawn-role")
    insert_ingest("unrelated-still-open")
    assert scan_bridge(service)["new_jobs"] == 2
    before = LegacyDatabaseAdapter().scan(scoped_source(service, [item["id"]]))
    external_id = before.jobs[0]["external_id"]
    if other_official_source:
        insert_web_job(external_id, source="独立官方来源", tags=["校园招聘", "链接已验证", "标题已验证"])
        assert service.run(scan_type="quick", source_ids=["legacy-recruitment-pipeline"])["updated_jobs"] == 1
    if disposition == "expired":
        insert_ingest("withdrawn-role", closing_date=(date.today() - timedelta(days=1)).isoformat(), payload_hash="updated-expiry")
    else:
        database.set_recruitment_ingest_candidate_verification(item["id"], disposition, "explicit-withdrawal")
    result = LegacyDatabaseAdapter().scan(scoped_source(service, [item["id"]]))
    assert result.snapshot_complete is False
    assert result.jobs == []
    assert result.retired_job_external_ids == {external_id}
    run = service.run(scan_type="quick", source_ids=[BRIDGE_SOURCE], bridge_candidate_ids=[item["id"]])
    assert run["status"] == "success"
    assert run["closed_jobs"] == (0 if other_official_source else 1)
    with database.connect() as connection:
        job = dict(connection.execute("SELECT * FROM radar_jobs WHERE external_id=?", (external_id,)).fetchone())
        assert job["status"] == ("open" if other_official_source else "closed")
        assert connection.execute("SELECT active FROM job_sources WHERE job_id=? AND source_id=?", (job["id"], BRIDGE_SOURCE)).fetchone()[0] == 0
        other = connection.execute("SELECT status FROM radar_jobs WHERE id<>?", (job["id"],)).fetchone()
        assert other["status"] == "open"
        if other_official_source:
            assert connection.execute("SELECT active FROM job_sources WHERE job_id=? AND source_id=?", (job["id"], "legacy-recruitment-pipeline")).fetchone()[0] == 1


def test_malformed_scoped_record_is_not_an_explicit_withdrawal(service):
    item = insert_ingest("malformed-role", company="")
    result = LegacyDatabaseAdapter().scan(scoped_source(service, [item["id"]]))
    assert result.jobs == [] and result.snapshot_complete is False
    assert result.retired_job_external_ids == set()
