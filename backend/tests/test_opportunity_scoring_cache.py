"""Private temporary DBs only: no auth, credentials, source network, or AI.

SQLite always runs; PostgreSQL requires the explicitly provisioned loopback
frostfire_test cluster. Each case owns a fresh randomly named private schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest


os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "not-used-in-opportunity-cache-tests")
os.environ.setdefault("JWT_SECRET", "isolated-cache-test-secret-at-least-32-bytes")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend.future_radar.opportunity_cache import (
    BoundedScoringCache, NAMESPACE_KEY, REVISION_KEY,
    install_opportunity_revision, read_opportunity_revision, scoring_scope,
)
from backend.future_radar.repository import RadarRepository, utc_now
from backend.future_radar.schema import migrate


def public_url(value):
    return value if isinstance(value, str) and value.startswith("https://careers.example.invalid/") else None


@pytest.fixture(params=("sqlite", "postgres"))
def cache_database(request, tmp_path):
    cleanup = lambda: None
    if request.param == "sqlite":
        path = tmp_path / "isolated-scoring.db"
        process_target = {"backend": "sqlite", "path": str(path)}

        def connect():
            connection = sqlite3.connect(path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        connection = connect()
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
    else:
        dsn = os.environ.get("FROSTFIRE_TEST_POSTGRES_URL")
        if not dsn:
            pytest.skip("FROSTFIRE_TEST_POSTGRES_URL is not configured")
        psycopg = pytest.importorskip("psycopg")
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict
        from backend.storage import close_postgres_pools, connect_postgres

        info = conninfo_to_dict(dsn)
        local = {"127.0.0.1", "::1", "localhost"}
        assert info.get("host") in local and info.get("hostaddr", info["host"]) in local
        assert info.get("dbname") == info.get("user") == "frostfire_test"
        assert not info.get("password"), "No credentialed database access in these tests"
        schema = f"ff_score_test_{uuid.uuid4().hex}"
        process_target = {"backend": "postgres", "dsn": dsn, "schema": schema}
        with psycopg.connect(dsn, autocommit=True) as raw:
            raw.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        def connect():
            return connect_postgres(dsn, schema=schema, timeout=5, max_size=4)

        def cleanup():
            close_postgres_pools()
            with psycopg.connect(dsn, autocommit=True) as raw:
                raw.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    try:
        connection = connect()
        try:
            migrate(connection)
            connection.execute(
                "CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            install_opportunity_revision(connection)
            connection.commit()
        finally:
            connection.close()
        yield SimpleNamespace(connect=connect, backend=request.param, process_target=process_target)
    finally:
        cleanup()


@pytest.fixture
def harness(cache_database):
    repository = RadarRepository(cache_database.connect)
    repository.seed_sources([
        {"id": "discovery", "name": "公开招聘线索", "source_type": "chatgpt_sync", "trust_level": "discovery"},
        {"id": "official", "name": "企业招聘官网", "source_type": "official_api", "trust_level": "official"},
    ])

    def insert(key, *, source="discovery", program_id=None, **overrides):
        item = {
            "external_id": key, "company": "示例科技",
            "title": f"2027 校园招聘数据分析岗 {key}", "city": "上海",
            "region": "中国大陆", "employer_type": "互联网企业", "industry": "科技",
            "primary_category": "internet_tech", "status": "open",
            "verification_status": "pending", "tags": ["校园招聘", "2027届"],
            "official_url": f"https://careers.example.invalid/campus/{key}",
            "description": "参与业务数据分析和金融风险研究。",
            "responsibilities": "分析经营数据并构建业务看板。",
            "requirements": "面向2027届毕业生，掌握数据分析技能。",
            "content_hash": key, **overrides,
        }
        src = repository.get_source(source)
        with repository.transaction() as connection:
            saved = repository.insert_job(connection, item, source_id=source, program_id=program_id, now=utc_now())
            repository.link_job_source(
                connection, job_id=saved["id"], source=src,
                source_url=item["official_url"], now=utc_now(),
                verification_role="verification" if source == "official" else "discovery",
                evidence=["PRIVATE_EVIDENCE_NOT_FOR_CACHE"],
            )
        return saved

    prepared = []

    def prepare(row):
        prepared.append(row["external_id"])
        # Mimic the actual API sanitizer boundary: no raw fields/DB internals
        # or source evidence/configuration can be retained in a cache entry.
        return {
            key: row.get(key) for key in (
                "id", "external_id", "company", "title", "city", "primary_category",
                "status", "verification_status", "requirements", "official_url",
                "program_name", "recruitment_year", "latest_event_type", "sources",
            )
        } | {"tier_code": "T0" if "top" in row["external_id"] else "T2"}

    def pool(*, repo=None, scope="opaque-test-user-profile-rules", **kwargs):
        return (repo or repository).list_opportunities(
            public_url=public_url, prepare=prepare, cache_scope=scope, **kwargs,
        )

    return SimpleNamespace(
        repository=repository, connect=cache_database.connect, backend=cache_database.backend,
        insert=insert, prepare=prepare, prepared=prepared, pool=pool,
        process_target=cache_database.process_target,
    )


def revision(harness):
    connection = harness.connect()
    try:
        return read_opportunity_revision(connection)
    finally:
        connection.close()


def test_tier_and_page_switches_score_all_once_with_exact_full_counts(harness):
    for index in range(57):
        harness.insert(f"{'top' if index % 3 == 0 else 'other'}-{index:02}",
                       primary_category="internet_tech" if index % 2 else "securities_funds")
    all_items = harness.pool(page=1, page_size=7)
    assert all_items["total"] == 57 and len(all_items["items"]) == 7
    assert len(harness.prepared) == 57
    second_page = harness.pool(page=2, page_size=7, filters={"tier_code": "T0"})
    assert second_page["total"] == 19 and len(second_page["items"]) == 7
    assert second_page["stats"]["matching_total"] == 57
    assert second_page["stats"]["tier_counts"]["T0"] == 19
    assert second_page["stats"]["tier_counts"]["T2"] == 38
    assert second_page["stats"]["category_counts"] == {"internet_tech": 28, "securities_funds": 29}
    assert all(item["tier_code"] == "T0" for item in second_page["items"])
    assert len(harness.prepared) == 57
    harness.pool(page=3, page_size=5, filters={"tier_code": "T2"})
    assert len(harness.prepared) == 57


def test_priority_tier_view_and_company_projections_share_scores_and_keep_secondary_details(harness):
    rows = [
        ("top-alpha", "Alpha", "internet_tech"),
        ("regular-alpha", "Alpha", "internet_tech"),
        ("unranked-alpha", "Alpha", "policy_state_banks"),
        ("secondary-alpha", "Alpha", "state_tech_telecom"),
        ("top-beta", "Beta", "policy_state_banks"),
        ("secondary-carrier", "中国电信", "state_tech_telecom"),
        ("unranked-bank", "示例银行", "policy_state_banks"),
    ]
    saved = {
        key: harness.insert(key, company=company, primary_category=category)
        for key, company, category in rows
    }

    def prepare(row):
        item = harness.prepare(row)
        if row["external_id"].startswith("secondary-"):
            item["tier_code"] = "不建议投"
        elif row["external_id"].startswith("unranked-"):
            item["tier_code"] = None
        return item

    def project(**kwargs):
        return harness.repository.list_opportunities(
            public_url=public_url, prepare=prepare,
            cache_scope="isolated-priority-projections", **kwargs,
        )

    full = project()
    assert full["total"] == 7
    focused = project(page_size=2, filters={"priority_only": True})
    assert len(focused["items"]) == 2 and focused["total"] == 5
    assert focused["stats"]["priority_total"] == 5
    assert focused["stats"]["secondary_total"] == 2
    assert focused["stats"]["visible_category_counts"] == {"internet_tech": 2, "policy_state_banks": 3}
    assert focused["stats"]["visible_category_company_counts"] == {"internet_tech": 1, "policy_state_banks": 3}
    project(page=2, page_size=2, filters={"priority_only": True})
    groups = project(filters={"view": "companies", "priority_only": True})
    assert groups["total_companies"] == 3
    alpha_key = next(group["company_key"] for group in groups["items"] if group["company_name"] == "Alpha")
    for priority_only in (False, True):
        for tier in ("T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3", "UNRANKED", "BELOW_PRIORITY"):
            page = project(filters={"priority_only": priority_only, "tier_code": tier, "view": "companies"})
            assert page["total_opportunities"] == (
                0 if priority_only and tier == "BELOW_PRIORITY" else full["stats"]["tier_counts"][tier]
            )
    expanded = project(filters={"company_key": alpha_key, "priority_only": True})
    assert expanded["total_opportunities"] == 3
    assert expanded["stats"]["matching_total"] == 4
    assert expanded["stats"]["priority_total"] == 3 and expanded["stats"]["secondary_total"] == 1
    # Mutating one response must not corrupt another projection's counters.
    focused["stats"]["visible_category_counts"]["internet_tech"] = 999
    focused["stats"]["visible_category_company_counts"]["internet_tech"] = 999
    assert project(filters={"priority_only": True})["stats"]["visible_category_counts"]["internet_tech"] == 2
    assert project(filters={"priority_only": True})["stats"]["visible_category_company_counts"]["internet_tech"] == 1
    detail = harness.repository.get_prepared_opportunity(
        saved["secondary-carrier"]["id"], public_url=public_url, prepare=prepare,
        cache_scope="isolated-priority-projections",
    )
    assert detail["tier_code"] == "不建议投"
    assert detail["company"] == "中国电信"
    assert {item["id"] for item in project()["items"]} == {row["id"] for row in saved.values()}
    assert len(harness.prepared) == len(rows), "priority is only a projection of the same complete scored cache"


def test_priority_projection_refreshes_after_native_scoring_category_and_status_changes(harness):
    harness.insert("top-steady", company="Alpha", primary_category="internet_tech")
    changed = harness.insert(
        "other-changing", company="示例银行", primary_category="state_tech_telecom",
        requirements="公开次级线索",
    )

    def prepare(row):
        item = harness.prepare(row)
        if row["requirements"] == "公开次级线索":
            item["tier_code"] = "不建议投"
        return item

    def project(*, repository=None, **filters):
        return (repository or harness.repository).list_opportunities(
            public_url=public_url, prepare=prepare,
            cache_scope="isolated-priority-mutations", filters={"priority_only": True, **filters},
        )

    other_worker = RadarRepository(harness.connect)
    assert project()["total_opportunities"] == project(repository=other_worker)["total_opportunities"] == 1
    assert project()["stats"]["secondary_total"] == 1
    before = revision(harness)
    with other_worker.transaction() as connection:
        # Simulate a source update without changing any timestamp or invoking
        # a repository invalidation hook; database revisions own invalidation.
        connection.execute(
            "UPDATE radar_jobs SET requirements=?, primary_category=? WHERE id=?",
            ("新的公开资格条件", "policy_state_banks", changed["id"]),
        )
    assert revision(harness) != before
    for repository in (harness.repository, other_worker):
        fresh = project(repository=repository)
        assert fresh["total_opportunities"] == fresh["stats"]["priority_total"] == 2
        assert fresh["stats"]["secondary_total"] == 0
        assert fresh["stats"]["visible_category_counts"] == {"internet_tech": 1, "policy_state_banks": 1}
        assert fresh["stats"]["visible_category_company_counts"] == {"internet_tech": 1, "policy_state_banks": 1}
    with other_worker.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (changed["id"],))
    assert project()["total_opportunities"] == project(repository=other_worker)["total_opportunities"] == 1
    archive = project(priority_only=False, status="all", active_only=False)
    assert archive["total_opportunities"] == 2
    assert next(item for item in archive["items"] if item["id"] == changed["id"])["status"] == "closed"
    with harness.connect() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM radar_jobs").fetchone()["count"] == 2


def test_profile_user_rule_and_alias_keys_are_independent(harness):
    harness.insert("top-role")
    profile = {"target_roles": ["数据分析"], "updated_at": "same-second", "private_note": "PRIVATE_PROFILE_TEXT"}
    keys = [
        scoring_scope(1, profile, "v1"),
        scoring_scope(2, profile, "v1"),
        scoring_scope(1, {**profile, "target_roles": ["风险管理"]}, "v1"),
        scoring_scope(1, profile, "v2"),
        scoring_scope(1, {**profile, "updated_at": "new-version"}, "v1"),
    ]
    assert len(set(keys)) == 5
    for key in keys:
        harness.pool(scope=key)
        harness.pool(scope=key)
    assert len(harness.prepared) == 5
    harness.pool(scope=keys[-1], company_aliases={"示例": "示例科技"})
    assert len(harness.prepared) == 6
    retained = repr(harness.repository._opportunity_cache._entries)
    assert "PRIVATE_PROFILE_TEXT" not in retained
    assert "PRIVATE_EVIDENCE_NOT_FOR_CACHE" not in retained


def test_returned_pages_and_details_cannot_mutate_a_cached_result(harness):
    saved = harness.insert("top-one")
    first = harness.pool()
    first["items"][0]["title"] = "MUTATED"
    first["items"][0]["sources"][0]["name"] = "MUTATED"
    first["stats"]["tier_counts"]["T0"] = 999
    detail = harness.repository.get_prepared_opportunity(
        saved["id"], public_url=public_url, prepare=harness.prepare,
        cache_scope="opaque-test-user-profile-rules",
    )
    assert detail["title"] == saved["title"]
    detail["sources"][0]["name"] = "ALSO MUTATED"
    second = harness.pool()
    assert second["items"][0]["sources"][0]["name"] == "公开招聘线索"
    assert second["stats"]["tier_counts"]["T0"] == 1
    assert len(harness.prepared) == 1


def test_other_worker_insert_change_close_delete_invalidate_without_timestamp_tick(harness):
    other_worker = RadarRepository(harness.connect)
    one = harness.insert("top-first")
    assert harness.pool()["total"] == harness.pool(repo=other_worker)["total"] == 1
    first_version = revision(harness)
    two = harness.insert("top-second")
    assert revision(harness) != first_version
    assert harness.pool()["total"] == harness.pool(repo=other_worker)["total"] == 2
    with other_worker.transaction() as connection:
        # Deliberately do not change updated_at or last_changed_at.
        connection.execute("UPDATE radar_jobs SET requirements=? WHERE id=?", ("新的公开资格条件", one["id"]))
    results = harness.pool()["items"]
    assert next(item for item in results if item["id"] == one["id"])["requirements"] == "新的公开资格条件"
    with other_worker.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (two["id"],))
    assert harness.pool()["total"] == harness.pool(repo=other_worker)["total"] == 1
    with other_worker.transaction() as connection:
        connection.execute("DELETE FROM radar_jobs WHERE id=?", (one["id"],))
    assert harness.pool()["total"] == harness.pool(repo=other_worker)["total"] == 0


def test_separate_process_native_sql_write_invalidates_warm_worker(harness):
    saved = harness.insert("top-before-process")
    assert harness.pool()["total"] == 1
    before = revision(harness)
    # A native DB client in a different process bypasses every repository
    # write method. Only the database trigger can invalidate this worker.
    script = """
import json, sqlite3, sys
target = json.loads(sys.argv[1])
if target['backend'] == 'sqlite':
    with sqlite3.connect(target['path'], timeout=5) as connection:
        connection.execute('UPDATE radar_jobs SET status=? WHERE id=?', ('closed', sys.argv[2]))
else:
    import psycopg
    from psycopg import sql
    with psycopg.connect(target['dsn']) as connection:
        connection.execute(sql.SQL('UPDATE {}.radar_jobs SET status=%s WHERE id=%s').format(
            sql.Identifier(target['schema'])), ('closed', sys.argv[2]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(harness.process_target), saved["id"]],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, "Isolated native SQL subprocess failed"
    assert revision(harness) != before
    assert harness.pool()["total"] == 0


def test_category_filter_totals_refresh_after_reclassification(harness):
    saved = harness.insert("top-finance", primary_category="securities_funds")
    harness.insert("other-internet")
    selected = {"primary_categories": ["securities_funds"]}
    assert harness.pool(filters=selected)["total"] == 1
    assert harness.pool(filters={**selected, "tier_code": "T0"})["total"] == 1
    assert len(harness.prepared) == 1
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET primary_category='internet_tech' WHERE id=?", (saved["id"],))
    assert harness.pool(filters=selected)["total"] == 0
    internet = harness.pool(filters={"primary_categories": ["internet_tech"]})
    assert internet["total"] == 2
    assert internet["stats"]["category_counts"] == {"internet_tech": 2}


def test_source_health_noop_or_private_evidence_do_not_flush_scores(harness):
    saved = harness.insert("top-one")
    harness.pool()
    before = revision(harness)
    with harness.repository.transaction() as connection:
        connection.execute(
            "UPDATE monitor_sources SET status='running', last_checked_at=?, last_error=?, lease_owner=? WHERE id='discovery'",
            (utc_now(), "private operational error", "another-worker"),
        )
        connection.execute("UPDATE job_sources SET evidence=? WHERE job_id=?", ('["private changed evidence"]', saved["id"]))
        connection.execute("UPDATE radar_jobs SET requirements=requirements, status=status WHERE id=?", (saved["id"],))
    assert revision(harness) == before
    harness.pool()
    assert len(harness.prepared) == 1
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE monitor_sources SET name='新的公开来源名称' WHERE id='discovery'")
    assert harness.pool()["items"][0]["sources"][0]["name"] == "新的公开来源名称"
    assert len(harness.prepared) == 2
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE job_sources SET active=0 WHERE job_id=?", (saved["id"],))
    assert harness.pool()["total"] == 0


def test_source_authority_change_and_closed_winner_never_resurrect_cached_discovery(harness):
    pending = harness.insert("top-discovery", title="2027 校园招聘风险分析岗")
    official = harness.insert(
        "top-official", source="official", title="2027 校园招聘风险分析岗", verification_status="verified",
    )
    selected = {"source_id": "discovery"}
    assert harness.pool(filters=selected)["items"][0]["id"] == official["id"]
    detail = harness.repository.get_prepared_opportunity(
        pending["external_id"], public_url=public_url, prepare=harness.prepare,
        cache_scope="opaque-test-user-profile-rules",
    )
    assert detail["id"] == official["id"] and len(harness.prepared) == 1
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (official["id"],))
    assert harness.pool(filters=selected)["total"] == 0
    assert harness.pool(filters={**selected, "verification_status": "pending"})["total"] == 0
    archived = harness.repository.get_prepared_opportunity(
        pending["id"], public_url=public_url, prepare=harness.prepare,
        cache_scope="opaque-test-user-profile-rules",
    )
    assert archived["id"] == official["id"] and archived["status"] == "closed"


def test_program_event_trust_and_deadline_changes_have_exact_revisions(harness):
    now = utc_now()
    with harness.repository.transaction() as connection:
        program = harness.repository.insert_program(connection, {
            "external_id": "program-one", "company": "示例科技", "program_name": "示例科技2027校园招聘",
            "recruitment_year": 2027, "recruitment_type": "campus", "content_hash": "program",
        }, source_id="discovery", now=now)
    saved = harness.insert("top-role", program_id=program["id"])
    harness.pool()
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE recruitment_programs SET recruitment_year=2028 WHERE id=?", (program["id"],))
    assert harness.pool()["items"][0]["recruitment_year"] == 2028
    with harness.repository.transaction() as connection:
        harness.repository.insert_event(
            connection, run_id="synthetic-run", entity_type="job", entity_id=saved["id"],
            external_id=saved["external_id"], event_type="UPDATED", before=None, after=None,
            fields=["requirements"], source_id="discovery", now=now,
        )
    assert harness.pool(filters={"event_type": "UPDATED"})["items"][0]["latest_event_type"] == "UPDATED"
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET closing_date=? WHERE id=?", (date.today().isoformat(), saved["id"]))
    assert harness.pool()["total"] == 0
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET closing_date=NULL WHERE id=?", (saved["id"],))
    assert harness.pool()["total"] == 1
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE monitor_sources SET trust_level='official' WHERE id='discovery'")
    assert harness.pool()["total"] == 0  # Pending is not an official verification.


def test_rolled_back_writer_does_not_invalidate_and_install_is_idempotent(harness):
    saved = harness.insert("top-one")
    harness.pool()
    before = revision(harness)
    connection = harness.connect()
    try:
        connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (saved["id"],))
        connection.rollback()
        install_opportunity_revision(connection)
        connection.commit()
    finally:
        connection.close()
    assert revision(harness) == before
    assert harness.pool()["total"] == 1 and len(harness.prepared) == 1


def test_midnight_boundary_invalidates_without_database_writes(harness, monkeypatch):
    from backend.future_radar import repository as repository_module

    harness.insert("top-one")
    day = ["2026-09-01"]
    monkeypatch.setattr(repository_module, "date_boundary", lambda: (day[0], day[0]))
    harness.pool()
    before = revision(harness)
    day[0] = "2026-09-02"
    harness.pool()
    assert revision(harness) == before and len(harness.prepared) == 2


def test_write_while_scoring_cannot_publish_stale_cache_version(harness):
    saved = harness.insert("top-one")
    original = harness.prepare
    changed = False

    def close_during_prepare(row):
        nonlocal changed
        if not changed:
            changed = True
            with harness.repository.transaction() as connection:
                connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (saved["id"],))
        return original(row)

    result = harness.repository.list_opportunities(
        public_url=public_url, prepare=close_during_prepare, cache_scope="write-race-scope",
    )
    assert result["total"] == 0
    assert harness.repository._opportunity_cache.info()["entries"] == 1
    assert harness.pool(scope="write-race-scope")["total"] == 0


def test_missing_revision_disables_cache_instead_of_sharing_unknown_database(harness):
    harness.insert("top-one")
    harness.pool()
    with harness.repository.transaction() as connection:
        connection.execute("DELETE FROM system_state WHERE key=?", (NAMESPACE_KEY,))
    harness.pool()
    harness.pool()
    assert len(harness.prepared) == 3


def test_postgres_truncate_invalidates_existing_worker_cache(harness):
    if harness.backend != "postgres":
        pytest.skip("SQLite has no TRUNCATE")
    harness.insert("top-one")
    harness.pool()
    before = revision(harness)
    with harness.repository.transaction() as connection:
        connection.execute("TRUNCATE radar_jobs CASCADE")
    assert revision(harness) != before
    assert harness.pool()["total"] == 0


def test_two_sqlite_files_cannot_share_cache_even_with_identical_epoch_revision(tmp_path):
    identities = []
    for name in ("one.db", "two.db"):
        connection = sqlite3.connect(tmp_path / name)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT)")
            connection.executemany("INSERT INTO system_state VALUES (?, ?)", [(NAMESPACE_KEY, "same-epoch"), (REVISION_KEY, "7")])
            identities.append(read_opportunity_revision(connection))
        finally:
            connection.close()
    assert identities[0][1:] == identities[1][1:]
    assert identities[0] != identities[1]


def test_single_flight_same_key_but_unrelated_queries_do_not_wait():
    cache = BoundedScoringCache()
    started = threading.Event()
    release = threading.Event()
    follower_started = threading.Event()
    calls = []

    def slow():
        calls.append("build")
        started.set()
        assert release.wait(3)
        return {"items": [1]}

    def follower():
        follower_started.set()
        return cache.get_or_compute("same", slow)

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(cache.get_or_compute, "same", slow)
        assert started.wait(2)
        second = executor.submit(follower)
        assert follower_started.wait(2)
        other = executor.submit(cache.get_or_compute, "other-user", lambda: 23)
        assert other.result(timeout=2) == 23
        release.set()
        assert first.result(timeout=3) == second.result(timeout=3) == {"items": [1]}
    assert calls == ["build"]


def test_cache_entry_memory_inflight_ttl_and_failures_are_bounded():
    clock = [0.0]
    cache = BoundedScoringCache(max_entries=2, max_bytes=800, ttl_seconds=10, max_inflight=0, clock=lambda: clock[0])
    assert cache.get_or_compute("uncached", lambda: 7) == 7
    assert cache.info() == {"entries": 0, "bytes": 0, "inflight": 0}
    cache.max_inflight = 2
    for index in range(3):
        assert cache.get_or_compute(index, lambda: index) == index
    assert cache.info()["entries"] == 2 and cache.info()["bytes"] <= 800
    cache.get_or_compute("huge", lambda: "x" * 10000)
    assert cache.info()["entries"] == 2 and cache.info()["bytes"] <= 800
    clock[0] = 11
    assert cache.info() == {"entries": 0, "bytes": 0, "inflight": 0}

    def fail():
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        cache.get_or_compute("retry", fail)
    assert cache.info()["inflight"] == 0
    assert cache.get_or_compute("retry", lambda: "recovered") == "recovered"


def test_idle_cache_hits_extend_retention_but_still_expire_after_inactivity():
    clock = [0.0]
    cache = BoundedScoringCache(ttl_seconds=10, refresh_on_hit=True, clock=lambda: clock[0])
    assert cache.get_or_compute("revision-1", lambda: {"id": "first"}) == {"id": "first"}
    retained_bytes = cache.info()["bytes"]
    clock[0] = 9
    assert cache.get_or_compute("revision-1", lambda: {"id": "incorrect"}) == {"id": "first"}
    clock[0] = 18
    assert cache.find(lambda key, value: value if key == "revision-1" else None) == {"id": "first"}
    assert cache.info()["entries"] == 1 and cache.info()["bytes"] == retained_bytes
    clock[0] = 27
    assert cache.info()["entries"] == 1
    clock[0] = 29
    assert cache.info() == {"entries": 0, "bytes": 0, "inflight": 0}


def test_validated_tier_pool_does_not_rebuild_after_five_minutes_but_closure_does(harness):
    clock = [0.0]
    harness.repository._opportunity_cache._clock = lambda: clock[0]
    saved = harness.insert("top-idle-tier")
    assert harness.pool()["total"] == 1
    clock[0] = 301
    assert harness.pool(filters={"tier_code": "T0"})["total"] == 1
    assert harness.prepared == ["top-idle-tier"]
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE radar_jobs SET status='closed' WHERE id=?", (saved["id"],))
    assert harness.pool(filters={"tier_code": "T0"})["total"] == 0



def test_compact_http_payload_keeps_stats_and_default_legacy_aliases(harness, monkeypatch):
    # Call only the route function with a synthetic identity: no account is
    # created, no auth endpoint called, no test browser or network used.
    from backend import main
    from fastapi.params import Param
    from inspect import signature

    harness.insert("top-api")
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=harness.repository))
    monkeypatch.setattr(main.database, "get_recruitment_profile", lambda _user: {"updated_at": "v1"})
    monkeypatch.setattr(main, "_public_reference_url", public_url)
    monkeypatch.setattr(main, "_public_radar_opportunity", lambda row, profile: harness.prepare(row))
    defaults = {name: (parameter.default.default if isinstance(parameter.default, Param) else parameter.default)
                for name, parameter in signature(main.future_radar_opportunities).parameters.items() if name != "user"}
    response = main.future_radar_opportunities(user={"id": 99001}, **defaults)
    full = json.loads(response.body)
    compact = json.loads(main.future_radar_opportunities(
        user={"id": 99001}, **{**defaults, "compact": True, "tier_code": "T0"},
    ).body)
    assert full["jobs"] == full["opportunities"] == full["items"]
    assert compact["items"] == full["items"]
    assert compact["stats"] == full["stats"] and compact["total"] == full["total"] == 1
    assert "jobs" not in compact and "opportunities" not in compact
    assert len(harness.prepared) == 1


def test_actual_public_scorer_is_reused_without_another_whole_pool_query(harness, monkeypatch):
    from backend import main

    for index in range(70):
        harness.insert(f"benchmark-{index:02}", company="腾讯",
                       title=f"2027 校园招聘金融科技数据分析岗 {index}")
    profile = {"desired_roles": ["数据分析"], "cities": ["上海"], "updated_at": "fixture-v1"}
    calls = []

    def prepare(row):
        calls.append(row["id"])
        return main._public_radar_opportunity(row, profile)

    scope = main._radar_scoring_scope(71, profile)
    started = time.perf_counter()
    first = harness.repository.list_opportunities(
        public_url=public_url, prepare=prepare, cache_scope=scope, page_size=10,
    )
    cold = time.perf_counter() - started
    assert first["total"] == 70
    assert len(calls) == 70

    def must_not_read_whole_pool(**kwargs):
        raise AssertionError("Warm tier/page query repeated full DB read/dedupe/score")

    monkeypatch.setattr(harness.repository, "_opportunity_rows", must_not_read_whole_pool)
    started = time.perf_counter()
    bucket = first["items"][0]["tier_bucket"]
    warm = harness.repository.list_opportunities(
        public_url=public_url, prepare=prepare, cache_scope=scope,
        page=2, page_size=10, filters={"tier_code": bucket},
    )
    elapsed = time.perf_counter() - started
    assert len(calls) == 70
    assert warm["total"] == first["stats"]["tier_counts"][bucket]
    assert warm["stats"]["matching_total"] == 70
    assert len(warm["items"]) == 10
    print(f"isolated-{harness.backend}: 70 actual scores; cold={cold:.4f}s, warm={elapsed:.4f}s, full reads on warm=0")


def test_auxiliary_coverage_nulls_and_health_updates_do_not_hide_pool(harness):
    harness.insert("top-one")
    harness.pool()
    before = revision(harness)
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE monitor_sources SET status='running' WHERE id='discovery'")
    summary = harness.repository.discovery_summary("discovery")
    assert summary == {"status": "running", "fetched_at": None, "metadata": {}}
    assert revision(harness) == before
    assert harness.pool()["total"] == 1 and len(harness.prepared) == 1
