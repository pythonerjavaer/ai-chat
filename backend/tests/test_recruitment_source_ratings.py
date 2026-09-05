"""Original monitor ratings survive transport, storage, dedupe and filtering."""

import json

import pytest
from pydantic import ValidationError

from backend.tests.test_recruitment_ingest_bridge import (
    BRIDGE_SOURCE, INGEST_HEADERS, harness, payload,
)
from backend import database, main
from backend.recruitment import score_job
from backend.recruitment_rating import SourceRating
from backend.future_radar.adapters import AdapterResult
from backend.future_radar.opportunity_cache import (
    PRE_RATING_SQLITE_REVISION_TRIGGERS, install_opportunity_revision,
    read_opportunity_revision,
)


def rated_payload(rating, *, source_id="chatgpt-radar-01", stamp=None, key="rated-role"):
    request = payload(key)
    request["source_id"] = source_id
    if stamp:
        request["source_updated_at"] = stamp
    request["jobs"][0].update({
        "source_rating": rating,
        "requirements": "面向应届毕业生，参与数据分析、风险研究、指标体系建设和业务策略优化。",
    })
    return request


def submit(harness, request):
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=request)
    assert response.status_code == 200, response.text
    return response.json()


def pool(harness, **params):
    response = harness.client.get(
        "/api/future-radar/opportunities", headers=harness.bearer,
        params={"priority_only": "false", **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("tier", ["T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3"])
def test_explicit_tier_preserves_no_invented_score_and_pending_verification(harness, tier):
    request = rated_payload({"scope": "job", "tier_code": tier, "reason": "原始岗位评价"})
    assert submit(harness, request)["pending"] == 1
    item = pool(harness, tier_code=tier)["items"][0]
    assert (item["tier_code"], item["tier_bucket"]) == (tier, tier)
    assert item["job_score"] is None and item["match_score"] is None
    assert item["source_rating"]["tier_code"] == tier
    assert "score" not in item["source_rating"]
    assert item["source_rating"]["source_id"] == "chatgpt-radar-01"
    assert item["rating_source"] == "chatgpt" and item["rating_status"] == "applied"
    assert item["verification_status"] == "pending" and not item["officially_verified"]
    assert not item["published_as_active_job"]
    assert "rating_key" not in item["source_rating"] and "observed_at" not in item["source_rating"]
    with database.connect() as connection:
        original = json.loads(connection.execute("SELECT source_rating FROM recruitment_ingest_candidates").fetchone()[0])
        persisted = json.loads(connection.execute("SELECT source_ratings FROM radar_jobs").fetchone()[0])
    assert original["tier_code"] == tier and persisted[0]["tier_code"] == tier


def test_explicit_decimal_score_and_tier_override_system_independently(harness):
    submit(harness, rated_payload({"scope": "job", "tier_code": "T0.5", "score": 91.25}))
    item = pool(harness)["items"][0]
    assert item["tier_code"] == "T0.5" and item["job_score"] == 91.25
    assert item["match_score"] == 91.25
    assert item["system_job_score"] != item["job_score"]
    assert item["scoring_status"] == "source_rated"
    # Detail and tier pages use the same resolved rating as the main pool.
    detail = harness.client.get(
        f"/api/future-radar/opportunities/{item['id']}", headers=harness.bearer,
    )
    assert detail.status_code == 200
    assert detail.json()["tier_code"] == "T0.5"


def test_numeric_only_original_has_no_invented_tier(harness):
    submit(harness, rated_payload({"scope": "job", "score": 100}))
    item = pool(harness, tier_code="UNRANKED")["items"][0]
    assert item["tier_code"] is None and item["tier_bucket"] == "UNRANKED"
    assert item["job_score"] == 100 and "tier_code" not in item["source_rating"]
    assert pool(harness, tier_code="T0")["total"] == 0


def test_company_rating_remains_context_without_overriding_job(harness):
    submit(harness, rated_payload({"scope": "company", "tier_code": "T0", "score": 99}))
    item = pool(harness)["items"][0]
    assert item["rating_status"] == "company_reference"
    assert item["source_rating"]["scope"] == "company"
    assert item["job_score"] == item["system_job_score"] != 99
    assert item["tier_code"] == item["system_tier_code"]


def test_rating_correction_invalidates_warm_pool_and_ignores_older_source_payload(harness):
    first = rated_payload({"scope": "job", "tier_code": "T2", "score": 72}, stamp="2026-08-20T00:00:00Z")
    submit(harness, first)
    assert pool(harness, tier_code="T2")["total"] == 1
    correction = rated_payload({"scope": "job", "tier_code": "T1.5", "score": 79.5}, stamp="2026-08-21T00:00:00Z")
    assert submit(harness, correction)["updated"] == 1
    assert pool(harness, tier_code="T2")["total"] == 0
    item = pool(harness, tier_code="T1.5")["items"][0]
    assert item["job_score"] == 79.5 and item["rating_status"] == "applied"
    assert len(item["source_ratings"]) == 1
    assert submit(harness, first)["stale"] == 1
    assert pool(harness, tier_code="T1.5")["items"][0]["job_score"] == 79.5


def test_conflicting_sources_are_retained_without_choosing_higher_rating(harness):
    submit(harness, rated_payload({"scope": "job", "tier_code": "T0", "score": 98}))
    submit(harness, rated_payload({"scope": "job", "tier_code": "T2.5", "score": 66}, source_id="chatgpt-radar-02"))
    result = pool(harness)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["rating_status"] == "conflicted" and item["source_rating"] is None
    assert {rating["tier_code"] for rating in item["source_ratings"]} == {"T0", "T2.5"}
    assert item["tier_code"] == item["system_tier_code"]
    assert item["job_score"] == item["system_job_score"]
    # A complete later projection must preserve the conflict too.
    harness.service.run(scan_type="quick", source_ids=[BRIDGE_SOURCE])
    assert pool(harness)["items"][0]["rating_status"] == "conflicted"


def test_official_duplicate_winner_keeps_discovery_original_rating(harness):
    submit(harness, rated_payload({"scope": "job", "tier_code": "T1", "score": 84}))
    source = harness.service.repository.create_source({
        "id": "rating-official", "name": "Official fixture", "source_type": "official_html",
        "trust_level": "verification", "adapter_config": {"adapter": "manual"},
    })
    official_job = {**rated_payload(None)["jobs"][0], "external_id": "official-duplicate", "source_rating": None}
    run = harness.service.repository.create_run("test", [source["id"]])
    harness.service.process_result(source=source, result=AdapterResult(jobs=[official_job]), run_id=run["id"])
    result = pool(harness)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["verification_status"] == "verified" and item["officially_verified"]
    assert item["tier_code"] == "T1" and item["job_score"] == 84
    assert item["rating_source"] == "chatgpt"


def test_verified_promotion_and_later_closed_status_do_not_lose_or_reopen_ratings(harness, monkeypatch):
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "verified", None, {"opening_date": "2026-08-01", "closing_date": "2099-09-01"},
    ))
    request = rated_payload({"scope": "job", "tier_code": "T0.5", "score": 89})
    assert submit(harness, request)["accepted"] == 1
    legacy = database.list_recruitment_jobs()[0]
    assert legacy["source_rating"]["score"] == 89
    assert score_job(legacy, {})["tier_code"] == "T0.5"
    request["jobs"][0]["status"] = "closed"
    assert submit(harness, request)["closed"] == 1
    assert pool(harness)["total"] == 0


def test_adding_original_rating_preserves_existing_official_verification_without_fetch(harness, monkeypatch):
    calls = []

    def verify(candidate):
        calls.append(candidate["id"])
        return "verified", None, {"opening_date": "2026-08-01", "closing_date": "2099-09-01"}

    monkeypatch.setattr(main, "_verify_ingest_candidate", verify)
    submit(harness, rated_payload(None, stamp="2026-08-20T00:00:00Z"))
    with database.connect() as connection:
        before = dict(connection.execute("SELECT * FROM recruitment_ingest_candidates").fetchone())
    legacy_before = database.list_recruitment_jobs()[0]
    request = rated_payload({"scope": "job", "tier_code": "T0.5", "score": 88.5}, stamp="2026-08-21T00:00:00Z")
    request["jobs"][0]["source_item_id"] = "corrected-rating-message"
    result = submit(harness, request)
    assert result["updated"] == result["accepted"] == 1
    assert result["pending"] == 0 and len(calls) == 1
    with database.connect() as connection:
        after = dict(connection.execute("SELECT * FROM recruitment_ingest_candidates").fetchone())
    for field in ("verification_status", "verified_at", "verified_opening_date", "verified_closing_date", "verification_attempt_count"):
        assert after[field] == before[field]
    legacy_after = database.list_recruitment_jobs()[0]
    assert legacy_after["source_rating"]["score"] == 88.5
    assert legacy_after["last_verified_at"] == legacy_before["last_verified_at"]
    assert pool(harness)["items"][0]["job_score"] == 88.5


def test_later_unrated_observation_preserves_explicit_durable_rating(harness, monkeypatch):
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "verified", None, {"opening_date": "2026-08-01", "closing_date": "2099-09-01"},
    ))
    submit(harness, rated_payload(
        {"scope": "job", "tier_code": "T1.5", "score": 77.5}, stamp="2026-08-20T00:00:00Z",
    ))
    later = rated_payload(None, stamp="2026-08-21T00:00:00Z")
    later["jobs"][0].pop("source_rating")
    later["jobs"][0]["requirements"] += "要求熟练使用数据分析工具。"
    submit(harness, later)
    with database.connect() as connection:
        candidate = json.loads(connection.execute("SELECT source_rating FROM recruitment_ingest_candidates").fetchone()[0])
    legacy = database.list_recruitment_jobs()[0]["source_rating"]
    item = pool(harness)["items"][0]
    for rating in (candidate, legacy, item["source_rating"]):
        assert rating["tier_code"] == "T1.5" and rating["score"] == 77.5
        assert rating["source_updated_at"].startswith("2026-08-20")


@pytest.mark.parametrize("field,value", [
    ("title", "2027 校园招聘投资研究岗"),
    ("requirements", "面向应届毕业生，参与投资研究与分析。"),
    ("official_url", "https://careers.example.com/campus/revised-url"),
    ("closing_date", "2099-10-01"),
    ("evidence", ["来源补充岗位信息。"]),
    ("tags", ["校园招聘", "internet_tech", "新职责"]),
])
def test_rating_change_with_material_job_change_still_reverifies(harness, monkeypatch, field, value):
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "verified", None, {"opening_date": "2026-08-01", "closing_date": "2099-09-01"},
    ))
    submit(harness, rated_payload(None))
    calls = []

    def pending(candidate):
        calls.append(candidate["id"])
        return "pending", "official_page_fetch_failed", {"opening_date": None, "closing_date": None}

    monkeypatch.setattr(main, "_verify_ingest_candidate", pending)
    request = rated_payload({"scope": "job", "tier_code": "T1", "score": 82})
    request["jobs"][0][field] = value
    result = submit(harness, request)
    assert result["pending"] == 1 and len(calls) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT verification_status FROM recruitment_ingest_candidates").fetchone()[0] == "pending"


def test_rating_metadata_update_does_not_bypass_known_official_expiry(harness, monkeypatch):
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _item: (
        "verified", None, {"opening_date": "2026-08-01", "closing_date": "2099-09-01"},
    ))
    submit(harness, rated_payload(None))
    with database.connect() as connection:
        connection.execute("UPDATE recruitment_ingest_candidates SET verified_closing_date='2000-01-01'")
    result = submit(harness, rated_payload({"scope": "job", "tier_code": "T0"}))
    assert result["closed"] == 1 and result["accepted"] == 0
    assert pool(harness)["total"] == 0


def test_sync_api_also_preserves_rating_and_does_not_grant_verification(harness):
    raw = rated_payload({"scope": "job", "score": 81.5})["jobs"][0]
    response = harness.client.post("/api/future-radar/sync", headers=INGEST_HEADERS, json={
        "version": "FROSTFIRE_SYNC_V1", "source_id": "chatgpt-radar-07", "jobs": [raw],
    })
    assert response.status_code == 200, response.text
    item = pool(harness)["items"][0]
    assert item["job_score"] == 81.5 and item["tier_code"] is None
    assert item["rating_source"] == "chatgpt" and not item["officially_verified"]
    assert item["source_rating"]["source_id"] == "chatgpt-radar-07"


def test_old_cache_trigger_upgrades_and_direct_rating_write_invalidates_pool(harness):
    submit(harness, rated_payload({"scope": "job", "tier_code": "T3"}))
    assert pool(harness, tier_code="T3")["total"] == 1
    with database.connect() as connection:
        name = "ff_radar_cache_v1_radar_jobs_update"
        connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(PRE_RATING_SQLITE_REVISION_TRIGGERS[name][1])
        install_opportunity_revision(connection)
        before = read_opportunity_revision(connection)
        connection.execute("UPDATE radar_jobs SET source_ratings=?", (json.dumps([
            {"scope": "job", "tier_code": "T1", "source_id": "chatgpt-radar-01"},
        ]),))
        after = read_opportunity_revision(connection)
    assert before != after
    assert pool(harness, tier_code="T3")["total"] == 0
    assert pool(harness, tier_code="T1")["total"] == 1


@pytest.mark.parametrize("rating", [
    {}, {"scope": "job"}, {"scope": "job", "tier_code": "T4"},
    {"scope": "job", "score": -1}, {"scope": "job", "score": 101},
    {"scope": "job", "score": True}, {"scope": "job", "score": "90"},
    {"scope": "company", "tier_code": "T0", "reason": "first\nsecond"},
    {"scope": "job", "score": 90, "reason": "contact private@example.com"},
])
def test_invalid_ratings_are_rejected_by_shared_transport_model(rating):
    with pytest.raises(ValidationError):
        SourceRating.model_validate(rating)
