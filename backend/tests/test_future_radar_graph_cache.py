"""Bounded public graph snapshots; fresh user state and database revisions.

SQLite/loopback PostgreSQL fixtures own isolated databases/schemas. No Neo4j,
production credentials, account mutation or network source access is used.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from backend.tests.test_opportunity_scoring_cache import cache_database, harness, public_url  # noqa: F401
from backend.tests.test_future_radar_graph_projection import GRAPH_FIELDS
from backend.future_radar import repository as repository_module
from backend.future_radar.opportunity_cache import NAMESPACE_KEY, scoring_scope


def graph(harness, *, user=17, states=None, sanitizer=None, aliases=None):
    from backend import main

    return harness.repository.list_graph_opportunities(
        public_url=public_url, input_sanitizer=sanitizer or main._public_search_update_detail,
        company_aliases=aliases or {}, application_states=states,
        cache_scope=scoring_scope(user, {}, "bounded-public-graph-v1"),
    )


def count_candidates(harness, monkeypatch):
    calls = []
    original = harness.repository._opportunity_rows

    def rows(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(harness.repository, "_opportunity_rows", rows)
    return calls


def synthetic_row(index=0):
    key = f"public-job-{index:03}"
    return {
        "id": key, "company": "公开企业", "title": "2027 校园招聘技术分析岗",
        "city": "上海", "industry": "科技", "tags": ["Python"],
        "official_url": f"https://careers.example.invalid/{key}",
        "application_url": f"https://careers.example.invalid/apply/{key}",
        "description": "公开岗位说明。" * 500, "requirements": "掌握 Python 与 SQL",
        "responsibilities": "分析业务数据。", "_member_ids": {key},
        "_member_external_ids": {"PRIVATE_ALIAS_" + key},
        "source_ratings": [{"reason": "PRIVATE_RATING"}], "evidence": "PRIVATE_EVIDENCE",
        "sources": [{"adapter_config": "PRIVATE_CONFIG"}], "profile": "PRIVATE_PROFILE",
    }


def test_warm_snapshot_avoids_candidate_read_and_only_retains_bounded_public_rows(harness, monkeypatch):
    from backend import main

    candidates = []
    sanitized = []

    def rows(**kwargs):
        assert kwargs["include_event_history"] is False
        candidates.append(1)
        return [synthetic_row(index) for index in range(205)]

    def sanitize(row):
        assert set(row) == GRAPH_FIELDS
        sanitized.append(row["id"])
        return main._public_search_update_detail(row) | {"notes": "PRIVATE_NOTES"}

    monkeypatch.setattr(harness.repository, "_opportunity_rows", rows)
    first = graph(harness, states={"PRIVATE_ALIAS_public-job-000": "skipped"}, sanitizer=sanitize)
    second = graph(harness, states={"PRIVATE_ALIAS_public-job-000": "skipped"}, sanitizer=sanitize)
    assert first == second and len(first) == 200
    assert first[0]["id"] == "public-job-001"
    assert len(candidates) == 1 and len(sanitized) == 200
    assert all(set(item) == GRAPH_FIELDS and len(item["description"]) <= 2000 for item in second)
    cache = harness.repository._graph_opportunity_cache
    assert cache.max_entries == 4 and cache.max_bytes == 8 * 1024 * 1024
    assert cache.info()["entries"] == 1 and cache.info()["bytes"] <= cache.max_bytes
    assert "PRIVATE_" not in repr(cache._entries)
    assert "skipped" not in repr(cache._entries)
    first[0]["tags"].clear()
    first[0]["requirements"] = "client mutation"
    assert graph(harness, states={"PRIVATE_ALIAS_public-job-000": "skipped"}, sanitizer=sanitize) == second


def test_warm_graph_checks_one_revision_without_provenance_or_event_queries(harness, monkeypatch):
    harness.insert("one")
    statements = []
    original_connect = harness.repository._connect

    if harness.backend == "sqlite":
        def connect():
            connection = original_connect()
            connection.set_trace_callback(
                lambda statement: statements.append(statement) if statement.lstrip().upper().startswith("SELECT") else None,
            )
            return connection
        monkeypatch.setattr(harness.repository, "_connect", connect)
    else:
        from backend.storage import Connection
        execute = Connection.execute

        def traced(connection, statement, parameters=None):
            statements.append(statement)
            return execute(connection, statement, parameters)
        monkeypatch.setattr(Connection, "execute", traced)
    assert len(graph(harness)) == 1
    assert len(statements) == 5, "Two revision reads plus three candidate batches on cold load"
    statements.clear()
    assert len(graph(harness)) == 1
    assert len(statements) == 1 and "system_state" in statements[0]
    assert "radar_events" not in statements[0] and "job_sources" not in statements[0]


@pytest.mark.parametrize("mutation", ("closed", "retired", "expired", "authoritative-closed"))
def test_revision_invalidates_closed_expired_retired_or_authoritative_duplicate(harness, monkeypatch, mutation):
    title = "2027 校园招聘数据分析岗"
    saved = harness.insert("one", title=title, closing_date="2099-01-01")
    calls = count_candidates(harness, monkeypatch)
    assert graph(harness)[0]["id"] == saved["id"]
    assert graph(harness)[0]["id"] == saved["id"] and len(calls) == 1
    if mutation == "authoritative-closed":
        harness.insert("closed-copy", source="official", title=title,
                       status="closed", verification_status="verified", closing_date="2099-01-01")
    else:
        with harness.repository.transaction() as connection:
            if mutation == "closed":
                connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (saved["id"],))
            elif mutation == "retired":
                connection.execute("UPDATE job_sources SET active=0 WHERE job_id=?", (saved["id"],))
            else:
                connection.execute("UPDATE radar_jobs SET closing_date='2000-01-01' WHERE id=?", (saved["id"],))
    assert graph(harness) == [] and len(calls) == 2


def test_ordered_alias_choices_and_user_scope_never_reuse_wrong_visibility(harness, monkeypatch):
    lead = harness.insert("lead", title="2027 校园招聘数据分析岗", city="上海市")
    official = harness.insert("official", source="official", title="2027 校园招聘数据分析岗",
                              city="上海", verification_status="verified")
    calls = count_candidates(harness, monkeypatch)
    hidden = {official["id"]: "planned", lead["external_id"]: "skipped"}
    visible = {lead["external_id"]: "skipped", official["id"]: "planned"}
    assert hidden == visible, "Only durable update/insertion order differs"
    assert graph(harness, user=1, states=hidden) == []
    assert graph(harness, user=1, states=visible)[0]["id"] == official["id"]
    assert len(calls) == 2
    assert graph(harness, user=1, states=visible)[0]["id"] == official["id"] and len(calls) == 2
    assert graph(harness, user=2, states=visible)[0]["id"] == official["id"] and len(calls) == 3
    assert graph(harness, user=2, states={})[0]["id"] == official["id"] and len(calls) == 4
    assert "skipped" not in repr(harness.repository._graph_opportunity_cache._entries)


def test_new_authoritative_alias_preserves_existing_personal_skip_after_revision(harness):
    lead = harness.insert("lead", title="2027 校园招聘数据分析岗")
    states = {lead["external_id"]: "skipped"}
    assert graph(harness, states=states) == []
    official = harness.insert("official", source="official", title="2027 校园招聘数据分析岗",
                              verification_status="verified")
    assert graph(harness, states=states) == []
    assert graph(harness, states={**states, official["id"]: "applied"})[0]["id"] == official["id"]


def test_date_boundary_rebuilds_and_removes_jobs_expiring_that_day(harness, monkeypatch):
    tomorrow = date.today() + timedelta(days=1)
    harness.insert("expires", closing_date=tomorrow.isoformat())
    calls = count_candidates(harness, monkeypatch)
    assert len(graph(harness)) == 1

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr(repository_module, "date", Tomorrow)
    monkeypatch.setattr(repository_module, "date_boundary", lambda: (tomorrow.isoformat(), tomorrow.isoformat()))
    assert graph(harness) == [] and len(calls) == 2


def test_sanitizer_url_alias_and_database_epoch_changes_are_distinct(harness, monkeypatch):
    from backend import main

    harness.insert("one")
    calls = count_candidates(harness, monkeypatch)
    assert len(graph(harness)) == 1
    assert len(graph(harness, sanitizer=lambda row: main._public_search_update_detail(row))) == 1
    assert len(graph(harness, aliases={"public-alias": "canonical"})) == 1
    assert len(calls) == 3
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE system_state SET value='fresh-test-epoch' WHERE key=?", (NAMESPACE_KEY,))
    assert len(graph(harness)) == 1 and len(calls) == 4
    assert len(harness.repository.list_graph_opportunities(
        public_url=lambda value: public_url(value), input_sanitizer=main._public_search_update_detail,
        cache_scope=scoring_scope(17, {}, "bounded-public-graph-v1"),
    )) == 1 and len(calls) == 5


def test_revision_change_during_build_never_publishes_closed_snapshot(harness):
    from backend import main

    saved = harness.insert("close-during-build")
    closed = []

    def sanitize(row):
        if not closed:
            closed.append(1)
            with harness.repository.transaction() as connection:
                connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (saved["id"],))
        return main._public_search_update_detail(row)

    assert graph(harness, sanitizer=sanitize) == []
    assert len(closed) == 1
    entries = harness.repository._graph_opportunity_cache._entries
    assert len(entries) == 1 and all(entry[2] == [] for entry in entries.values())


def test_continuous_revision_change_returns_live_uncached_projection(harness, monkeypatch):
    harness.insert("one")
    original = harness.repository._opportunity_revision()
    revision_calls = []

    def revision():
        revision_calls.append(1)
        return (*original[:2], original[2] + len(revision_calls))

    calls = count_candidates(harness, monkeypatch)
    monkeypatch.setattr(harness.repository, "_opportunity_revision", revision)
    assert len(graph(harness)) == 1 and len(calls) == 3
    assert harness.repository._graph_opportunity_cache.info()["entries"] == 0


def test_missing_revision_or_scope_uses_live_reads_without_anonymous_cache(harness, monkeypatch):
    from backend import main

    harness.insert("one")
    calls = count_candidates(harness, monkeypatch)
    assert len(harness.repository.list_graph_opportunities(
        public_url=public_url, input_sanitizer=main._public_search_update_detail,
    )) == 1
    with harness.repository.transaction() as connection:
        connection.execute("DELETE FROM system_state WHERE key=?", (NAMESPACE_KEY,))
    assert len(graph(harness)) == len(graph(harness)) == 1
    assert len(calls) == 3 and harness.repository._graph_opportunity_cache.info()["entries"] == 0


def test_same_scope_singleflight_does_not_block_other_users_and_returns_detached_copies(harness, monkeypatch):
    started, release, follower_started = threading.Event(), threading.Event(), threading.Event()
    build_calls = []
    guard = threading.Lock()

    def rows(**_kwargs):
        with guard:
            build_calls.append(1)
            first = len(build_calls) == 1
        if first:
            started.set()
            assert release.wait(5)
        return [synthetic_row()]

    def follower():
        follower_started.set()
        return graph(harness, user=1)

    monkeypatch.setattr(harness.repository, "_opportunity_rows", rows)
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(graph, harness, user=1)
        try:
            assert started.wait(5)
            second = executor.submit(follower)
            assert follower_started.wait(5)
            other = executor.submit(graph, harness, user=2)
            assert len(other.result(timeout=5)) == 1
        finally:
            release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert a == b and len(build_calls) == 2
    a[0]["tags"].clear()
    assert b[0]["tags"] == ["Python"]
    assert graph(harness, user=1) == b and len(build_calls) == 2
    assert harness.repository._graph_opportunity_cache.info()["inflight"] == 0


def test_graph_lru_budget_is_bounded_and_does_not_evict_scored_pool(harness):
    harness.insert("one")
    assert harness.pool()["total"] == 1
    for user in range(6):
        assert len(graph(harness, user=user)) == 1
    cache = harness.repository._graph_opportunity_cache
    assert cache.info()["entries"] == 4 and cache.info()["bytes"] <= 8 * 1024 * 1024
    assert harness.pool()["total"] == 1 and len(harness.prepared) == 1
    retained = json.dumps([entry[2] for entry in cache._entries.values()])
    assert "PRIVATE_" not in retained and "application_status" not in retained
