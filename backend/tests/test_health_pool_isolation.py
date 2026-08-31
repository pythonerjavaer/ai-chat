"""Offline application checks and explicitly isolated loopback PostgreSQL only.

No app lifespan, source fetch, user/account creation, login, or production DSN.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import uuid
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "not-used-health-test")
os.environ.setdefault("JWT_SECRET", "isolated-health-test-secret-at-least-32-bytes")

from backend import database, main, storage
from backend.future_radar.service import RadarRunBusy


@pytest.fixture
def isolated_health_database(monkeypatch, tmp_path):
    dsn = os.environ.get("FROSTFIRE_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("FROSTFIRE_TEST_POSTGRES_URL is not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"localhost", "127.0.0.1", "::1"}
    assert info.get("hostaddr", info["host"]) in {"localhost", "127.0.0.1", "::1"}
    assert info.get("dbname") == info.get("user") == "frostfire_test" and not info.get("password")
    config = SimpleNamespace(
        database_backend="postgres", database_url=dsn,
        database_schema="ff_health_test_" + uuid.uuid4().hex, database_pool_size=4,
        database_path=tmp_path / "must-not-create.db",
    )
    monkeypatch.setattr(database, "settings", config)
    monkeypatch.setattr(main, "settings", config)
    # SELECT 1 needs no new application schema/tables. Both pool purposes use
    # this same unique search_path, but tests never import real application rows.
    try:
        yield SimpleNamespace(config=config, psycopg=psycopg)
    finally:
        storage.close_postgres_pools()


def probe_app():
    app = FastAPI()
    app.get("/api/health")(main.health)
    return app


def test_full_application_pool_does_not_make_real_health_fail(isolated_health_database):
    with ExitStack() as held:
        writers = [held.enter_context(database.connect()) for _ in range(4)]
        for writer in writers:
            assert writer.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="pool is busy"):
            database.connect(timeout=0.03)
        with TestClient(probe_app()) as client:
            for _ in range(3):
                response = client.get("/api/health")
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
        with database.connect_health() as reserved:
            assert reserved._pool is not writers[0]._pool
            assert reserved._pool.max_size == 1
            with pytest.raises(sqlite3.OperationalError, match="pool is busy"):
                database.connect_health(timeout=0.03)


def test_real_database_failure_still_503_not_cached_and_recovers(isolated_health_database, caplog):
    environment = isolated_health_database
    with database.connect_health() as connection:
        pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
    # Stop only the exact idle test probe connection created immediately above,
    # not the server, another session, a user account, or any database data.
    with environment.psycopg.connect(environment.config.database_url, autocommit=True) as control:
        assert control.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
    with TestClient(probe_app()) as client:
        failed = client.get("/api/health")
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Database is unavailable."}
        assert client.get("/api/health").status_code == 200
    assert "purpose=health" in caplog.text
    assert environment.config.database_url not in caplog.text
    assert environment.config.database_schema not in caplog.text
    assert "SELECT" not in caplog.text and "password" not in caplog.text.casefold()


@pytest.mark.parametrize("purpose", ["user-input", "health-2", "", None, {}, 1])
def test_arbitrary_pool_names_cannot_create_unbounded_pools(purpose):
    with pytest.raises(sqlite3.ProgrammingError, match="pool purpose"):
        storage.connect_postgres("postgresql://not-contacted.invalid/db", purpose=purpose)


def test_slow_scan_preflight_does_not_block_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    event_thread = threading.get_ident()
    seen = []

    def get_source(source_id):
        seen.append(threading.get_ident())
        entered.set()
        assert release.wait(1), "event loop was blocked by synchronous registry I/O"
        return {"id": source_id}

    def select(*args, **kwargs):
        seen.append(threading.get_ident())
        return [{"id": "local-source"}]

    def run(**kwargs):
        seen.append(threading.get_ident())
        assert kwargs["source_ids"] == ["local-source"]
        return {"status": "success"}

    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(
        repository=SimpleNamespace(get_source=get_source, manual_scan_sources=select), run=run,
    ))
    monkeypatch.setattr(main, "_public_radar_run", lambda result: result)

    async def scenario():
        task = asyncio.create_task(main.run_future_radar(None, main.RadarRunRequest(source_ids=["local-source"]), None))
        try:
            assert await asyncio.to_thread(entered.wait, 0.5)
            assert not task.done()
            release.set()
            assert (await asyncio.wait_for(task, 2))["status"] == "success"
        finally:
            release.set()

    asyncio.run(scenario())
    assert len(seen) == 3 and all(thread != event_thread for thread in seen)


@pytest.mark.parametrize("exists,selected,requested,expected", [
    (False, [], ["missing"], 404),
    (True, [], ["unavailable"], 422),
    (True, [], [], 503),
])
def test_preflight_http_statuses_are_preserved(monkeypatch, exists, selected, requested, expected):
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=SimpleNamespace(
        get_source=lambda value: {"id": value} if exists else None,
        manual_scan_sources=lambda *args, **kwargs: selected,
    )))
    with pytest.raises(HTTPException) as result:
        asyncio.run(main.run_future_radar(None, main.RadarRunRequest(source_ids=requested), None))
    assert result.value.status_code == expected


def test_force_auth_and_active_run_conflict_remain_authoritative(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(admin_dashboard_token="local-test-admin"))
    monkeypatch.setattr(main, "_future_radar_run_sources", lambda payload: ["local-source"])

    def busy(**kwargs):
        raise RadarRunBusy("already running", scan_type="quick")

    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(run=busy))
    with pytest.raises(HTTPException) as unauthorized:
        asyncio.run(main.run_future_radar(None, main.RadarRunRequest(force=True), "not-authorized"))
    assert unauthorized.value.status_code == 401
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(main.run_future_radar(None, main.RadarRunRequest(), None))
    assert conflict.value.status_code == 409 and conflict.value.headers == {"Retry-After": "3"}
