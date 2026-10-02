"""Notifications batch current rows without changing cursor/personal semantics.

SQLite and the explicitly configured credential-free loopback PostgreSQL test
cluster only; these fixtures never use application database credentials.
"""

import json
from types import SimpleNamespace

import pytest

from backend.tests.test_opportunity_scoring_cache import (  # noqa: F401
    cache_database, harness,
)


def trace_repository_reads(repository, monkeypatch):
    statements = []
    connections = []
    connect = repository._connect

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

    def traced_connect():
        connections.append(True)
        return TracedConnection(connect())

    monkeypatch.setattr(repository, "_connect", traced_connect)
    return statements, connections


@pytest.mark.parametrize("job_count", [1, 55])
def test_notification_jobs_use_two_selects_and_preserve_public_projection(
    harness, monkeypatch, job_count,
):
    from backend import main

    with harness.repository.transaction() as connection:
        program = harness.repository.insert_program(
            connection, {"external_id": "notice-program", "company": "示例科技",
                         "program_name": "2027 校园招聘项目", "recruitment_year": 2027,
                         "content_hash": "notice-program"},
            source_id="discovery", now="2030-01-01T00:00:00+00:00",
        )
    source_rating = {
        "scope": "job", "tier_code": "T1.5", "score": 77.0,
        "reason": "来源明确给出的岗位评级", "source_id": "discovery",
        "source_updated_at": "2030-01-01T00:00:00+00:00",
        "observed_at": "2030-01-02T00:00:00+00:00",
        "rating_key": "candidate-" + "a" * 32,
    }
    jobs = [harness.insert(f"notice-{index:02}", status="closed" if index == 0 else "open",
                           program_id=program["id"] if index == 0 else None,
                           source_ratings=[source_rating] if index == 0 else [])
            for index in range(job_count)]
    # Add another source with a different ordering key. Public source clocks,
    # roles and active flags remain identical without loading raw evidence.
    official = harness.repository.get_source("official")
    with harness.repository.transaction() as connection:
        harness.repository.link_job_source(
            connection, job_id=jobs[0]["id"], source=official,
            source_url=jobs[0]["official_url"], verification_role="verification",
            now="2030-01-02T00:00:00+00:00", evidence=["PRIVATE_SOURCE_EVIDENCE"],
        )
        connection.execute(
            "UPDATE job_sources SET active=0 WHERE job_id=? AND source_id=?",
            (jobs[0]["id"], "official"),
        )
        harness.repository.insert_event(
            connection, run_id="notification-history", entity_type="job",
            entity_id=jobs[0]["id"], external_id=jobs[0]["external_id"],
            event_type="NEW", before=None, after={"private": "PRIVATE_EVENT_DATA"},
            fields=[], source_id="discovery", now="2030-01-02T00:00:00+00:00",
        )
    profile = {"desired_roles": ["数据分析"], "industries": ["科技"]}
    expected = {
        job["id"]: main._public_radar_opportunity(harness.repository.get_job(job["id"]), profile)
        for job in jobs
    }
    first = expected[jobs[0]["id"]]
    assert first["program_name"] == "2027 校园招聘项目"
    assert first["recruitment_year"] == 2027
    assert first["rating_status"] == "applied"
    assert first["tier_code"] == "T1.5" and first["job_score"] == 77.0
    assert first["source_ratings"] == [{
        key: value for key, value in source_rating.items()
        if key not in {"rating_key", "observed_at"}
    }]
    assert any(source["source_id"] == "official" and source["active"] is False
               for source in first["sources"])
    statements, connections = trace_repository_reads(harness.repository, monkeypatch)
    result = harness.repository.get_notification_jobs(
        [job["id"] for job in jobs] + [jobs[0]["id"], "missing-job"],
    )

    assert len(connections) == 1
    assert len(statements) == 2
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert all("radar_events" not in statement and "evidence" not in statement for statement in statements)
    assert set(result) == set(expected)
    assert {
        job_id: main._public_radar_opportunity(row, profile)
        for job_id, row in result.items()
    } == expected
    assert all("events" not in row for row in result.values())
    assert all("evidence" not in source for row in result.values() for source in row["sources"])
    assert "PRIVATE_" not in json.dumps(result)


def test_notification_jobs_empty_input_does_not_connect(harness, monkeypatch):
    def forbidden():
        pytest.fail("An empty event batch must not open a database connection")

    monkeypatch.setattr(harness.repository, "_connect", forbidden)
    assert harness.repository.get_notification_jobs([]) == {}


def old_notification_items(repository, events, application_states, profile, presenter):
    """The pre-batch route, retained as an independent response oracle."""
    items = []
    seen = set()
    for event in events:
        if event["entity_id"] in seen:
            continue
        current = repository.get_job(event["entity_id"])
        if current is None:
            continue
        job = presenter(current, profile)
        job["application_status"] = application_states.get(job["id"], "not_applied")
        seen.add(event["entity_id"])
        if job["application_status"] != "skipped":
            items.append({"event_id": event["id"], "job": job})
    return items


def patch_notification_route(harness, monkeypatch, events, states, profile, through):
    from backend import main
    from backend.future_radar import personal

    acknowledgements = []
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=harness.repository))
    monkeypatch.setattr(personal, "pending_events", lambda _connect, _user_id: (events, through))
    monkeypatch.setattr(personal, "application_states", lambda _connect, _user_id: states)
    monkeypatch.setattr(main.database, "get_recruitment_profile", lambda _user_id: profile)
    monkeypatch.setattr(personal, "acknowledge", lambda _connect, user_id, cursor:
                        acknowledgements.append((user_id, cursor)))
    return main, acknowledgements


def test_notification_route_55_jobs_matches_old_order_first_event_and_personal_state(
    harness, monkeypatch,
):
    from backend import main

    jobs = [harness.insert(f"route-{index:02}", status="closed" if index == 0 else "open")
            for index in range(55)]
    events = [{"id": 100 + index, "entity_id": job["id"]}
              for index, job in enumerate(reversed(jobs))]
    events.extend([
        {"id": 155, "entity_id": jobs[-1]["id"]},  # Keep the first event, not this duplicate.
        {"id": 156, "entity_id": "missing-job"},
        {"id": 157, "entity_id": "missing-job"},
    ])
    states = {
        jobs[0]["id"]: "planned", jobs[2]["id"]: "applied", jobs[5]["id"]: "skipped",
        # The notification route has always read personal state by current ID,
        # not by external aliases or opportunity-pool deduplication identities.
        jobs[3]["external_id"]: "skipped",
    }
    profile = {"desired_roles": ["数据分析"]}
    expected = old_notification_items(
        harness.repository, events, states, profile, main._public_radar_opportunity,
    )
    main, acknowledgements = patch_notification_route(
        harness, monkeypatch, events, states, profile, through=200,
    )
    statements, connections = trace_repository_reads(harness.repository, monkeypatch)
    monkeypatch.setattr(harness.repository, "get_job", lambda *_args:
                        pytest.fail("Notifications must not read each job separately"))
    monkeypatch.setattr(harness.repository, "_opportunity_rows", lambda **_kwargs:
                        pytest.fail("Notifications must not rebuild or filter the opportunity pool"))

    result = main.radar_notifications({"id": 7})

    assert result == {"items": expected, "through_event_id": 200}
    assert len(result["items"]) == 54
    assert result["items"][0]["event_id"] == 100
    assert result["items"][-1]["job"]["status"] == "closed"
    assert len(statements) == 2 and len(connections) == 1
    assert acknowledgements == []


@pytest.mark.parametrize("case", ["empty", "missing", "skipped"])
def test_notification_route_acknowledges_only_empty_visible_results(harness, monkeypatch, case):
    events = []
    states = {}
    if case == "missing":
        events = [{"id": 10, "entity_id": "missing-job"}]
    elif case == "skipped":
        job = harness.insert("only-skipped")
        events = [{"id": 10, "entity_id": job["id"]}]
        states = {job["id"]: "skipped"}
    main, acknowledgements = patch_notification_route(
        harness, monkeypatch, events, states, {}, through=23,
    )

    assert main.radar_notifications({"id": 7}) == {"items": [], "through_event_id": 23}
    assert acknowledgements == [(7, 23)]
