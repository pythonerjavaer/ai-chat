"""Exercise actual application APIs against an isolated local PostgreSQL.

These tests never use DATABASE_URL, Keychain, remote databases or real AI. The
explicit local test DSN and a unique schema are required for every test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections import deque
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def persistent_app(monkeypatch, tmp_path):
    dsn = os.environ.get("FROSTFIRE_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("FROSTFIRE_TEST_POSTGRES_URL is required")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert params.get("hostaddr", params.get("host")) in {"127.0.0.1", "localhost", "::1"}
    assert params.get("user") == params.get("dbname") == "frostfire_test"
    assert not params.get("password")

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "no-external-ai-in-database-tests")
    monkeypatch.setenv("JWT_SECRET", "local-postgres-api-test-secret-at-least-32-characters")
    monkeypatch.setenv("FUTURE_RADAR_ENABLED", "false")
    monkeypatch.setenv("RECRUITMENT_REFRESH_MINUTES", "0")
    monkeypatch.setenv("RECRUITMENT_WEB_SEARCH_ENABLED", "false")
    from backend import database, main
    from backend.future_radar.service import FutureRadarService
    from backend.storage import close_postgres_pools

    schema = f"ff_app_test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    options = vars(main.settings).copy()
    options.update(
        database_backend="postgres", database_url=dsn, database_schema=schema,
        database_pool_size=4, database_path=tmp_path / "must-not-be-created.sqlite3",
        future_radar_enabled=False, recruitment_refresh_minutes=0,
        recruitment_web_search_enabled=False,
        admin_dashboard_token="local-fixture-admin-only-not-production",
    )
    monkeypatch.setattr(main, "settings", SimpleNamespace(**options))
    monkeypatch.setattr(database, "settings", SimpleNamespace(**options))
    monkeypatch.setattr(main, "_registration_requests", deque())
    monkeypatch.setattr(main, "_model_user_units", {})

    def no_network(_source):
        raise AssertionError("PostgreSQL API reads must not call network adapters")

    service = FutureRadarService(
        connect=database.connect, openai_api_key="unused", ai_model="unused",
        web_search_enabled=False, adapter_factory=no_network,
    )
    monkeypatch.setattr(main, "future_radar_service", service)
    try:
        yield SimpleNamespace(
            dsn=dsn, schema=schema, database=database, main=main, service=service,
        )
        assert not options["database_path"].exists()
    finally:
        close_postgres_pools()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def register(client, name):
    response = client.post("/api/auth/register", json={
        "username": name, "password": "local-fixture-password-123",
        "privacy_accepted": True,
    })
    assert response.status_code == 201
    data = response.json()
    return {"Authorization": "Bearer " + data["access_token"]}, data["user"]


def test_accounts_chat_documents_profiles_spaces_and_tokens_survive_restart(persistent_app, monkeypatch):
    from fastapi.testclient import TestClient

    app = persistent_app
    monkeypatch.setattr(app.main, "retrieve_context", lambda *_: [])
    monkeypatch.setattr(app.main, "create_embeddings", lambda chunks: [[1.0, 0.0] for _ in chunks])
    monkeypatch.setattr(app.main, "run_agent", lambda *_: (
        "Local deterministic test reply", [],
        {"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
    ))
    with TestClient(app.main.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["database"] == "postgres"
        headers, user = register(client, "postgres-user")
        duplicate = client.post("/api/auth/register", json={
            "username": "POSTGRES-USER", "password": "another-fixture-password",
            "privacy_accepted": True,
        })
        assert duplicate.status_code == 409
        chat = client.post("/api/chat", headers=headers, json={"message": "Fixture message"})
        assert chat.status_code == 200
        session_id = chat.json()["session_id"]
        document = client.post("/api/documents", headers=headers, files={
            "file": ("fixture.md", "Only local fixture content.", "text/markdown"),
        })
        assert document.status_code == 201
        space = client.post("/api/spaces", headers=headers, json={
            "name": "Persistent fixture", "template_id": "blank",
        })
        assert space.status_code == 201
        profile = client.put("/api/recruitment/profile", headers=headers, json={
            "locations": ["上海"], "desired_roles": ["数据分析"],
        })
        assert profile.status_code == 200
        second_headers, _ = register(client, "other-postgres-user")
        assert client.get(f"/api/sessions/{session_id}/messages", headers=second_headers).status_code == 404
        assert client.get("/api/documents", headers=second_headers).json() == []
        assert client.get("/api/spaces", headers=second_headers).json() == []
        assert app.database.model_token_usage(user["id"])["total_tokens"] == 9
        assert client.get("/api/admin/usage").status_code == 401
        usage = client.get("/api/admin/usage", headers={
            "X-Admin-Token": "local-fixture-admin-only-not-production",
        })
        assert usage.status_code == 200
        assert usage.json()["totals"]["registered_users"] == 2
        assert usage.json()["totals"]["documents"] == 1
        assert usage.json()["totals"]["total_tokens"] == 9
        assert usage.json()["totals"]["active_users_24h"] == 2
        assert usage.json()["totals"]["api_requests"] > 0
        assert "postgres-user" not in usage.text
        assert "Fixture message" not in usage.text
        assert "Only local fixture content" not in usage.text
        assert "local-fixture-password" not in usage.text

    # Read in a genuinely separate interpreter after all application pools have
    # closed, not merely through another reference to an in-memory connection.
    child_environment = {
        **os.environ, "PYTHON_DOTENV_DISABLED": "1",
        "OPENAI_API_KEY": "unused-local-fixture", "JWT_SECRET": "fixture-secret-at-least-32-characters",
        "DATABASE_BACKEND": "postgres", "DATABASE_URL": app.dsn,
        "DATABASE_SCHEMA": app.schema, "DATABASE_POOL_SIZE": "2",
    }
    check = subprocess.run([
        sys.executable, "-c",
        "import json; from backend import database; "
        "c=database.connect(); "
        "counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] "
        "for t in ('users','sessions','messages','documents','chunks','spaces','recruitment_profiles','token_usage')}; "
        "c.close(); database.close_database_pools(); print(json.dumps(counts))",
    ], env=child_environment, cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=20)
    assert check.returncode == 0, "Separate-process persistence check failed"
    counts = json.loads(check.stdout)
    assert counts == {
        "users": 2, "sessions": 1, "messages": 2, "documents": 1,
        "chunks": 1, "spaces": 1, "recruitment_profiles": 1, "token_usage": 1,
    }

    with TestClient(app.main.app) as restarted:
        login = restarted.post("/api/auth/login", json={
            "username": "POSTGRES-USER", "password": "local-fixture-password-123",
        })
        assert login.status_code == 200
        assert login.json()["user"]["id"] == user["id"]
        assert login.json()["user"]["privacy_accepted"] is True
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        messages = restarted.get(f"/api/sessions/{session_id}/messages", headers=headers)
        assert messages.status_code == 200
        assert [m["content"] for m in messages.json()] == [
            "Fixture message", "Local deterministic test reply",
        ]
        assert restarted.get("/api/documents", headers=headers).json()[0]["id"] == document.json()["id"]
        assert restarted.get("/api/spaces", headers=headers).json()[0]["id"] == space.json()["id"]
        assert restarted.get("/api/recruitment/profile", headers=headers).json()["locations"] == ["上海"]


def test_large_unified_pool_filters_links_and_counts_survive_restart(persistent_app):
    from fastapi.testclient import TestClient
    from backend.future_radar.normalization import normalize_job
    from backend.future_radar.repository import utc_now

    app = persistent_app
    cohort = date.today().year + int(date.today().month >= 6)
    with TestClient(app.main.app) as client:
        headers, _ = register(client, "pool-postgres-user")
        for index in range(56):
            marker = chr(65 + index // 26) + chr(65 + index % 26)
            source_id = "legacy-recruitment-pipeline" if index % 2 else "legacy-search-discovery"
            source = app.service.repository.get_source(source_id)
            item = normalize_job({
                "external_id": f"fixture-{marker}", "company": "示例科技",
                "title": f"{cohort} 校园招聘数据分析岗 {marker}",
                "city": "上海" if index < 30 else "北京", "region": "中国大陆",
                "employer_type": "互联网企业", "industry": "科技",
                "primary_category": "internet_tech", "status": "open",
                "verification_status": "verified" if index % 2 else "pending",
                "official_url": f"https://careers.example.com/campus/{marker}",
                "tags": ["校园招聘", f"{cohort}届"],
                "requirements": "面向应届毕业生，开展数据分析和业务研究。",
                "description": "负责数据分析、研究经营问题、构建指标体系。",
            })
            with app.service.repository.transaction() as connection:
                saved = app.service.repository.insert_job(
                    connection, item, source_id=source_id, program_id=None, now=utc_now(),
                )
                app.service.repository.link_job_source(
                    connection, job_id=saved["id"], source=source,
                    source_url=item["official_url"], now=utc_now(),
                    verification_role="verification" if index % 2 else "discovery",
                    evidence=["PUBLIC LOCAL TEST FIXTURE"],
                )
        first = client.get("/api/future-radar/opportunities", headers=headers)
        assert first.status_code == 200
        assert first.json()["total"] == 56
        assert len(first.json()["items"]) == 50
        second = client.get("/api/future-radar/opportunities?page=2", headers=headers).json()
        assert second["total"] == 56 and len(second["items"]) == 6
        assert not ({row["id"] for row in first.json()["items"]} & {row["id"] for row in second["items"]})
        for state in ("pending", "verified"):
            filtered = client.get("/api/future-radar/opportunities", params={
                "verification_status": state, "page_size": 100,
            }, headers=headers)
            assert filtered.status_code == 200
            assert filtered.json()["total"] == 28
            for item in filtered.json()["items"]:
                detail = client.get(f"/api/future-radar/opportunities/{item['id']}", headers=headers)
                assert detail.status_code == 200
                assert detail.json()["official_url"].startswith("https://careers.example.com/campus/")
                assert detail.json()["verification_status"] == state
                assert detail.json()["officially_verified"] is (state == "verified")
        city = client.get("/api/future-radar/opportunities", params={"city": "上海"}, headers=headers)
        assert city.status_code == 200 and city.json()["total"] == 30
        wrong_category = client.get("/api/future-radar/opportunities", params={
            "category": "policy_state_banks",
        }, headers=headers)
        assert wrong_category.status_code == 200
        assert wrong_category.json()["total"] == 0

    with TestClient(app.main.app) as restarted:
        login = restarted.post("/api/auth/login", json={
            "username": "pool-postgres-user", "password": "local-fixture-password-123",
        })
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        persisted = restarted.get("/api/future-radar/opportunities?page_size=100", headers=headers)
        assert persisted.status_code == 200
        assert persisted.json()["total"] == len(persisted.json()["items"]) == 56


def test_health_fails_closed_without_returning_connection_details(monkeypatch):
    from fastapi import HTTPException
    from backend import database, main

    seen = []

    def unavailable(*, timeout):
        seen.append(timeout)
        raise RuntimeError("postgresql://private-user:private-password@private-host/private-db")

    monkeypatch.setattr(database, "connect", unavailable)
    with pytest.raises(HTTPException) as caught:
        main.health()
    assert caught.value.status_code == 503
    assert caught.value.detail == "Database is unavailable."
    assert "private-" not in str(caught.value)
    assert seen == [2.0]
