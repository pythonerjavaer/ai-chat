"""Reuse public scoring inputs; preserve raw listing decisions and legacy callbacks."""

import json
import sqlite3

import pytest

from backend.tests.test_opportunity_scoring_cache import cache_database, harness, public_url  # noqa: F401
from backend.future_radar.repository import RadarRepository, utc_now
from backend.live_sources import is_recruitment_program_listing


def options(main, profile, *, scope, reuse):
    result = {
        "public_url": public_url,
        "prepare": lambda row: main._public_radar_opportunity(row, profile),
        "input_sanitizer": main._public_search_update,
        "cache_scope": scope,
    }
    if reuse:
        result["prepare_sanitized"] = lambda row, public: main._public_radar_opportunity(
            row, profile, public_input=public,
        )
    return result


def test_reused_public_projection_matches_original_scores_programs_sources_and_personal_state(harness):
    from backend import main

    one = harness.insert("one", company="腾讯", source_ratings=[{
        "scope": "job", "tier_code": "T0.5", "score": 88.25, "source_id": "chatgpt-radar-01",
    }])
    program = harness.insert("program", title="示例科技管理培训生项目", tags=[],
                             requirements="培训计划公开信息。" * 50 + "面向2027届应届毕业生。")
    skipped = harness.insert("skip")
    raw_program = harness.repository.get_job(program["id"])
    assert is_recruitment_program_listing(raw_program)
    assert not is_recruitment_program_listing(main._public_search_update(raw_program))
    profile = {"desired_roles": ["数据分析"], "cities": ["上海"], "private_note": "PRIVATE_PROFILE"}
    states = {one["external_id"]: "applied", skipped["id"]: "skipped"}
    original = harness.repository.list_opportunities(
        **options(main, profile, scope="legacy", reuse=False), application_states=states,
    )
    optimized_repo = RadarRepository(harness.connect)
    optimized = optimized_repo.list_opportunities(
        **options(main, profile, scope="reused", reuse=True), application_states=states,
    )
    assert optimized == original
    assert {item["id"] for item in optimized["items"]} == {one["id"], program["id"]}
    by_id = {item["id"]: item for item in optimized["items"]}
    assert by_id[one["id"]]["job_score"] == 88.25
    assert by_id[program["id"]]["scoring_status"] == "unscored_program_listing"
    assert by_id[program["id"]]["tier_code"] is None
    retained = repr(optimized_repo._opportunity_record_cache._entries)
    assert "PRIVATE_PROFILE" not in retained
    assert "PRIVATE_EVIDENCE_NOT_FOR_CACHE" not in retained


@pytest.mark.parametrize("cache_scope", (None, "reused-cold-cache"))
def test_one_sanitizer_per_cold_job_and_no_scorer_repeat_on_cached_browse(harness, monkeypatch, cache_scope):
    from backend import main

    for index in range(12):
        harness.insert(f"one-{index}")
    original = main._public_search_update
    sanitized = []
    scored = []

    def sanitizer(row, **kwargs):
        sanitized.append(row["id"])
        return original(row, **kwargs)

    def prepare_sanitized(row, public):
        scored.append(row["id"])
        return main._public_radar_opportunity(row, {}, public_input=public)

    monkeypatch.setattr(main, "_public_search_update", sanitizer)
    args = options(main, {}, scope=cache_scope, reuse=True) | {"prepare_sanitized": prepare_sanitized}
    first = harness.repository.list_opportunities(**args, page_size=5)
    assert first["total"] == len(sanitized) == len(scored) == 12
    second = harness.repository.list_opportunities(**args, page=2, page_size=5)
    assert second["total"] == first["total"]
    assert len(sanitized) == len(scored) == (12 if cache_scope else 24)


def test_default_custom_prepare_still_receives_raw_row_with_unchanged_contract(harness):
    saved = harness.insert("custom", requirements="raw public requirements " * 100)
    seen = []

    def prepare(row):
        seen.append(row)
        assert len(row["requirements"]) > 320
        return harness.prepare(row)

    result = harness.repository.list_opportunities(
        public_url=public_url, prepare=prepare, input_sanitizer=lambda row: {"id": row["id"]},
        cache_scope="legacy-custom-contract",
    )
    assert result["items"][0]["id"] == saved["id"]
    assert len(seen) == 1


def test_detail_and_changes_reuse_public_input_and_preserve_detail_level(harness, monkeypatch):
    from backend import main

    saved = harness.insert("details", requirements="公开岗位条件。" * 100)
    details = []
    summaries = []
    original = main._public_search_update

    def detail_sanitizer(row):
        details.append(row["id"])
        return original(row, include_detail=True)

    def summary_sanitizer(row):
        summaries.append(row["id"])
        return original(row)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Presenter repeated the sanitizer after receiving public_input")

    monkeypatch.setattr(main, "_public_search_update", forbidden)
    expected_detail = main._public_radar_opportunity(
        harness.repository.get_opportunity(saved["id"], public_url=public_url), {},
        include_detail=True, public_input=original(harness.repository.get_job(saved["id"]), include_detail=True),
    )
    detail = harness.repository.get_prepared_opportunity(
        saved["id"], public_url=public_url, cache_scope="reused-detail",
        prepare=forbidden, input_sanitizer=detail_sanitizer,
        prepare_sanitized=lambda row, public: main._public_radar_opportunity(
            row, {}, include_detail=True, public_input=public,
        ),
    )
    assert details == [saved["id"]]
    assert detail["requirements"] == expected_detail["requirements"]
    assert len(detail["requirements"]) > 320
    with harness.repository.transaction() as connection:
        harness.repository.insert_event(
            connection, run_id="sanitized-change", entity_type="job", entity_id=saved["id"],
            external_id="details", event_type="UPDATED", before=None, after=None,
            fields=["last_seen_at"], source_id="discovery", now=utc_now(),
        )
    changes = harness.repository.list_opportunity_changes(
        public_url=public_url, cache_scope="reused-changes", prepare=forbidden,
        input_sanitizer=summary_sanitizer,
        prepare_sanitized=lambda row, public: main._public_radar_opportunity(row, {}, public_input=public),
    )
    assert summaries == [saved["id"]]
    assert len(changes["items"]) == 1
    assert changes["items"][0]["job"]["requirements"] == ""
    assert "PRIVATE_EVIDENCE_NOT_FOR_CACHE" not in json.dumps(changes)


@pytest.mark.parametrize("reuse", (False, True))
def test_main_pool_uses_fixed_batch_reads_for_fifty_five_jobs_and_one_warm_revision(harness, monkeypatch, reuse):
    from backend import main
    from backend.storage import Connection

    for index in range(55):
        harness.insert(f"fixed-batch-{index}")
    statements = []
    original_connect = harness.repository._connect
    if harness.backend == "sqlite":
        def connect():
            connection = original_connect()
            assert isinstance(connection, sqlite3.Connection)
            connection.set_trace_callback(statements.append)
            return connection
        monkeypatch.setattr(harness.repository, "_connect", connect)
    else:
        original_execute = Connection.execute

        def execute(connection, query, parameters=None):
            statements.append(query)
            return original_execute(connection, query, parameters)
        monkeypatch.setattr(Connection, "execute", execute)
    args = options(main, {}, scope="fixed-batch-reads", reuse=reuse)
    cold = harness.repository.list_opportunities(**args, page_size=10)
    selects = [query for query in statements if query.lstrip().upper().startswith("SELECT")]
    assert cold["total"] == 55
    assert len(selects) == 5, "Two revision reads plus three full-pool batches, independent of job count"
    assert sum("SELECT js.job_id, js.source_id" in query for query in selects) == 1
    statements.clear()
    warm = harness.repository.list_opportunities(**args, page=2, page_size=10)
    assert warm["total"] == 55
    assert len([query for query in statements if query.lstrip().upper().startswith("SELECT")]) == 1
