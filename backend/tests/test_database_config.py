"""Storage configuration is explicit and never silently loses persistence."""

import os

import pytest


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-config-only-at-least-32-characters")

from backend.config import load_settings


@pytest.fixture(autouse=True)
def isolated_database_environment(monkeypatch):
    for key in (
        "DATABASE_BACKEND", "DATABASE_URL", "DATABASE_SCHEMA", "DATABASE_POOL_SIZE", "RENDER",
    ):
        monkeypatch.delenv(key, raising=False)


def test_local_development_keeps_sqlite_as_default():
    options = load_settings()
    assert options.database_backend == "sqlite"
    assert options.database_url == ""


def test_postgres_url_selects_persistent_backend(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:do-not-print@db.example/app")
    options = load_settings()
    assert options.database_backend == "postgres"
    assert options.database_schema == "frostfire"
    assert options.database_pool_size == 4
    assert "do-not-print" not in repr(options)
    assert "db.example" not in repr(options)


def test_explicit_postgresql_alias(monkeypatch):
    monkeypatch.setenv("DATABASE_BACKEND", "PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", "postgres://test@localhost/test")
    assert load_settings().database_backend == "postgres"


@pytest.mark.parametrize("url", ["", "sqlite:///somewhere.db", "https://db.example/?key=secret"])
def test_postgres_never_falls_back_to_temporary_sqlite(monkeypatch, url):
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(RuntimeError, match="PostgreSQL DATABASE_URL") as caught:
        load_settings()
    assert "key=secret" not in str(caught.value)


@pytest.mark.parametrize("backend", ["", "sqlite"])
def test_render_refuses_ephemeral_sqlite(monkeypatch, backend):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DATABASE_BACKEND", backend)
    with pytest.raises(RuntimeError, match="Render requires PostgreSQL persistence"):
        load_settings()


def test_render_accepts_explicit_persistent_database(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test@localhost/test")
    assert load_settings().database_backend == "postgres"


@pytest.mark.parametrize("schema", [
    "public", "auth", "storage", "realtime", "extensions", "information_schema",
    "graphql", "graphql_public", "supabase_functions", "supabase_migrations",
    "pg_catalog", "PG_private",
    "a;DROP TABLE users", "with-dash", "0first", "名字", "x" * 64,
])
def test_application_schema_must_be_private_and_valid(monkeypatch, schema):
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    with pytest.raises(RuntimeError, match="private application schema"):
        load_settings()


@pytest.mark.parametrize("size,expected", [("0", 1), ("4", 4), ("99", 12), ("", 4)])
def test_small_connection_pool(monkeypatch, size, expected):
    monkeypatch.setenv("DATABASE_POOL_SIZE", size)
    assert load_settings().database_pool_size == expected


def test_unknown_backend_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_BACKEND", "unknown")
    with pytest.raises(RuntimeError, match="DATABASE_BACKEND"):
        load_settings()
