"""Unchanged public scores survive source churn; current facts never do.

Use the same isolated SQLite/PostgreSQL fixture as the pool revision tests.
Work-count assertions measure avoided scoring without machine timing flakes.
"""

import json

import pytest

from backend.tests.test_opportunity_scoring_cache import cache_database, harness, public_url
from backend.future_radar import repository as repository_module
from backend.future_radar.opportunity_cache import (
    BoundedScoringCache, NAMESPACE_KEY, scoring_scope,
)


@pytest.fixture
def record_pool(harness):
    from backend import main

    calls = []

    def pool(*, profile=None, user=1, **kwargs):
        profile = profile or {}

        def prepare(row):
            calls.append(row["external_id"])
            return main._public_radar_opportunity(row, profile)

        return harness.repository.list_opportunities(
            public_url=public_url, prepare=prepare,
            input_sanitizer=main._public_search_update,
            cache_scope=scoring_scope(user, profile, "record-cache-test-rules"), **kwargs,
        )

    return pool, calls


def test_observation_and_event_updates_refresh_public_metadata_without_rescoring(harness, record_pool):
    pool, calls = record_pool
    saved = harness.insert("one")
    old = pool()["items"][0]
    stamp = "2026-09-06T12:34:56+00:00"
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET last_seen_at=?,last_changed_at=? WHERE id=?", (stamp, stamp, saved["id"]))
        connection.execute("UPDATE job_sources SET last_seen_at=? WHERE job_id=?", (stamp, saved["id"]))
        harness.repository.insert_event(
            connection, run_id="isolated-observation", entity_type="job",
            entity_id=saved["id"], external_id="one", event_type="UPDATED",
            before=None, after=None, fields=["last_seen_at"],
            source_id="discovery", now=stamp,
        )
    new = pool()["items"][0]
    assert calls == ["one"]
    assert old["last_seen_at"] != stamp
    assert new["last_seen_at"] == new["last_changed_at"] == new["latest_event_at"] == stamp
    assert new["latest_event_type"] == "UPDATED"
    assert new["sources"][0]["last_seen_at"] == stamp
    assert new["discovered_by"][0]["last_seen_at"] == stamp
    assert new["tier_code"] == old["tier_code"]


def test_three_live_revision_retries_score_each_unchanged_record_once(harness, record_pool, monkeypatch):
    pool, calls = record_pool
    for index in range(32):
        harness.insert(f"one-{index}")
    repository = harness.repository
    original_revision = repository._opportunity_revision()
    revisions = [0]
    builds = [0]
    original_build = repository._prepare_opportunity_pool

    def moving_revision():
        revisions[0] += 1
        return (*original_revision[:2], original_revision[2] + revisions[0])

    def build(**kwargs):
        builds[0] += 1
        return original_build(**kwargs)

    monkeypatch.setattr(repository, "_opportunity_revision", moving_revision)
    monkeypatch.setattr(repository, "_prepare_opportunity_pool", build)
    assert pool()["total"] == 32
    assert builds == [3], "Preserve the existing stable-snapshot retry policy"
    assert len(calls) == 32, "Do not score all 32 records on each of three retries"
    assert repository._opportunity_cache.info()["entries"] == 0
    assert pool()["total"] == 32
    assert builds == [6] and len(calls) == 32


def test_one_changed_rating_rescores_only_that_record_and_tier_filter_is_exact(harness, record_pool):
    pool, calls = record_pool
    one = harness.insert("one", source_ratings=[{
        "scope": "job", "tier_code": "T2", "source_id": "chatgpt-radar-01",
    }])
    harness.insert("two")
    assert pool(filters={"tier_code": "T2"})["total"] >= 1
    assert len(calls) == 2
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET source_ratings=? WHERE id=?", (json.dumps([{
            "scope": "job", "tier_code": "T0.5", "score": 88.25,
            "source_id": "chatgpt-radar-01",
        }]), one["id"]))
    changed = pool(filters={"tier_code": "T0.5"})
    assert changed["total"] == 1
    assert changed["items"][0]["job_score"] == 88.25
    assert calls.count("one") == 2 and calls.count("two") == 1


def test_new_closure_removes_cached_job_and_archive_has_current_status(harness, record_pool):
    pool, calls = record_pool
    saved = harness.insert("one")
    assert pool()["total"] == 1
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (saved["id"],))
    assert pool()["total"] == 0 and calls == ["one"]
    archive = pool(filters={"status": "all", "active_only": False})
    assert archive["items"][0]["status"] == "closed"
    assert calls == ["one", "one"]


def test_official_closed_duplicate_wins_over_cached_source_lead(harness, record_pool):
    pool, calls = record_pool
    harness.insert("pending", title="2027 校园招聘风险分析岗")
    assert pool()["total"] == 1
    harness.insert("official", source="official", title="2027 校园招聘风险分析岗",
                   status="closed", verification_status="verified")
    assert pool()["total"] == 0
    assert pool(filters={"source_id": "discovery"})["total"] == 0
    archive = pool(filters={"status": "all", "active_only": False})
    assert archive["items"][0]["external_id"] == "official"
    assert archive["items"][0]["status"] == "closed"


def test_source_authority_and_safety_fields_never_reuse_an_old_rating(harness, record_pool):
    pool, calls = record_pool
    saved = harness.insert("one")
    pool()
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE monitor_sources SET name='Fresh public source' WHERE id='discovery'")
    assert pool()["items"][0]["sources"][0]["name"] == "Fresh public source"
    assert calls == ["one", "one"]
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET verification_status='source_screened' WHERE id=?", (saved["id"],))
    item = pool()["items"][0]
    assert item["verification_status"] == "source_screened"
    assert item["source_screened"] and not item["officially_verified"]
    assert calls == ["one", "one", "one"]
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE job_sources SET active=0 WHERE job_id=?", (saved["id"],))
    assert pool()["total"] == 0


def test_user_profile_date_and_database_epoch_isolate_record_cache(harness, record_pool, monkeypatch):
    pool, calls = record_pool
    harness.insert("one")
    private_profile = {"private_note": "PRIVATE_PROFILE_NOT_RETAINED", "target_roles": ["数据分析"]}
    pool(profile=private_profile)
    pool(profile=private_profile, user=2)
    pool(profile={**private_profile, "target_roles": ["风险管理"]})
    assert len(calls) == 3
    current_day = repository_module.date_boundary()
    monkeypatch.setattr(repository_module, "date_boundary", lambda: ("2099-01-01", current_day[1]))
    pool(profile=private_profile)
    assert len(calls) == 4
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE system_state SET value='new-isolated-database-epoch' WHERE key=?", (NAMESPACE_KEY,))
    pool(profile=private_profile)
    assert len(calls) == 5
    retained = repr(harness.repository._opportunity_record_cache._entries)
    assert "PRIVATE_PROFILE_NOT_RETAINED" not in retained
    assert "PRIVATE_EVIDENCE_NOT_FOR_CACHE" not in retained


def test_deadline_input_change_cannot_keep_a_cached_active_job(harness, record_pool):
    pool, calls = record_pool
    saved = harness.insert("one", closing_date="2099-01-01")
    assert pool()["total"] == 1
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET closing_date='2000-01-01' WHERE id=?", (saved["id"],))
    assert pool()["total"] == 0
    item = pool(filters={"status": "all", "active_only": False})["items"][0]
    assert item["closing_date"] == "2000-01-01" and item["days_left"] < 0
    assert len(calls) == 2


def test_record_results_are_detached_from_pool_grouping_and_client_mutation(harness, record_pool):
    pool, calls = record_pool
    saved = harness.insert("one")
    result = pool()
    result["items"][0]["score_breakdown"].clear()
    result["items"][0]["sources"][0]["name"] = "MUTATED"
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET last_seen_at='2099-01-01' WHERE id=?", (saved["id"],))
    fresh = pool()["items"][0]
    assert fresh["score_breakdown"]
    assert fresh["sources"][0]["name"] != "MUTATED"
    assert calls == ["one"]
    cached = next(iter(harness.repository._opportunity_record_cache._entries.values()))[2]
    assert "display_company_key" not in cached


def test_refresh_cache_expiration_keeps_lru_deadlines_ordered():
    clock = [0.0]
    cache = BoundedScoringCache(max_entries=10_000, max_bytes=10_000_000,
                               ttl_seconds=10, refresh_on_hit=True, clock=lambda: clock[0])
    for index in range(5000):
        cache.get_or_compute(index, lambda: {"number": index})
    clock[0] = 9
    for index in (0, 2000, 4999):
        cache.get_or_compute(index, lambda: pytest.fail("unexpected cache miss"))
    clock[0] = 11
    assert cache.info()["entries"] == 3
    assert cache.get_or_compute(2000, lambda: pytest.fail("unexpected cache miss"))["number"] == 2000
    clock[0] = 20
    assert cache.info()["entries"] == 1
    clock[0] = 22
    assert cache.info()["entries"] == 0
