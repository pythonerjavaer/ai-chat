"""Telemetry regressions without an HTTP client, accounts or external access."""

import asyncio
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.requests import Request
from starlette.responses import Response


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from backend import database, main


@pytest.fixture
def metrics_database(tmp_path, monkeypatch):
    path = tmp_path / "only-telemetry.sqlite3"
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=path))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE api_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                method TEXT NOT NULL, route TEXT NOT NULL,
                status_code INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
    yield path
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def metric_rows(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM api_usage_events")]


def request_for(path="/api/future-radar/opportunities/public-example"):
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "server": ("127.0.0.1", 1),
        "client": None, "path": path, "root_path": "",
        "query_string": b"private_query=DO_NOT_STORE_QUERY",
        "headers": [(b"x-private-context", b"DO_NOT_STORE_HEADER")],
        "route": SimpleNamespace(path="/api/future-radar/opportunities/{job_id}"),
    })


async def deliver(response, request, send=None):
    async def receive():
        return {"type": "http.request", "body": b"DO_NOT_STORE_BODY"}

    async def discard(_message):
        pass

    await response(request.scope, receive, send or discard)


def test_telemetry_is_deferred_and_freezes_safe_business_metadata(metrics_database, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(main, "time", SimpleNamespace(perf_counter=lambda: now[0]))

    async def scenario():
        request = request_for()

        async def business_response(_request):
            now[0] += 0.025
            return Response("business-body")

        response = await main.security_headers(request, business_response)
        assert metric_rows(metrics_database) == []
        assert response.headers["Cache-Control"] == "no-store"
        assert response.background.tasks[-1].args == (
            None, "GET", "/api/future-radar/opportunities/{job_id}", 200, 25,
        )
        # The deferred worker must not recalculate timing after other work.
        now[0] += 10
        await deliver(response, request)

    asyncio.run(scenario())
    rows = metric_rows(metrics_database)
    assert len(rows) == 1
    assert rows[0]["duration_ms"] == 25
    assert rows[0]["user_id"] is None
    assert rows[0]["route"] == "/api/future-radar/opportunities/{job_id}"
    assert "DO_NOT_STORE" not in str(rows)
    assert "business-body" not in str(rows)


@pytest.mark.parametrize("container", ["single", "multiple"])
def test_existing_background_work_is_preserved(metrics_database, container):
    completed = []

    async def scenario():
        request = request_for()

        async def first_task():
            completed.append("first")

        def second_task():
            completed.append("second")

        original = BackgroundTask(first_task)
        if container == "multiple":
            original = BackgroundTasks()
            original.add_task(first_task)
            original.add_task(second_task)

        async def business_response(_request):
            return Response("ok", background=original)

        response = await main.security_headers(request, business_response)
        assert completed == []
        await deliver(response, request)

    asyncio.run(scenario())
    assert completed == (["first", "second"] if container == "multiple" else ["first"])
    assert len(metric_rows(metrics_database)) == 1


def test_real_sqlite_writer_does_not_block_body_or_event_loop(metrics_database, monkeypatch):
    assert 0 < database.API_USAGE_SQLITE_TIMEOUT_SECONDS <= 0.1
    writer = sqlite3.connect(metrics_database)
    writer.execute("BEGIN IMMEDIATE")
    original_recorder = database.record_api_usage_event
    recorder_started = threading.Event()
    recorder_finished = threading.Event()
    observed = {"ticks_while_writing": 0}
    order = []

    def observe_recorder(*args):
        observed["recorder_thread"] = threading.get_ident()
        recorder_started.set()
        order.append("telemetry")
        try:
            original_recorder(*args)
        finally:
            recorder_finished.set()

    monkeypatch.setattr(database, "record_api_usage_event", observe_recorder)

    async def scenario():
        observed["event_loop_thread"] = threading.get_ident()
        request = request_for()
        body_sent = asyncio.Event()
        stop_ticking = asyncio.Event()

        async def business_response(_request):
            return Response("ready")

        async def send(message):
            if message["type"] == "http.response.body":
                order.append("body")
                body_sent.set()

        async def ticker():
            while not stop_ticking.is_set():
                if recorder_started.is_set() and not recorder_finished.is_set():
                    observed["ticks_while_writing"] += 1
                await asyncio.sleep(0.002)

        response = await main.security_headers(request, business_response)
        assert not recorder_started.is_set()
        ticking = asyncio.create_task(ticker())
        task = asyncio.create_task(deliver(response, request, send))
        started = time.monotonic()
        try:
            await asyncio.wait_for(body_sent.wait(), 0.25)
            await asyncio.wait_for(task, 0.75)
        finally:
            stop_ticking.set()
            await ticking
        observed["elapsed"] = time.monotonic() - started

    try:
        asyncio.run(scenario())
        assert order == ["body", "telemetry"]
        assert observed["recorder_thread"] != observed["event_loop_thread"]
        assert observed["ticks_while_writing"] > 0
        assert observed["elapsed"] < 0.75
        assert metric_rows(metrics_database) == []
    finally:
        writer.rollback()
        writer.close()

    # A dropped contended metric must not prevent later ordinary recording.
    database.record_api_usage_event(None, "GET", "/api/after-lock", 200, 3)
    assert metric_rows(metrics_database)[0]["route"] == "/api/after-lock"


def test_failed_telemetry_does_not_fail_response_or_leak_exception_content(metrics_database, monkeypatch, caplog):
    completed = []

    def fail_recorder(*_args):
        raise sqlite3.OperationalError("DO_NOT_LOG_PRIVATE_DATABASE_DETAIL")

    monkeypatch.setattr(database, "record_api_usage_event", fail_recorder)
    caplog.set_level("INFO", logger=main.__name__)

    async def scenario():
        request = request_for()

        async def business_response(_request):
            return Response("ok", background=BackgroundTask(completed.append, "original"))

        response = await main.security_headers(request, business_response)
        await deliver(response, request)
        assert response.status_code == 200

    asyncio.run(scenario())
    assert completed == ["original"]
    assert "OperationalError" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text
    assert metric_rows(metrics_database) == []


def test_unhandled_business_error_is_not_held_by_telemetry(metrics_database, monkeypatch):
    release_worker = threading.Event()
    worker_finished = threading.Event()
    calls = []

    def recorder(*args):
        calls.append(args)
        try:
            release_worker.wait(timeout=1)
        finally:
            worker_finished.set()

    monkeypatch.setattr(database, "record_api_usage_event", recorder)

    async def scenario():
        async def failed_business(_request):
            raise ValueError("original business failure")

        started = time.monotonic()
        try:
            with pytest.raises(ValueError, match="original business failure"):
                await main.security_headers(request_for(), failed_business)
            assert time.monotonic() - started < 0.2
        finally:
            release_worker.set()
        deadline = time.monotonic() + 1
        while not worker_finished.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert worker_finished.is_set()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][0:4] == (
        None, "GET", "/api/future-radar/opportunities/{job_id}", 500,
    )
    assert all(not isinstance(value, Request) for value in calls[0])


@pytest.mark.parametrize("path", ["/api/health", "/api/admin/usage", "/index.html"])
def test_untracked_paths_keep_original_background_only(metrics_database, path):
    completed = []
    original = BackgroundTask(completed.append, "original")

    async def scenario():
        request = request_for(path)

        async def business_response(_request):
            return Response("ok", background=original)

        response = await main.security_headers(request, business_response)
        assert response.background is original
        await deliver(response, request)

    asyncio.run(scenario())
    assert completed == ["original"]
    assert metric_rows(metrics_database) == []
