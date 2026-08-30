"""Real independent-connection lease tests; never use deployment credentials.

SQLite always runs. PostgreSQL is opt-in via FROSTFIRE_TEST_POSTGRES_URL and
restricted to the explicitly provisioned local frostfire_test database/user.
Every PostgreSQL case owns a fresh ff_lock_test_* schema and drops only that
schema. All service adapters below are in-process fakes: no network/AI calls.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture(params=("sqlite", "postgres"))
def lock_database(request, tmp_path, monkeypatch):
    # Importing the repository traverses config.py; do not load any real .env.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "not-used-in-lease-tests")
    monkeypatch.setenv("JWT_SECRET", "isolated-lease-test-secret-only")
    monkeypatch.setenv("FUTURE_RADAR_ENABLED", "false")

    from backend.future_radar.schema import migrate

    cleanup = lambda: None
    if request.param == "sqlite":
        path = tmp_path / "radar-locks.db"

        def connect():
            connection = sqlite3.connect(path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        with connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
    else:
        dsn = os.environ.get("FROSTFIRE_TEST_POSTGRES_URL")
        if not dsn:
            pytest.skip("FROSTFIRE_TEST_POSTGRES_URL is not explicitly configured")
        psycopg = pytest.importorskip("psycopg")
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(dsn)
        assert info.get("host") in {"127.0.0.1", "::1", "localhost"}, (
            "Lease integration tests only allow a loopback PostgreSQL host"
        )
        assert info.get("hostaddr", info.get("host")) in {
            "127.0.0.1", "::1", "localhost",
        }, "Lease integration tests cannot override the loopback host"
        assert info.get("dbname") == "frostfire_test", "Use the isolated test database"
        assert info.get("user") == "frostfire_test", "Use the isolated test role"
        assert not info.get("password"), "These local tests do not accept credentials"
        from backend.storage import close_postgres_pools, connect_postgres

        schema = f"ff_lock_test_{uuid.uuid4().hex}"
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        def connect():
            return connect_postgres(dsn, schema=schema, timeout=5, max_size=4)

        def cleanup():
            close_postgres_pools()
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

    try:
        connection = connect()
        try:
            migrate(connection)
            connection.commit()
        finally:
            connection.close()
        yield SimpleNamespace(connect=connect, backend=request.param)
    finally:
        cleanup()


@pytest.fixture
def lease_repository(lock_database):
    from backend.future_radar.repository import RadarRepository

    return RadarRepository(lock_database.connect)


def _lease(repository, name):
    connection = repository._connect()
    try:
        row = connection.execute(
            "SELECT * FROM radar_locks WHERE lock_name=?", (name,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


class _PlainTransactionConnection:
    """Prove CAS works even without the legacy global write-lock fallback."""

    def __init__(self, connection, barrier=None, statements=None):
        self.connection = connection
        self.barrier = barrier
        self.statements = statements

    def execute(self, query, parameters=()):
        if query == "BEGIN IMMEDIATE":
            result = self.connection.execute("BEGIN")
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            return result
        if self.statements is not None:
            self.statements.append(query)
        return self.connection.execute(query, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("name", (
    "future-radar-run:quick", "future-radar-run:deep", "future-radar-source:official-01",
))
@pytest.mark.parametrize("expired", (False, True), ids=("new", "expired"))
def test_two_independent_connections_only_one_lease_winner(lock_database, name, expired):
    from backend.future_radar.repository import RadarRepository

    repository = RadarRepository(lock_database.connect)
    if expired:
        assert repository.acquire_lock(name, "abandoned-worker", 60)
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE radar_locks SET expires_at=? WHERE lock_name=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), name),
            )

    # Hold two real connections simultaneously. Disabling BEGIN IMMEDIATE's
    # write serialization makes this exercise the conditional UPSERT itself,
    # not just SQLite's lock or the PostgreSQL adapter's advisory fallback.
    barrier = threading.Barrier(2)
    connections = []
    connection_guard = threading.Lock()

    def connect():
        connection = lock_database.connect()
        with connection_guard:
            connections.append(connection)
        return _PlainTransactionConnection(connection, barrier=barrier)

    def compete(owner):
        return owner, RadarRepository(connect).acquire_lock(name, owner, 60)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete, owner) for owner in ("worker-a", "worker-b")]
        outcomes = [future.result(timeout=10) for future in futures]

    assert len(connections) == 2
    assert connections[0] is not connections[1]
    winners = [owner for owner, acquired in outcomes if acquired]
    assert len(winners) == 1
    assert _lease(repository, name)["owner"] == winners[0]


def test_acquire_is_single_conditional_upsert_not_select_then_write(lock_database):
    from backend.future_radar.repository import RadarRepository

    statements = []
    repository = RadarRepository(lambda: _PlainTransactionConnection(
        lock_database.connect(), statements=statements,
    ))
    assert repository.acquire_lock("future-radar-run:quick", "first", 60)
    assert not repository.acquire_lock("future-radar-run:quick", "second", 60)
    assert len(statements) == 2
    assert all("SELECT" not in query.upper() for query in statements)
    assert all("ON CONFLICT" in query and "RETURNING owner" in query for query in statements)


def test_expired_takeover_racing_heartbeat_has_only_one_winner(lock_database):
    from backend.future_radar.repository import RadarRepository

    name = "future-radar-source:official-01"
    repository = RadarRepository(lock_database.connect)
    assert repository.acquire_lock(name, "old-owner", 60)
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE radar_locks SET expires_at=? WHERE lock_name=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), name),
        )
    barrier = threading.Barrier(2)

    def worker_repository():
        return RadarRepository(lambda: _PlainTransactionConnection(
            lock_database.connect(), barrier=barrier,
        ))

    with ThreadPoolExecutor(max_workers=2) as executor:
        renewal = executor.submit(worker_repository().renew_lock, name, "old-owner", 60)
        takeover = executor.submit(worker_repository().acquire_lock, name, "new-owner", 60)
        renewed, acquired = renewal.result(timeout=10), takeover.result(timeout=10)

    assert int(renewed) + int(acquired) == 1
    assert _lease(repository, name)["owner"] == ("old-owner" if renewed else "new-owner")


@pytest.mark.parametrize("name", ("future-radar-run:quick", "future-radar-source:official-01"))
def test_lease_takeover_renewal_and_owner_guarded_release(lease_repository, name):
    repository = lease_repository
    assert repository.acquire_lock(name, "old-owner", 5)
    first_expiry = _lease(repository, name)["expires_at"]
    assert repository.acquire_lock(name, "old-owner", 60)
    assert _lease(repository, name)["expires_at"] > first_expiry
    assert not repository.renew_lock(name, "outsider", 600)
    assert repository.renew_lock(name, "old-owner", 120)

    with repository.transaction() as connection:
        connection.execute(
            "UPDATE radar_locks SET expires_at=? WHERE lock_name=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), name),
        )
    assert repository.acquire_lock(name, "new-owner", 60)
    assert not repository.renew_lock(name, "old-owner", 600)
    repository.release_lock(name, "old-owner")
    assert _lease(repository, name)["owner"] == "new-owner"
    assert repository.renew_lock(name, "new-owner", 90)
    repository.release_lock(name, "new-owner")
    assert _lease(repository, name) is None
    assert not repository.renew_lock(name, "new-owner", 90)
    # There is deliberately no post-completion five-minute cooldown.
    assert repository.acquire_lock(name, "third-owner", 60)


def test_expired_same_owner_can_renew_until_another_owner_takes_over(lease_repository):
    repository = lease_repository
    name = "future-radar-source:official-01"
    assert repository.acquire_lock(name, "owner", 60)
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE radar_locks SET expires_at=? WHERE lock_name=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), name),
        )
    assert repository.renew_lock(name, "owner", 60)
    assert not repository.acquire_lock(name, "other", 60)


def test_run_types_and_source_keys_are_independently_available(lease_repository):
    repository = lease_repository
    keys = (
        "future-radar-run:quick", "future-radar-run:deep", "future-radar-run:scheduled",
        "future-radar-source:official-01", "future-radar-source:official-02",
    )
    for index, name in enumerate(keys):
        assert repository.acquire_lock(name, f"worker-{index}", 60)
    for index, name in enumerate(keys):
        assert not repository.acquire_lock(name, "unrelated-worker", 60)
        repository.release_lock(name, f"worker-{index}")
        assert repository.acquire_lock(name, "unrelated-worker", 60)


def _service(lock_database, adapter, *, ttl=60):
    from backend.future_radar.service import FutureRadarService

    return FutureRadarService(
        connect=lock_database.connect,
        openai_api_key="",  # The adapter is always an in-memory fake.
        ai_model="not-used",
        web_search_enabled=False,
        adapter_factory=lambda _source: adapter,
        run_lock_ttl_seconds=ttl,
        source_lock_ttl_seconds=ttl,
    )


def _source(service, source_id, *, deep=False):
    adapter = "openai_web_search" if deep else "official_html"
    return service.repository.create_source({
        "id": source_id,
        "name": source_id,
        "platform": "isolated-test",
        "source_type": adapter,
        "enabled": True,
        "interval_minutes": 120,
        "trust_level": "discovery",
        "adapter_config": {"adapter": adapter, "ai_extract": False},
    })


class _BlockingFakeAdapter:
    def __init__(self, *, blocked_source="source-a", ai_calls=0):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.blocked_source = blocked_source
        self.ai_calls = ai_calls

    def scan(self, source):
        from backend.future_radar.adapters import AdapterResult

        self.calls.append(source["id"])
        if source["id"] == self.blocked_source:
            self.entered.set()
            assert self.release.wait(timeout=10), "Test did not release the fake adapter"
        return AdapterResult(
            content_hash="isolated-lock-test",
            snapshot_complete=False,
            ai_calls=self.ai_calls,
        )


@pytest.mark.parametrize("scan_type", ("quick", "deep"))
def test_new_service_cannot_duplicate_run_or_ai_and_can_restart_immediately(
    lock_database, scan_type,
):
    from backend.future_radar.service import RadarRunBusy

    adapter = _BlockingFakeAdapter(ai_calls=int(scan_type == "deep"))
    service = _service(lock_database, adapter)
    _source(service, "source-a", deep=scan_type == "deep")
    contender = _service(lock_database, adapter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(service.run, scan_type=scan_type, source_ids=["source-a"])
        try:
            assert adapter.entered.wait(timeout=5)
            assert contender.repository.active_run_types() == [scan_type]
            # A separate service/connection models another worker or browser
            # refresh. Even Force Scan must not bypass a currently active run.
            with pytest.raises(RadarRunBusy):
                contender.run(scan_type=scan_type, source_ids=["source-a"], force=True)
            assert contender.repository.list_runs()["total"] == 1
            assert adapter.calls == ["source-a"]
        finally:
            adapter.release.set()
        assert first.result(timeout=5)["status"] == "success"

    assert contender.repository.active_run_types() == []
    second = contender.run(scan_type=scan_type, source_ids=["source-a"])
    assert second["status"] == "success"
    assert second["ai_calls"] == int(scan_type == "deep")
    assert contender.repository.list_runs()["total"] == 2
    assert adapter.calls == ["source-a", "source-a"]
    assert contender.repository.get_source("source-a")["interval_minutes"] == 120


def test_busy_source_does_not_hold_transaction_or_block_other_sources(lock_database):
    adapter = _BlockingFakeAdapter()
    service = _service(lock_database, adapter)
    _source(service, "source-a")
    _source(service, "source-b")
    contender = _service(lock_database, adapter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(service.run, scan_type="quick", source_ids=["source-a"])
        try:
            assert adapter.entered.wait(timeout=5)
            # Different run types may proceed, but the shared source lease
            # skips source-a. source-b must finish while source-a is still
            # blocked, proving no database transaction spans adapter work.
            other = contender.run(scan_type="scheduled", source_ids=["source-a", "source-b"])
            assert other["status"] == "partial_success"
            assert other["sources_skipped"] == 1
            assert other["sources_succeeded"] == 1
            assert other["errors"][0]["code"] == "SOURCE_BUSY"
            assert adapter.calls == ["source-a", "source-b"]
            assert not first.done()
        finally:
            adapter.release.set()
        assert first.result(timeout=5)["status"] == "success"


def test_run_and_source_heartbeats_keep_both_leases_live(lock_database):
    adapter = _BlockingFakeAdapter()
    service = _service(lock_database, adapter, ttl=1)
    _source(service, "source-a")
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(service.run, scan_type="quick", source_ids=["source-a"])
        try:
            assert adapter.entered.wait(timeout=5)
            time.sleep(1.3)  # Cross the original lease expiry; no provider calls.
            for name in ("future-radar-run:quick", "future-radar-source:source-a"):
                lease = _lease(service.repository, name)
                assert datetime.fromisoformat(lease["expires_at"]) > datetime.now(timezone.utc)
                assert not service.repository.acquire_lock(name, "new-worker", 60)
        finally:
            adapter.release.set()
        assert first.result(timeout=5)["status"] == "success"
    assert service.repository.active_run_types() == []
    assert _lease(service.repository, "future-radar-source:source-a") is None
