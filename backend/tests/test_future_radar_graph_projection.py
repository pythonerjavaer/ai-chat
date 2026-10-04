"""Graph reads use public, bounded rows without profile-dependent scoring."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from backend.tests.test_opportunity_scoring_cache import (  # noqa: F401
    cache_database, harness, public_url,
)


GRAPH_FIELDS = {
    "id", "company", "title", "city", "industry", "tags", "official_url",
    "application_url", "description", "responsibilities", "requirements",
}


def graph_rows(harness, **kwargs):
    from backend import main

    return harness.repository.list_graph_opportunities(
        public_url=public_url,
        input_sanitizer=lambda row: main._public_search_update(row, include_detail=True),
        **kwargs,
    )


def test_graph_bounds_sanitization_and_excludes_private_fields_without_scoring(harness, monkeypatch):
    from backend import main

    def forbidden(*_args, **_kwargs):
        pytest.fail("Graph projection must not score, prepare or group the full pool")

    for name in ("_prepare_opportunity_pool", "_balanced_opportunities", "_opportunity_company_groups"):
        monkeypatch.setattr(harness.repository, name, forbidden)
    monkeypatch.setattr(main, "score_job", forbidden)
    monkeypatch.setattr(main, "_public_radar_opportunity", forbidden)
    raw_rows = [{
        "id": f"job-{index:03}", "company": "示例科技", "title": "校园招聘数据分析岗",
        "city": "上海", "industry": "科技", "tags": ["Python"],
        "description": "使用 Python 分析数据", "requirements": "掌握 SQL",
        "official_url": "https://careers.example.invalid/campus/role",
        "_member_ids": {f"job-{index:03}"}, "_member_external_ids": {f"external-{index:03}"},
        "application_status": "PRIVATE_APPLICATION_STATE", "profile": "PRIVATE_PROFILE",
        "source_ratings": [{"reason": "PRIVATE_RATING"}], "evidence": "PRIVATE_EVIDENCE",
        "sources": [{"adapter_config": "PRIVATE_CONFIG"}],
    } for index in range(205)]
    monkeypatch.setattr(harness.repository, "_opportunity_rows", lambda **_kwargs: raw_rows)
    sanitized = []

    def sanitize(row):
        assert set(row) == GRAPH_FIELDS
        sanitized.append(row["id"])
        return main._public_search_update(row, include_detail=True) | {"notes": "PRIVATE_NOTES"}

    result = harness.repository.list_graph_opportunities(
        public_url=public_url, input_sanitizer=sanitize,
        application_states={"job-000": "skipped"},
    )
    assert len(result) == len(sanitized) == 200
    assert sanitized == [f"job-{index:03}" for index in range(1, 201)]
    assert all(set(row) == GRAPH_FIELDS for row in result)
    assert "PRIVATE_" not in json.dumps(result)
    assert result[0]["requirements"] == "掌握 SQL"


def test_graph_preserves_active_expiry_source_campus_and_actionable_rules(harness):
    open_job = harness.insert("open")
    unknown_job = harness.insert("unknown", status="unknown", closing_date=None)
    harness.insert("closed", status="closed")
    harness.insert("expired", closing_date=(date.today() - timedelta(days=1)).isoformat())
    harness.insert("closing-today", closing_date=date.today().isoformat())
    harness.insert("social", title="社会招聘资深数据分析岗")
    harness.insert("non-campus", title="数据分析员", tags=[], requirements="具有三年经验",
                   description="负责业务分析和研究")
    harness.insert("non-actionable", city="招聘公告")
    harness.insert("rejected", verification_status="rejected")
    harness.insert("unsafe-url", official_url="http://127.0.0.1/jobs")
    retired = harness.insert("retired")
    with harness.repository.transaction() as connection:
        connection.execute("UPDATE job_sources SET active=0 WHERE job_id=?", (retired["id"],))

    expected = {open_job["id"], unknown_job["id"]}
    ordinary = harness.pool(filters={"status": "active", "sort": "company", "active_only": True})
    assert {row["id"] for row in ordinary["items"]} == expected
    assert {row["id"] for row in graph_rows(harness)} == expected


@pytest.mark.parametrize("alias_field", ["id", "external_id"])
def test_graph_deduplicates_and_resolves_latest_personal_choice_across_aliases(harness, alias_field):
    discovery = harness.insert("discovery", title="2027 校园招聘数据分析岗", city="上海市")
    official = harness.insert("official", source="official", title="2027 校园招聘数据分析岗",
                              city="上海", verification_status="verified")
    assert [row["id"] for row in graph_rows(harness)] == [official["id"]]

    skipped = {official["id"]: "planned", discovery[alias_field]: "skipped"}
    assert graph_rows(harness, application_states=skipped) == []
    # A later applied choice for the verified winner replaces the older skip.
    applied = {discovery[alias_field]: "skipped", official["id"]: "applied"}
    result = graph_rows(harness, application_states=applied)
    assert [row["id"] for row in result] == [official["id"]]
    assert "application_status" not in result[0]
    # Personal state is per request; another user's public graph remains visible.
    assert [row["id"] for row in graph_rows(harness)] == [official["id"]]


def test_graph_does_not_resurrect_a_discovery_with_an_authoritative_closed_copy(harness):
    harness.insert("lead", title="2027 校园招聘数据分析岗")
    harness.insert("closed-official", source="official", title="2027 校园招聘数据分析岗",
                   status="closed", verification_status="verified")
    assert graph_rows(harness) == []


def test_graph_omits_event_history_queries_but_ordinary_reads_retain_them(harness, monkeypatch):
    harness.insert("one")
    statements = []
    connect = harness.repository._connect

    class TracedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, statement, *args):
            statements.append(statement)
            return self.connection.execute(statement, *args)

    monkeypatch.setattr(harness.repository, "_connect", lambda: TracedConnection(connect()))
    assert len(graph_rows(harness)) == 1
    assert len(statements) == 3, "Public provenance must be loaded in a batch"
    assert all("radar_events" not in statement for statement in statements)
    statements.clear()
    ordinary = harness.repository._opportunity_rows(
        filters={"status": "active", "sort": "company", "active_only": True},
        public_url=public_url, company_aliases={},
    )
    assert "latest_event_type" in ordinary[0]
    assert "latest_event_at" in ordinary[0]
    assert any("radar_events" in statement for statement in statements)


def test_latest_job_event_batch_join_matches_highest_id_reference(harness):
    first = harness.insert("first")
    second = harness.insert("second")
    no_history = harness.insert("no-history")
    events = [
        (first, "job", "UPDATED", "2031-01-01T10:00:00+00:00"),
        (second, "job", "NEW", "2031-01-01T11:00:00+00:00"),
        (first, "job", "UPDATED", "2032-01-01T10:00:00+00:00"),
        # A later insertion can have an older timestamp. Preserve ID ordering.
        (first, "job", "DEADLINE_CHANGED", "2020-01-01T10:00:00+00:00"),
        (second, "job", "UPDATED", "2021-01-01T10:00:00+00:00"),
        # A larger ID belonging to another entity type must not win.
        (first, "program", "PROGRAM_UPDATED", "2040-01-01T10:00:00+00:00"),
    ]
    with harness.repository.transaction() as connection:
        for index, (job, entity_type, event_type, stamp) in enumerate(events):
            harness.repository.insert_event(
                connection, run_id=f"isolated-event-{index}", entity_type=entity_type,
                entity_id=job["id"], external_id=job["external_id"], event_type=event_type,
                before=None, after=None, fields=[], source_id="discovery", now=stamp,
            )
        # This is the old, independent lookup on a tiny test fixture. Compare
        # both values, partition isolation, missing history and row cardinality.
        reference = connection.execute("""
            SELECT j.id,
                (SELECT e.event_type FROM radar_events e
                 WHERE e.entity_type='job' AND e.entity_id=j.id ORDER BY e.id DESC LIMIT 1) AS event_type,
                (SELECT e.detected_at FROM radar_events e
                 WHERE e.entity_type='job' AND e.entity_id=j.id ORDER BY e.id DESC LIMIT 1) AS detected_at
            FROM radar_jobs j
        """).fetchall()
    expected = {row["id"]: (row["event_type"], row["detected_at"]) for row in reference}
    ordinary = harness.repository._opportunity_rows(
        filters={"status": "active", "sort": "company", "active_only": True},
        public_url=public_url, company_aliases={},
    )
    assert len(ordinary) == len(expected) == 3
    assert {row["id"]: (row["latest_event_type"], row["latest_event_at"])
            for row in ordinary} == expected
    assert expected[first["id"]] == ("DEADLINE_CHANGED", "2020-01-01T10:00:00+00:00")
    assert expected[second["id"]] == ("UPDATED", "2021-01-01T10:00:00+00:00")
    assert expected[no_history["id"]] == (None, None)
    assert {row["id"] for row in graph_rows(harness)} == set(expected)


def test_graph_endpoint_skips_profile_scoring_and_passes_only_public_rows(harness, monkeypatch):
    from backend import database, main
    from backend.future_radar import personal

    saved = harness.insert("visible", requirements="面向2027届毕业生，掌握 Python 和 SQL")
    captured = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("Graph endpoint must not load a profile or score opportunities")

    monkeypatch.setattr(database, "get_recruitment_profile", forbidden)
    monkeypatch.setattr(main, "score_job", forbidden)
    monkeypatch.setattr(main, "_public_radar_opportunity", forbidden)
    monkeypatch.setattr(main, "settings", SimpleNamespace(neo4j_uri="neo4j+s://example.invalid"))
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=harness.repository))
    monkeypatch.setattr(main, "_public_reference_url", public_url)
    monkeypatch.setattr(main, "_radar_company_aliases", lambda: {})

    def states(_connect, user_id):
        assert user_id == 17
        return {saved["id"]: "applied"}

    monkeypatch.setattr(personal, "application_states", states)

    def sync(rows):
        captured.extend(rows)
        return {"status": "synced", "opportunities": len(rows), "items": [{
            "employer": rows[0]["company"], "id": rows[0]["id"],
            "title": rows[0]["title"], "skills": ["Python", "SQL"],
        }]}

    monkeypatch.setattr(main, "neo4j_opportunity_graph", SimpleNamespace(sync_opportunities=sync))
    result = main.future_radar_relationship_graph({"id": 17})
    assert len(captured) == result["postgres_opportunities_considered"] == 1
    assert set(captured[0]) == GRAPH_FIELDS
    assert "PRIVATE_" not in json.dumps(captured)
    assert {node["kind"] for node in result["nodes"]} == {"employer", "opportunity", "skill"}
    assert {edge["kind"] for edge in result["relationships"]} == {"POSTS", "REQUIRES"}


def test_graph_endpoint_exposes_more_than_twelve_evidenced_skills_without_private_or_negated_labels(harness, monkeypatch):
    from backend import database, main
    from backend.future_radar import personal
    from backend.portfolio_datastores import Neo4jOpportunityGraph
    from backend.tests.test_neo4j_graph_adapter import RecordingDriver

    requirements = ("面向2027届毕业生，掌握Python、NumPy、pandas、SciPy、scikit-learn、XGBoost、"
                    "LightGBM、Keras、OpenCV、NLP、计算机视觉、LLM、风险管理、财务分析、企业估值、"
                    "用户研究和需求分析；不要求SQL Server、React Native和信用分析。")
    saved = harness.insert("expanded-skills", title="2027 校园招聘工程师", requirements=requirements,
                           description="公开岗位说明", responsibilities="公开岗位职责",
                           company="AWS 项目管理公司", tags=["校园招聘", "SQL", "AWS", "项目管理"])
    expected = Neo4jOpportunityGraph._skills({"requirements": requirements})
    assert len(expected) > 12
    driver = RecordingDriver(records=[{"id": saved["id"], "skills": [*reversed(expected), "PRIVATE_SKILL"]}],
                             relationship_count=73)
    graph = Neo4jOpportunityGraph("neo4j://unused.test")
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Skill enrichment must not load profiles or invoke scoring")

    monkeypatch.setattr(database, "get_recruitment_profile", forbidden)
    monkeypatch.setattr(main, "score_job", forbidden)
    monkeypatch.setattr(main, "_public_radar_opportunity", forbidden)
    monkeypatch.setattr(main, "settings", SimpleNamespace(neo4j_uri="neo4j+s://example.invalid"))
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=harness.repository))
    monkeypatch.setattr(main, "_public_reference_url", public_url)
    monkeypatch.setattr(main, "_radar_company_aliases", lambda: {})
    monkeypatch.setattr(personal, "application_states", lambda _connect, _user_id: {})
    monkeypatch.setattr(main, "neo4j_opportunity_graph", graph)

    result = main.future_radar_relationship_graph({"id": 19})

    assert result["items"][0]["skills"] == expected
    assert {node["label"] for node in result["nodes"] if node["kind"] == "skill"} == set(expected)
    requires = [edge for edge in result["relationships"] if edge["kind"] == "REQUIRES"]
    assert len(requires) == len(expected)
    assert {edge["target"] for edge in requires} == {f"skill:{skill.casefold()}" for skill in expected}
    assert not {"SQL", "SQL Server", "React", "React Native", "信用分析", "AWS", "项目管理"} & set(expected)
    assert result["relationships_stored"] == result["relationships_written"] == 73
    assert result["postgres_opportunities_considered"] == 1
    assert "PRIVATE_" not in json.dumps(result)
    write = next(parameters for query, parameters in driver.calls if "UNWIND $rows AS row" in query)
    assert write["rows"][0]["skills"] == expected
    assert "PRIVATE_" not in repr(driver.calls)
