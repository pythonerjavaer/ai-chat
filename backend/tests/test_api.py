import os
import json
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest


TEST_DIRECTORY = Path(tempfile.mkdtemp(prefix="ai-chat-tests-"))
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-tests"
os.environ["DATABASE_PATH"] = str(TEST_DIRECTORY / "test.db")
os.environ["RECRUITMENT_INGEST_TOKEN"] = "test-recruitment-ingest-token"
os.environ["ADMIN_DASHBOARD_TOKEN"] = "test-admin-dashboard-token"
os.environ["RECRUITMENT_REFRESH_MINUTES"] = "0"

from fastapi.testclient import TestClient

from backend import main
from backend import ai_service
from backend import database
from backend import recruitment_search
from backend import recruitment_watch
from backend.ai_service import (
    build_messages,
    calculate,
    calculate_financial_metric,
    extract_document,
    split_document,
    tools_for_workspace,
)
from backend.recruitment_watch import WatchFetchResult, fetch_watch_page


CURRENT_RECRUITMENT_COHORT_YEAR = (
    date.today().year + (1 if date.today().month >= 6 else 0)
)
CURRENT_RECRUITMENT_COHORT = f"{CURRENT_RECRUITMENT_COHORT_YEAR}届"
TEST_AUTHORIZED_ATS = "https://app.mokahr.com"


@pytest.fixture(autouse=True)
def offline_official_discovery_followup(monkeypatch):
    # These pre-existing API tests isolate hosted search and its single-page
    # verifier. Real linked-list traversal is covered with synthetic HTTP in
    # test_official_job_discovery.py, never by live internet in the API suite.
    monkeypatch.setattr(
        recruitment_search, "discover_official_job_pages",
        lambda *_args, **_kwargs: SimpleNamespace(candidates=(), coverage={
            "status": "partial", "pagination_complete": False,
            "snapshot_complete": False, "completion_reason": "offline_test_fixture",
        }),
    )


def employer_only_pool(pool: dict, employer: str) -> dict:
    return {**pool, "employers": [employer]}


def coverage_payload(pool: dict, jobs: list[dict]) -> dict:
    targets = recruitment_search.build_employer_search_targets([pool])
    assert len(targets) == 1
    target_id = targets[0].id
    return {
        "checked_employers": [{
            "target_id": target_id,
            "status": "open_jobs_found" if jobs else "no_current_opening",
        }],
        "jobs": [{**job, "target_id": target_id} for job in jobs],
    }


def register(client: TestClient, username: str) -> tuple[str, dict]:
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": "correct-horse-123",
            "privacy_accepted": True,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    return payload["access_token"], payload["user"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_authentication_and_user_isolation():
    with TestClient(main.app) as client:
        alice_token, _ = register(client, "alice")
        duplicate = client.post(
            "/api/auth/register",
            json={
                "username": "ALICE",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        )
        assert duplicate.status_code == 409

        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct-horse-123"},
        )
        assert login.status_code == 200
        assert client.get("/api/auth/me", headers=auth(alice_token)).status_code == 200

        created = client.post(
            "/api/sessions",
            headers=auth(alice_token),
            json={"title": "Alice private session"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        bob_token, _ = register(client, "bob")
        forbidden = client.get(
            f"/api/sessions/{session_id}/messages",
            headers=auth(bob_token),
        )
        assert forbidden.status_code == 404


def test_admin_usage_is_token_protected_aggregate_only():
    with TestClient(main.app) as client:
        missing = client.get("/api/admin/usage")
        assert missing.status_code == 401
        wrong = client.get(
            "/api/admin/usage",
            headers={"X-Admin-Token": "wrong-token"},
        )
        assert wrong.status_code == 401

        _, user = register(client, "admin-metrics-user")
        session = database.create_session(user["id"], title="Sensitive title")
        database.append_message(session["id"], "user", "TOP SECRET CHAT BODY")
        database.append_message(session["id"], "assistant", "PRIVATE MODEL REPLY")
        database.create_document(
            user["id"],
            "private-document.md",
            "CONFIDENTIAL DOCUMENT CONTENT",
            [{"content": "CONFIDENTIAL CHUNK", "page": None}],
            [[1.0, 0.0]],
        )
        space = database.create_space(
            user["id"],
            "Private Space",
            "Private description",
            "X",
            "mono",
            "blank",
            "PRIVATE SYSTEM PROMPT",
            10_000,
        )
        database.create_space_run(
            user["id"],
            space["id"],
            "fingerprint",
            "lean",
            "lean",
            "PRIVATE SPACE INPUT",
            {},
            "PRIVATE SPACE OUTPUT",
            actual_input_tokens=17,
            actual_output_tokens=5,
            actual_total_tokens=22,
        )
        database.record_token_usage(user["id"], space["id"], 17, 5, 22)

        response = client.get(
            "/api/admin/usage?hours=24&bucket_minutes=60",
            headers={"X-Admin-Token": "test-admin-dashboard-token"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["totals"]["users"] >= 1
        assert payload["totals"]["active_users_24h"] >= 1
        assert payload["totals"]["sessions"] >= 1
        assert payload["totals"]["messages"] >= 2
        assert payload["totals"]["documents"] >= 1
        assert payload["totals"]["ai_requests"] >= 2
        assert payload["totals"]["input_tokens"] >= 17
        assert payload["recent"]["registrations_24h"] >= 1
        assert payload["recent"]["messages_24h"] >= 2
        assert payload["recent"]["ai_requests_24h"] >= 2
        assert payload["series"]
        serialized = response.text
        for secret in (
            "TOP SECRET CHAT BODY",
            "PRIVATE MODEL REPLY",
            "CONFIDENTIAL DOCUMENT CONTENT",
            "CONFIDENTIAL CHUNK",
            "PRIVATE SYSTEM PROMPT",
            "PRIVATE SPACE INPUT",
            "PRIVATE SPACE OUTPUT",
            "correct-horse-123",
        ):
            assert secret not in serialized


def test_admin_usage_unconfigured_does_not_break_health(monkeypatch):
    unconfigured = vars(main.settings).copy()
    unconfigured["admin_dashboard_token"] = ""
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(**unconfigured),
    )
    with TestClient(main.app) as client:
        unavailable = client.get(
            "/api/admin/usage",
            headers={"X-Admin-Token": "anything"},
        )
        assert unavailable.status_code == 503
        assert client.get("/api/health").status_code == 200


def test_api_usage_events_expire_after_retention_window():
    expired_at = (
        datetime.now(timezone.utc)
        - timedelta(days=database.API_USAGE_RETENTION_DAYS + 1)
    ).isoformat()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO api_usage_events
                (user_id, method, route, status_code, duration_ms, created_at)
            VALUES (NULL, 'GET', '/api/expired-test', 200, 1, ?)
            """,
            (expired_at,),
        )
    database.record_api_usage_event(None, "GET", "/api/current-test", 200, 1)
    with database.connect() as connection:
        expired_count = connection.execute(
            "SELECT COUNT(*) AS count FROM api_usage_events WHERE route = ?",
            ("/api/expired-test",),
        ).fetchone()["count"]
    assert expired_count == 0


def test_api_usage_event_detaches_deleted_or_missing_user():
    database.record_api_usage_event(999_999_999, "DELETE", "/api/auth/account", 204, 2)
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT user_id, status_code
            FROM api_usage_events
            WHERE route = '/api/auth/account'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert row["user_id"] is None
    assert row["status_code"] == 204


def test_privacy_consent_and_account_deletion():
    with TestClient(main.app) as client:
        rejected = client.post(
            "/api/auth/register",
            json={"username": "no-consent", "password": "correct-horse-123"},
        )
        assert rejected.status_code == 400

        token, user = register(client, "delete-me")
        profile = client.get("/api/auth/me", headers=auth(token)).json()
        assert profile["privacy_accepted"] is True

        stale_token, stale_user = register(client, "stale-consent")
        with database.connect() as connection:
            connection.execute(
                "UPDATE users SET privacy_version = ? WHERE id = ?",
                ("2026-08-21", stale_user["id"]),
            )
        stale_profile = client.get(
            "/api/auth/me", headers=auth(stale_token)
        ).json()
        assert stale_profile["privacy_accepted"] is False
        blocked = client.post(
            "/api/spaces",
            headers=auth(stale_token),
            json={"name": "Blocked Space", "template_id": "blank"},
        )
        assert blocked.status_code == 428
        renewed = client.post(
            "/api/auth/privacy-consent",
            headers=auth(stale_token),
            json={"accepted": True},
        )
        assert renewed.status_code == 200
        assert renewed.json()["privacy_accepted"] is True

        created = client.post(
            "/api/sessions",
            headers=auth(token),
            json={"title": "Private history"},
        )
        assert created.status_code == 201

        wrong_password = client.request(
            "DELETE",
            "/api/auth/account",
            headers=auth(token),
            json={"password": "wrong-password", "confirmation": "DELETE"},
        )
        assert wrong_password.status_code == 401

        deleted = client.request(
            "DELETE",
            "/api/auth/account",
            headers=auth(token),
            json={"password": "correct-horse-123", "confirmation": "DELETE"},
        )
        assert deleted.status_code == 204
        assert database.get_user_by_id(user["id"]) is None
        assert database.get_session(created.json()["id"], user["id"]) is None


def test_persistent_chat_and_session_history(monkeypatch):
    monkeypatch.setattr(main, "retrieve_context", lambda *_: [])
    monkeypatch.setattr(
        main,
        "run_agent",
        lambda *_: (
            "Persisted assistant reply",
            ["calculate"],
            {"input_tokens": 31, "output_tokens": 9, "total_tokens": 40},
        ),
    )

    with TestClient(main.app) as client:
        token, user = register(client, "persistent-user")
        response = client.post(
            "/api/chat",
            headers=auth(token),
            json={"message": "Remember this message"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["reply"] == "Persisted assistant reply"
        assert payload["tools_used"] == ["calculate"]
        assert database.model_token_usage(user["id"])["total_tokens"] == 40
        assert database.token_usage(user["id"])["total_tokens"] == 0
        billing = client.get("/api/billing/status", headers=auth(token))
        assert billing.status_code == 200
        assert billing.json()["remaining_tokens"] == billing.json()["limits"]["monthly_tokens"]

        messages = client.get(
            f"/api/sessions/{payload['session_id']}/messages",
            headers=auth(token),
        ).json()
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "Remember this message"
        assert messages[1]["content"] == "Persisted assistant reply"

    with TestClient(main.app) as restarted_client:
        login = restarted_client.post(
            "/api/auth/login",
            json={"username": "persistent-user", "password": "correct-horse-123"},
        )
        sessions = restarted_client.get(
            "/api/sessions",
            headers=auth(login.json()["access_token"]),
        ).json()
        assert any(item["id"] == payload["session_id"] for item in sessions)


def test_creative_single_pass_skips_rag_and_disables_tools(monkeypatch):
    def unexpected_retrieval(*_):
        raise AssertionError("creative single pass must not retrieve user documents")

    calls = []

    def fake_run_agent(messages, workspace, tools_enabled=True):
        calls.append({"messages": messages, "workspace": workspace, "tools_enabled": tools_enabled})
        return (
            "One-pass creative result",
            [],
            {"input_tokens": 18, "output_tokens": 7, "total_tokens": 25},
        )

    monkeypatch.setattr(main, "retrieve_context", unexpected_retrieval)
    monkeypatch.setattr(main, "run_agent", fake_run_agent)

    with TestClient(main.app) as client:
        token, _ = register(client, "creative-pass-user")
        response = client.post(
            "/api/chat",
            headers=auth(token),
            json={
                "message": "Project this idea once",
                "workspace": "general",
                "creative_single_pass": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "One-pass creative result"
    assert response.json()["sources"] == []
    assert response.json()["tools_used"] == []
    assert response.json()["usage"]["total_tokens"] == 25
    assert len(calls) == 1
    assert calls[0]["tools_enabled"] is False


def test_document_index_and_rag_sources(monkeypatch):
    monkeypatch.setattr(
        main,
        "create_embeddings",
        lambda chunks: [[1.0, 0.0] for _ in chunks],
    )

    with TestClient(main.app) as client:
        token, user = register(client, "rag-user")
        upload = client.post(
            "/api/documents",
            headers=auth(token),
            files={"file": ("handbook.md", "Project codename is Aurora.", "text/markdown")},
        )
        assert upload.status_code == 201
        assert upload.json()["chunk_count"] == 1
        assert client.get("/api/documents", headers=auth(token)).json()[0]["name"] == "handbook.md"

        monkeypatch.setattr(
            main,
            "retrieve_context",
            lambda *_: [
                {
                    "document_id": upload.json()["id"],
                    "name": "handbook.md",
                    "content": "Project codename is Aurora.",
                    "score": 0.99,
                }
            ],
        )
        monkeypatch.setattr(main, "run_agent", lambda *_: ("Aurora", []))
        reply = client.post(
            "/api/chat",
            headers=auth(token),
            json={"message": "What is the project codename?"},
        )
        assert reply.status_code == 200
        assert reply.json()["sources"][0]["name"] == "handbook.md"
        assert user["username"] == "rag-user"


def test_streaming_chat_persists_final_reply(monkeypatch):
    monkeypatch.setattr(main, "retrieve_context", lambda *_: [])

    def fake_stream(*_):
        yield {"type": "token", "content": "Hello "}
        yield {"type": "tool", "name": "get_current_time"}
        yield {"type": "token", "content": "world"}
        yield {
            "type": "done",
            "reply": "Hello world",
            "tools_used": ["get_current_time"],
            "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
        }

    monkeypatch.setattr(main, "stream_agent", fake_stream)

    with TestClient(main.app) as client:
        token, user = register(client, "stream-user")
        response = client.post(
            "/api/chat/stream",
            headers=auth(token),
            json={"message": "Stream this"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: token" in response.text
        assert "Hello world" not in response.text
        assert "get_current_time" in response.text
        assert database.model_token_usage(user["id"])["total_tokens"] == 24
        assert database.token_usage(user["id"])["total_tokens"] == 0

        session_id = next(
            line.split('"session_id": "', 1)[1].split('"', 1)[0]
            for line in response.text.splitlines()
            if '"session_id":' in line
        )
        messages = client.get(
            f"/api/sessions/{session_id}/messages",
            headers=auth(token),
        ).json()
        assert messages[-1]["content"] == "Hello world"


def test_safe_calculator_chunking_and_financial_metrics():
    assert calculate("(12 + 3) * 2")["result"] == 30
    assert len(split_document("A" * 2000)) >= 2

    try:
        calculate("__import__('os').system('echo unsafe')")
    except ValueError:
        pass
    else:
        raise AssertionError("Unsafe expression was accepted")

    growth = calculate_financial_metric("growth_rate", 120, 100)
    assert growth["result"] == 20
    assert growth["unit"] == "%"
    ratio = calculate_financial_metric("current_ratio", 250, 100)
    assert ratio["result"] == 2.5
    assert ratio["unit"] == "x"


def test_run_agent_without_tools_makes_exactly_one_model_call(monkeypatch):
    requests = []

    def fake_completion(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4, total_tokens=16),
            choices=[SimpleNamespace(message=SimpleNamespace(content="Projected once", tool_calls=None))],
        )

    monkeypatch.setattr(ai_service.client.chat.completions, "create", fake_completion)
    reply, tools, usage = ai_service.run_agent(
        [{"role": "user", "content": "Create one result"}],
        tools_enabled=False,
    )

    assert reply == "Projected once"
    assert tools == []
    assert usage["total_tokens"] == 16
    assert len(requests) == 1
    assert "tools" not in requests[0]
    assert "tool_choice" not in requests[0]


def test_professional_workspaces_isolate_sessions_documents_and_prompts(monkeypatch):
    monkeypatch.setattr(
        main,
        "create_embeddings",
        lambda chunks: [[1.0, 0.0] for _ in chunks],
    )
    monkeypatch.setattr(main, "retrieve_context", lambda *_: [])
    monkeypatch.setattr(main, "run_agent", lambda *_: ("ok", []))

    with TestClient(main.app) as client:
        token, user = register(client, "workspace-user")
        workspace_config = client.get("/api/workspaces").json()
        assert {item["id"] for item in workspace_config} == {
            "general",
            "legal",
            "finance",
        }

        legal_session = client.post(
            "/api/sessions",
            headers=auth(token),
            json={"title": "Contract review", "workspace": "legal"},
        )
        assert legal_session.status_code == 201
        assert legal_session.json()["workspace"] == "legal"

        legal_upload = client.post(
            "/api/documents",
            headers=auth(token),
            data={"workspace": "legal"},
            files={"file": ("agreement.md", "Payment is due in 30 days.", "text/markdown")},
        )
        finance_upload = client.post(
            "/api/documents",
            headers=auth(token),
            data={"workspace": "finance"},
            files={"file": ("results.md", "Revenue was 120 million.", "text/markdown")},
        )
        assert legal_upload.json()["workspace"] == "legal"
        assert finance_upload.json()["workspace"] == "finance"
        legal_documents = client.get(
            "/api/documents?workspace=legal",
            headers=auth(token),
        ).json()
        finance_documents = client.get(
            "/api/documents?workspace=finance",
            headers=auth(token),
        ).json()
        assert [item["name"] for item in legal_documents] == ["agreement.md"]
        assert [item["name"] for item in finance_documents] == ["results.md"]

        mismatch = client.post(
            "/api/chat",
            headers=auth(token),
            json={
                "message": "Analyze this",
                "session_id": legal_session.json()["id"],
                "workspace": "finance",
            },
        )
        assert mismatch.status_code == 409

        legal_messages = build_messages(
            user["id"],
            legal_session.json()["id"],
            [],
            "legal",
        )
        assert "Contract and Compliance workspace" in legal_messages[0]["content"]
        assert "calculate_financial_metric" not in {
            tool["function"]["name"] for tool in tools_for_workspace("legal")
        }
        assert "calculate_financial_metric" in {
            tool["function"]["name"] for tool in tools_for_workspace("finance")
        }


def test_cross_exam_links_legal_and_finance_evidence(monkeypatch):
    monkeypatch.setattr(
        main,
        "create_embeddings",
        lambda chunks: [[1.0, 0.0] for _ in chunks],
    )

    with TestClient(main.app) as client:
        token, _ = register(client, "cross-exam-user")
        legal_upload = client.post(
            "/api/documents",
            headers=auth(token),
            data={"workspace": "legal"},
            files={
                "file": (
                    "subscription.md",
                    "The agreement renews automatically and fees rise by 8%.",
                    "text/markdown",
                )
            },
        )
        finance_upload = client.post(
            "/api/documents",
            headers=auth(token),
            data={"workspace": "finance"},
            files={
                "file": (
                    "forecast.md",
                    "Operating costs are expected to increase next year.",
                    "text/markdown",
                )
            },
        )
        assert legal_upload.status_code == 201
        assert finance_upload.status_code == 201

        def fake_context(_user_id, _query, workspace, *_args):
            if workspace == "legal":
                return [{
                    "document_id": legal_upload.json()["id"],
                    "name": "subscription.md",
                    "content": "The agreement renews automatically and fees rise by 8%.",
                    "page": None,
                    "score": 0.91,
                }]
            return [{
                "document_id": finance_upload.json()["id"],
                "name": "forecast.md",
                "content": "Operating costs are expected to increase next year.",
                "page": None,
                "score": 0.88,
            }]

        monkeypatch.setattr(main, "retrieve_context", fake_context)
        monkeypatch.setattr(
            main,
            "run_cross_exam",
            lambda *_: {
                "headline": "续约条款可能放大成本压力",
                "executive_summary": "合同升级机制与成本预测形成交叉风险。",
                "collisions": [{
                    "title": "自动续约与成本上行",
                    "severity": "high",
                    "confidence": 86,
                    "legal_mechanism": "自动续约并上调费用。",
                    "financial_consequence": "预计成本压力可能被放大。",
                    "why_it_matters": "预算可能未覆盖合同升级。",
                    "legal_source_ids": ["L1"],
                    "finance_source_ids": ["F1", "F99"],
                    "missing_evidence": "缺少终止通知窗口。",
                    "next_action": "核对终止窗口并更新预算。",
                }],
                "stress_scenarios": [
                    {
                        "name": name,
                        "trigger": "示例触发条件",
                        "impact_chain": "示例影响链",
                        "early_warning": "示例预警",
                        "response": "示例响应",
                    }
                    for name in ["Base", "Downside", "Breakpoint"]
                ],
                "blind_spots": ["缺少终止窗口信息"],
            },
        )

        response = client.post(
            "/api/cross-exam",
            headers=auth(token),
            json={"focus": "检查合同对成本和现金流的影响"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["analysis_id"].startswith("FF-")
        assert payload["document_counts"] == {"legal": 1, "finance": 1}
        assert [item["source_id"] for item in payload["collisions"][0]["evidence"]] == [
            "L1",
            "F1",
        ]
        assert payload["method"]["name"] == "Clause-to-Cashflow Cross-Examination"

        missing_token, _ = register(client, "cross-exam-missing")
        missing = client.post(
            "/api/cross-exam",
            headers=auth(missing_token),
            json={"focus": "检查跨域影响"},
        )
        assert missing.status_code == 409


def test_ai_space_studio_usage_and_billing_boundaries(monkeypatch):
    monkeypatch.setattr(
        main,
        "run_space",
        lambda *_args, **_kwargs: (
            "A concise project plan.",
            {"input_tokens": 120, "output_tokens": 80, "total_tokens": 200},
        ),
    )

    with TestClient(main.app) as client:
        token, _ = register(client, "space-owner")
        templates = client.get("/api/platform/templates").json()
        assert {item["id"] for item in templates} >= {
            "project_engineer",
            "workflow_designer",
            "document_oracle",
            "blank",
        }

        created = client.post(
            "/api/spaces",
            headers=auth(token),
            json={
                "name": "My Project Engineer",
                "description": "Plans and verifies product work.",
                "template_id": "project_engineer",
                "monthly_token_budget": 1_000,
            },
        )
        assert created.status_code == 201
        space_id = created.json()["id"]
        assert created.json()["theme"] == "forge"

        run = client.post(
            f"/api/spaces/{space_id}/run",
            headers=auth(token),
            json={"message": "Plan a token-efficient MVP."},
        )
        assert run.status_code == 200
        assert run.json()["reply"] == "A concise project plan."
        assert run.json()["usage"]["total_tokens"] == 200
        assert run.json()["billing"]["usage"]["total_tokens"] == 200

        listed = client.get("/api/spaces", headers=auth(token)).json()
        assert listed[0]["usage"]["total_tokens"] == 200

        second_token, _ = register(client, "space-visitor")
        hidden = client.post(
            f"/api/spaces/{space_id}/run",
            headers=auth(second_token),
            json={"message": "Try to access another account."},
        )
        assert hidden.status_code == 404

        apple = client.post(
            "/api/billing/apple/verify",
            headers=auth(token),
            json={"signed_transaction": "x" * 20},
        )
        assert apple.status_code == 503


def test_ai_space_v2_preflight_local_cache_budget_and_history(monkeypatch):
    model_calls: list[dict] = []

    def fake_run_space(_system_prompt, message, **kwargs):
        model_calls.append({"message": message, **kwargs})
        return (
            "A reusable outcome capsule.",
            {"input_tokens": 50, "output_tokens": 40, "total_tokens": 90},
        )

    monkeypatch.setattr(main, "run_space", fake_run_space)

    with TestClient(main.app) as client:
        token, _ = register(client, "space-v2-owner")
        created = client.post(
            "/api/spaces",
            headers=auth(token),
            json={
                "name": "Outcome Capsule",
                "template_id": "workflow_designer",
                "monthly_token_budget": 1_000,
            },
        )
        assert created.status_code == 201
        space_id = created.json()["id"]

        local_preview = client.post(
            f"/api/spaces/{space_id}/preflight",
            headers=auth(token),
            json={"message": "事实：原型完成", "mode": "local"},
        )
        assert local_preview.status_code == 200
        assert local_preview.json()["execution_path"] == "local"
        assert local_preview.json()["estimated_total_tokens"] == 0
        assert local_preview.json()["estimated_tokens_saved"] > 0
        assert model_calls == []

        local_run = client.post(
            f"/api/spaces/{space_id}/run",
            headers=auth(token),
            json={
                "message": "事实：原型完成\n待确认：支付范围\n行动：验证缓存",
                "mode": "local",
            },
        )
        assert local_run.status_code == 200
        assert local_run.json()["usage"]["total_tokens"] == 0
        assert local_run.json()["artifact"]["facts"] == ["原型完成"]
        assert local_run.json()["artifact"]["open_questions"] == ["支付范围"]
        assert model_calls == []

        model_message = "Summarize this requirement.\nKeep code:\n    approve()"
        model_preview = client.post(
            f"/api/spaces/{space_id}/preflight",
            headers=auth(token),
            json={"message": model_message, "mode": "lean"},
        )
        assert model_preview.status_code == 200
        assert model_preview.json()["allowed"] is True
        assert model_preview.json()["execution_path"] == "lean"
        assert model_preview.json()["estimated_total_tokens"] > 0
        assert model_calls == []

        model_run = client.post(
            f"/api/spaces/{space_id}/runs",
            headers=auth(token),
            json={"message": model_message, "mode": "lean"},
        )
        assert model_run.status_code == 201
        assert model_run.json()["execution_path"] == "lean"
        assert model_run.json()["usage"]["total_tokens"] == 90
        assert len(model_calls) == 1
        assert model_calls[0]["mode"] == "lean"

        cached_preview = client.post(
            f"/api/spaces/{space_id}/preflight",
            headers=auth(token),
            json={
                "message": "Summarize this requirement.\r\nKeep code:\r\n    approve()",
                "mode": "lean",
            },
        )
        assert cached_preview.status_code == 200
        assert cached_preview.json()["execution_path"] == "cache"
        assert cached_preview.json()["estimated_total_tokens"] == 0
        assert cached_preview.json()["model_calls"] == 0

        indentation_sensitive_preview = client.post(
            f"/api/spaces/{space_id}/preflight",
            headers=auth(token),
            json={
                "message": "Summarize this requirement.\nKeep code:\napprove()",
                "mode": "lean",
            },
        )
        assert indentation_sensitive_preview.status_code == 200
        assert indentation_sensitive_preview.json()["execution_path"] == "lean"

        cached_run = client.post(
            f"/api/spaces/{space_id}/runs",
            headers=auth(token),
            json={
                "message": "Summarize this requirement.\r\nKeep code:\r\n    approve()",
                "mode": "lean",
            },
        )
        assert cached_run.status_code == 201
        assert cached_run.json()["execution_path"] == "cache"
        assert cached_run.json()["cache_hit"] is True
        assert cached_run.json()["cached_from_run_id"] == model_run.json()["run_id"]
        assert cached_run.json()["usage"]["total_tokens"] == 0
        assert cached_run.json()["saved_tokens"] > 0
        assert cached_run.json()["billing"]["usage"]["total_tokens"] == 90
        assert len(model_calls) == 1

        history = client.get(
            f"/api/spaces/{space_id}/runs",
            headers=auth(token),
        )
        assert history.status_code == 200
        assert [item["execution_path"] for item in history.json()[:3]] == [
            "cache",
            "lean",
            "local",
        ]
        detail = client.get(
            f"/api/spaces/{space_id}/runs/{cached_run.json()['run_id']}",
            headers=auth(token),
        )
        assert detail.status_code == 200
        assert detail.json()["cached_from_run_id"] == model_run.json()["run_id"]

        expensive = client.post(
            "/api/spaces",
            headers=auth(token),
            json={
                "name": "Hard Budget Gate",
                "template_id": "blank",
                "system_prompt": "规则" * 2_000,
                "monthly_token_budget": 1_000,
            },
        )
        assert expensive.status_code == 201
        expensive_id = expensive.json()["id"]
        blocked_preview = client.post(
            f"/api/spaces/{expensive_id}/preflight",
            headers=auth(token),
            json={"message": "Run this task.", "mode": "lean"},
        )
        assert blocked_preview.status_code == 200
        assert blocked_preview.json()["allowed"] is False
        blocked_run = client.post(
            f"/api/spaces/{expensive_id}/runs",
            headers=auth(token),
            json={"message": "Run this task.", "mode": "lean"},
        )
        assert blocked_run.status_code == 429
        assert len(model_calls) == 1

        other_token, _ = register(client, "space-v2-visitor")
        hidden_history = client.get(
            f"/api/spaces/{space_id}/runs",
            headers=auth(other_token),
        )
        assert hidden_history.status_code == 404


def test_recruitment_profile_matching_and_deadline_metadata():
    with TestClient(main.app) as client:
        bootstrap_job = next(
            job for job in main.CURATED_CAMPUS_JOBS
            if "BytePlus" in job["company"]
        )
        database.upsert_recruitment_jobs([bootstrap_job])
        token, _ = register(client, "recruiter")
        profile = client.put(
            "/api/recruitment/profile",
            headers=auth(token),
            json={
                "desired_roles": ["产品经理"],
                "industries": ["互联网"],
                "employer_types": ["互联网企业"],
            },
        )
        assert profile.status_code == 200
        assert set(profile.json()) == {"desired_roles", "industries", "locations", "employer_types"}
        jobs = client.get("/api/recruitment/jobs", headers=auth(token))
        assert jobs.status_code == 200
        payload = jobs.json()
        assert payload["data_status"]["mode"] == "hybrid_live"
        assert set(payload["data_status"]["tier_counts"]) == {
            "T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3",
        }
        assert len(payload["monitor_pools"]) == 10
        assert any(pool["id"] == "tobacco_monopoly" for pool in payload["monitor_pools"])
        assert any(pool["id"] == "professional_services" for pool in payload["monitor_pools"])
        assert payload["jobs"]
        assert all("employer_categories" in job for job in payload["jobs"])
        titles = {job["title"] for job in payload["jobs"]}
        assert "Strategy Manager Graduate（BytePlus）– 2027 Start" in titles
        assert "拼多多 2027届校园招聘提前批" not in titles
        assert "2027 Business Analyst (General Practice)_Campus" not in titles
        assert not any("梧桐计划" in title for title in titles)
        assert all(job["url"].startswith("https://") for job in payload["jobs"])
        assert not any(job["id"].startswith("sample-") for job in payload["jobs"])
        first = payload["jobs"][0]
        assert "match_score" in first
        assert "estimated_rate" not in first
        assert "historical_rate" not in first
        assert "tier_label" not in first
        assert "days_left" in first
        assert first["tier_code"] in {"T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3"}
        assert "composite_fit" not in first


def test_recruitment_ingest_accepts_live_campus_jobs_and_rejects_expired_jobs(monkeypatch):
    live_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘数据岗"
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=(
                f"测试重点机构 {live_title} 校园招聘 "
                "应届毕业生 立即申请 投递截止：2099年8月25日"
            )
        ),
    )
    payload = {
        "jobs": [
            {
                "company": "测试重点机构",
                "title": live_title,
                "city": "北京",
                "official_url": f"{TEST_AUTHORIZED_ATS}/campus/data-current",
                "closing_date": "2099-08-25",
                "tags": ["校园招聘", "数据"],
            },
            {
                "company": "过期机构",
                "title": "2020届校园招聘岗位",
                "city": "上海",
                "official_url": "https://example.com/campus/expired",
                "closing_date": "2020-08-01",
            },
            {
                "company": "社会招聘机构",
                "title": "高级销售经理",
                "city": "北京",
                "official_url": "https://example.com/jobs/sales-manager",
                "tags": ["社会招聘"],
            },
        ]
    }
    with TestClient(main.app) as client:
        unauthorized = client.post("/api/recruitment/ingest", json=payload)
        assert unauthorized.status_code == 401
        result = client.post(
            "/api/recruitment/ingest",
            headers={"X-Recruitment-Token": "test-recruitment-ingest-token"},
            json=payload,
        )
        assert result.status_code == 200
        assert result.json()["accepted"] == 1
        assert result.json()["skipped"] == [
            {"title": "2020届校园招聘岗位", "reason": "expired"},
            {"title": "高级销售经理", "reason": "not_campus"},
        ]
        token, _ = register(client, "dynamic-recruiter")
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        assert any(job["title"] == live_title for job in jobs)
        assert not any(job["title"] == "2020届校园招聘岗位" for job in jobs)
        closed = client.post(
            "/api/recruitment/ingest",
            headers={"X-Recruitment-Token": "test-recruitment-ingest-token"},
            json={"jobs": [{**payload["jobs"][0], "status": "closed"}]},
        )
        assert closed.status_code == 200
        assert closed.json()["skipped"] == [
            {"title": live_title, "reason": "closed"}
        ]
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        assert not any(job["title"] == live_title for job in jobs)


def test_recruitment_ingest_is_idempotent_allows_shared_pages_and_hides_thread_ids(monkeypatch):
    raw_thread_id = "private-chat-thread-must-not-leak"
    data_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘数据分析岗"
    product_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘产品策略岗"
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=(
                f"桥接测试集团 {data_title} "
                f"桥接测试集团 {product_title} "
                "校园招聘 campus 立即申请"
            )
        ),
    )
    jobs_payload = [
        {
            "company": "桥接测试集团",
            "title": data_title,
            "city": "北京",
            "official_url": (
                f"{TEST_AUTHORIZED_ATS}/campus/shared?jobList=1&utm_source=chatgpt#data"
            ),
            "closing_date": "2099-09-01",
            "source_id": "chatgpt-radar-01",
            "source_thread_id": raw_thread_id,
            "source_item_id": "item-data",
            "evidence": ["官方页面出现岗位标题和校招标志"],
        },
        {
            "company": "桥接测试集团",
            "title": product_title,
            "city": "北京",
            "official_url": (
                f"{TEST_AUTHORIZED_ATS}/campus/shared?utm_medium=monitor&jobList=1#product"
            ),
            "closing_date": "2099-09-01",
            "source_id": "chatgpt-radar-01",
            "source_thread_id": raw_thread_id,
            "source_item_id": "item-product",
            "evidence": ["官方页面出现岗位标题和校招标志"],
        },
    ]
    ingest_headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    with TestClient(main.app) as client:
        first = client.post(
            "/api/recruitment/ingest",
            headers=ingest_headers,
            json={"jobs": jobs_payload},
        )
        assert first.status_code == 200
        assert first.json()["accepted"] == 2
        assert first.json()["new"] == 2
        assert first.json()["duplicates"] == 0

        repeated = client.post(
            "/api/recruitment/ingest",
            headers=ingest_headers,
            json={"jobs": jobs_payload},
        )
        assert repeated.status_code == 200
        assert repeated.json()["accepted"] == 2
        assert repeated.json()["duplicates"] == 2

        token, _ = register(client, "bridge-idempotency-user")
        jobs_response = client.get(
            "/api/recruitment/jobs", headers=auth(token)
        ).json()
        bridged = [
            job for job in jobs_response["jobs"]
            if job["company"] == "桥接测试集团"
        ]
        assert {job["title"] for job in bridged} == {
            data_title,
            product_title,
        }
        with database.connect() as connection:
            canonical_urls = {
                row[0]
                for row in connection.execute(
                    "SELECT canonical_url FROM recruitment_ingest_candidates "
                    "WHERE company = ?",
                    ("桥接测试集团",),
                )
            }
        assert all("utm_" not in url and "#" not in url for url in canonical_urls)
        assert jobs_response["data_status"]["chatgpt_sync"]["connected_source_count"] >= 1
        assert "sources" not in jobs_response["data_status"]["chatgpt_sync"]

        assert client.get("/api/recruitment/sync/status").status_code == 401
        sync = client.get(
            "/api/recruitment/sync/status", headers=ingest_headers
        )
        assert sync.status_code == 200
        sync_payload = sync.json()
        assert sync_payload["expected_source_count"] == 6
        source = next(
            item for item in sync_payload["sources"]
            if item["source_id"] == "chatgpt-radar-01"
        )
        assert source["status"] == "synced"
        assert source["source_ref"] is None
        assert raw_thread_id not in sync.text
        assert "source_thread_id" not in sync.text


def test_repeated_ingest_rechecks_official_page_and_closes_removed_job(monkeypatch):
    title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘战略岗"
    page = {"text": f"复核集团 {title} 校园招聘 应届毕业生 立即申请"}
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(text=page["text"]),
    )
    job = {
        "company": "复核集团",
        "title": title,
        "city": "北京",
        "official_url": f"{TEST_AUTHORIZED_ATS}/campus/recheck-role",
        "source_id": "chatgpt-radar-03",
        "external_id": "recheck-role",
    }
    headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    with TestClient(main.app) as client:
        first = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [job]}
        )
        assert first.status_code == 200
        assert first.json()["accepted"] == 1

        page["text"] = "该职位已下线，申请通道已关闭。"
        repeated = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [job]}
        )
        assert repeated.status_code == 200
        assert repeated.json()["duplicates"] == 1
        assert repeated.json()["closed"] == 1

        token, _ = register(client, "recheck-closed-user")
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        assert not any(item["title"] == job["title"] for item in jobs)


def test_recruitment_ingest_quarantines_unverifiable_and_rejects_noncampus_pages(monkeypatch):
    pending_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘研究岗"
    noncampus_title = f"{CURRENT_RECRUITMENT_COHORT}届审计岗"

    def fake_fetch(url, *_args, **_kwargs):
        if url.endswith("/unreachable"):
            raise recruitment_watch.WatchFetchError("temporary failure")
        return SimpleNamespace(
            text=f"核验失败集团 {noncampus_title} 仅限社会招聘"
        )

    monkeypatch.setattr(main, "fetch_watch_page", fake_fetch)
    payload = {
        "jobs": [
            {
                "company": "待核验集团",
                "title": pending_title,
                "city": "上海",
                "official_url": f"{TEST_AUTHORIZED_ATS}/unreachable",
                "source_id": "chatgpt-radar-02",
                "external_id": "pending-role",
            },
            {
                "company": "核验失败集团",
                "title": noncampus_title,
                "city": "上海",
                "official_url": f"{TEST_AUTHORIZED_ATS}/no-campus-signal",
                "source_id": "chatgpt-radar-02",
                "external_id": "rejected-role",
                "tags": ["应届"],
            },
        ]
    }
    ingest_headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    with TestClient(main.app) as client:
        response = client.post(
            "/api/recruitment/ingest", headers=ingest_headers, json=payload
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 0
        assert response.json()["pending"] == 1
        assert response.json()["rejected"] == 1
        assert {item["reason"] for item in response.json()["skipped"]} == {
            "official_page_fetch_failed",
            "official_page_non_campus",
        }
        token, _ = register(client, "bridge-quarantine-user")
        titles = {
            job["title"]
            for job in client.get(
                "/api/recruitment/jobs", headers=auth(token)
            ).json()["jobs"]
        }
        assert pending_title not in titles
        assert noncampus_title not in titles


def test_recruitment_ingest_requires_complete_official_page_evidence(monkeypatch):
    current_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘数据分析岗"
    generic_title = "校园招聘战略分析岗"

    def fake_fetch(url, *_args, **_kwargs):
        if url.endswith("/unknown-domain"):
            text = f"未知域名集团 {current_title} 校园招聘 立即申请"
        elif url.endswith("/missing-open"):
            text = f"静态公告集团 {current_title} 校园招聘"
        elif url.endswith("/wrong-cohort"):
            text = f"届别错误集团 2099届 {generic_title} 立即申请"
        else:
            text = (
                f"正式证据集团 {current_title} 校园招聘 立即申请 "
                "发布日期：2099年9月10日"
            )
        return SimpleNamespace(text=text, final_url=url)

    monkeypatch.setattr(main, "fetch_watch_page", fake_fetch)
    jobs = [
        {
            "company": "未知域名集团",
            "title": current_title,
            "city": "北京",
            "official_url": "https://example.com/unknown-domain",
            "source_id": "chatgpt-radar-01",
            "external_id": "unknown-domain-evidence",
        },
        {
            "company": "静态公告集团",
            "title": current_title,
            "city": "北京",
            "official_url": f"{TEST_AUTHORIZED_ATS}/missing-open",
            "source_id": "chatgpt-radar-02",
            "external_id": "missing-open-evidence",
        },
        {
            "company": "届别错误集团",
            "title": generic_title,
            "city": "北京",
            "official_url": f"{TEST_AUTHORIZED_ATS}/wrong-cohort",
            "source_id": "chatgpt-radar-03",
            "external_id": "wrong-cohort-evidence",
        },
        {
            "company": "正式证据集团",
            "title": current_title,
            "city": "北京",
            "official_url": f"{TEST_AUTHORIZED_ATS}/complete-evidence",
            "opening_date": "2099-09-10",
            "closing_date": "2099-09-10",
            "source_id": "chatgpt-radar-04",
            "external_id": "complete-evidence",
        },
    ]
    headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    with TestClient(main.app) as client:
        response = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": jobs}
        )

        assert response.status_code == 200
        assert response.json()["accepted"] == 1
        assert response.json()["pending"] == 3
        assert {item["reason"] for item in response.json()["skipped"]} == {
            "page_missing_official_domain_evidence",
            "page_missing_open_application_evidence",
            "page_missing_current_cohort_evidence",
        }
        with database.connect() as connection:
            verified_dates = connection.execute(
                "SELECT verified_opening_date, verified_closing_date "
                "FROM recruitment_ingest_candidates WHERE external_id = ?",
                ("complete-evidence",),
            ).fetchone()
        assert tuple(verified_dates) == (None, None)


def test_recruitment_ingest_ignores_stale_source_updates(monkeypatch):
    title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘战略岗"
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=f"时序测试集团 {title} 校园招聘 立即申请"
        ),
    )
    headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    current = {
        "company": "时序测试集团",
        "title": title,
        "city": "深圳",
        "official_url": f"{TEST_AUTHORIZED_ATS}/campus/timeline",
        "source_id": "chatgpt-radar-03",
        "external_id": "timeline-role",
        "source_updated_at": "2099-08-20T10:00:00Z",
    }
    stale = {
        **current,
        "title": "过时且不应覆盖的岗位标题",
        "source_updated_at": "2099-08-19T10:00:00Z",
    }
    with TestClient(main.app) as client:
        first = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [current]}
        )
        assert first.status_code == 200
        assert first.json()["accepted"] == 1
        second = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [stale]}
        )
        assert second.status_code == 200
        assert second.json()["duplicates"] == 1
        assert second.json()["stale"] == 1
        assert second.json()["accepted"] == 1
        assert second.json()["skipped"] == [
            {"title": "过时且不应覆盖的岗位标题", "reason": "stale_source_update"}
        ]


def test_recruitment_ingest_requires_strict_title_and_preserves_last_known_good(monkeypatch):
    initial_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘量化研究岗"
    changed_title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘产品经理岗"
    page = {
        "text": (
            f"存量保护集团 {initial_title} 校园招聘 "
            "截止日期 2099年10月31日"
        )
    }
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(text=page["text"]),
    )
    headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    initial = {
        "company": "存量保护集团",
        "title": initial_title,
        "city": "北京",
        "official_url": f"{TEST_AUTHORIZED_ATS}/campus/last-known-good",
        "closing_date": "2099-10-31",
        "source_id": "chatgpt-radar-04",
        "external_id": "last-known-good-role",
        "source_updated_at": "2099-08-20T10:00:00Z",
    }
    changed = {
        **initial,
        "title": changed_title,
        "source_updated_at": "2099-08-21T10:00:00Z",
    }
    with TestClient(main.app) as client:
        accepted = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [initial]}
        )
        assert accepted.status_code == 200
        assert accepted.json()["accepted"] == 1

        page["text"] = f"存量保护集团 {CURRENT_RECRUITMENT_COHORT}校园招聘 校园招聘"
        pending = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [changed]}
        )
        assert pending.status_code == 200
        assert pending.json()["pending"] == 1
        assert pending.json()["skipped"] == [
            {
                "title": changed_title,
                "reason": "page_missing_title_evidence",
            }
        ]

        token, _ = register(client, "last-known-good-user")
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        protected = [job for job in jobs if job["company"] == "存量保护集团"]
        assert len(protected) == 1
        assert protected[0]["title"] == initial_title
        assert protected[0]["closing_date"] == "2099-10-31"

        page["text"] = f"存量保护集团 {changed_title} 校园招聘 立即申请"
        reverified = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [changed]}
        )
        assert reverified.status_code == 200
        assert reverified.json()["duplicates"] == 1
        assert reverified.json()["accepted"] == 1
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        promoted = [job for job in jobs if job["company"] == "存量保护集团"]
        assert len(promoted) == 1
        assert promoted[0]["title"] == changed_title
        assert promoted[0]["closing_date"] is None

        page["text"] = (
            f"存量保护集团 {changed_title} 校园招聘，申请已结束"
        )
        closed = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={"jobs": [{**changed, "requirements": "触发官网状态复核"}]},
        )
        assert closed.status_code == 200
        assert closed.json()["closed"] == 1
        assert closed.json()["skipped"] == [
            {
                "title": changed_title,
                "reason": "official_page_closed",
            }
        ]
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        assert not any(job["company"] == "存量保护集团" for job in jobs)


def test_recruitment_ingest_heartbeat_uses_latest_event_and_monotonic_timestamp(monkeypatch):
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            recruitment_watch.WatchFetchError("temporary failure")
        ),
    )
    headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    candidate = {
        "company": "心跳状态集团",
        "title": "2099届校园招聘研究岗",
        "city": "上海",
        "official_url": "https://example.com/campus/heartbeat",
        "source_id": "chatgpt-radar-05",
        "external_id": "heartbeat-pending-role",
        "source_updated_at": "2099-08-20T10:00:00Z",
    }
    with TestClient(main.app) as client:
        pending = client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": [candidate]}
        )
        assert pending.status_code == 200
        assert pending.json()["pending"] == 1

        heartbeat = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={
                "jobs": [],
                "source_id": "chatgpt-radar-05",
                "source_updated_at": "2099-08-22T10:00:00Z",
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["received"] == 0
        status = client.get("/api/recruitment/sync/status", headers=headers).json()
        source = next(
            item for item in status["sources"]
            if item["source_id"] == "chatgpt-radar-05"
        )
        assert source["status"] == "synced"
        assert source["latest_pending"] == 0
        assert source["inventory_pending"] >= 1
        assert source["last_source_updated_at"] == "2099-08-22T10:00:00+00:00"

        older_heartbeat = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={
                "jobs": [],
                "source_id": "chatgpt-radar-05",
                "source_updated_at": "2099-08-21T10:00:00Z",
            },
        )
        assert older_heartbeat.status_code == 200
        status = client.get("/api/recruitment/sync/status", headers=headers).json()
        source = next(
            item for item in status["sources"]
            if item["source_id"] == "chatgpt-radar-05"
        )
        assert source["last_source_updated_at"] == "2099-08-22T10:00:00+00:00"

        assert client.post(
            "/api/recruitment/ingest", headers=headers, json={"jobs": []}
        ).status_code == 422


def test_recruitment_ingest_schema_canonicalization_and_cross_source_merge(monkeypatch):
    title = f"{CURRENT_RECRUITMENT_COHORT}校园招聘战略分析岗"
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=f"跨源合并集团 {title} 校园招聘 立即申请"
        ),
    )
    canonical = main.canonicalize_recruitment_url(
        "https://EXAMPLE.com/campus?ref=partner&campaign=autumn&utm_source=gpt&gclid=x#role"
    )
    assert "ref=partner" in canonical
    assert "campaign=autumn" in canonical
    assert "utm_source" not in canonical
    assert "gclid" not in canonical
    assert "#" not in canonical

    headers = {"X-Recruitment-Token": "test-recruitment-ingest-token"}
    common = {
        "company": "跨源合并集团",
        "title": title,
        "city": "深圳",
        "official_url": f"{TEST_AUTHORIZED_ATS}/campus/cross-source",
        "external_id": "shared-vacancy-42",
    }
    with TestClient(main.app) as client:
        first = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={"jobs": [{**common, "source_id": "chatgpt-radar-04"}]},
        )
        second = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={"jobs": [{**common, "source_id": "chatgpt-radar-05"}]},
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["accepted"] == second.json()["accepted"] == 1
        with database.connect() as connection:
            formal_count = connection.execute(
                "SELECT COUNT(*) FROM recruitment_jobs WHERE company = ?",
                ("跨源合并集团",),
            ).fetchone()[0]
            candidate_count = connection.execute(
                "SELECT COUNT(*) FROM recruitment_ingest_candidates WHERE company = ?",
                ("跨源合并集团",),
            ).fetchone()[0]
        assert formal_count == 1
        assert candidate_count == 2

        private_evidence = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={
                "jobs": [{
                    **common,
                    "source_id": "chatgpt-radar-01",
                    "evidence": ["请联系 recruiter@example.com 核验"],
                }]
            },
        )
        assert private_evidence.status_code == 422
        extra_field = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={"jobs": [{**common, "unknown_field": "not allowed"}]},
        )
        assert extra_field.status_code == 422
        too_many = client.post(
            "/api/recruitment/ingest",
            headers=headers,
            json={"jobs": [common] * 11},
        )
        assert too_many.status_code == 422


def test_recruitment_deep_search_is_explicit_and_can_repeat_after_completion(monkeypatch):
    calls = []
    state = {"value": None}

    def fake_refresh(*, include_web_search=True, force_web_search=False):
        calls.append((include_web_search, force_web_search))
        if include_web_search:
            state["value"] = {
                "status": "success",
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "jobs": 3,
            }
        return 3

    with TestClient(main.app) as client:
        token, _ = register(client, "deep-search-user")
        monkeypatch.setattr(
            main,
            "settings",
            SimpleNamespace(recruitment_web_search_enabled=True),
        )
        monkeypatch.setattr(main, "refresh_recruitment_sources", fake_refresh)
        monkeypatch.setattr(
            database,
            "get_system_state",
            lambda _key: state["value"],
        )
        monkeypatch.setattr(main, "_recruitment_source_last_refresh", 0.0)

        deep = client.post(
            "/api/recruitment/refresh?deep_search=true", headers=auth(token)
        )
        assert deep.status_code == 200
        assert deep.json()["web_search_ran"] is True
        assert deep.json()["skip_reason"] is None
        assert deep.json()["next_due_at"] is None
        assert calls[-1] == (True, True)

        repeated = client.post(
            "/api/recruitment/refresh?deep_search=true", headers=auth(token)
        )
        assert repeated.status_code == 200
        assert repeated.json()["web_search_ran"] is True
        assert repeated.json()["skip_reason"] is None
        assert calls[-1] == (True, True)


def test_recruitment_watch_fetch_is_offline_deterministic_and_safe(monkeypatch):
    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"
            self.headers["Content-Length"] = "140"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def geturl(self):
            return "https://careers.example.com/campus"

        def read(self, _limit):
            return (
                b"<html><style>hidden</style><body>2027 Campus Recruitment "
                b"<script>ignored()</script>Data Analyst</body></html>"
            )

    class FakeOpener:
        def open(self, _request, timeout):
            assert timeout == recruitment_watch.DEFAULT_TIMEOUT_SECONDS
            return FakeResponse()

    monkeypatch.setattr(
        recruitment_watch,
        "_resolved_addresses",
        lambda _hostname: {recruitment_watch.ipaddress.ip_address("93.184.216.34")},
    )
    result = fetch_watch_page(
        "https://careers.example.com/campus#jobs",
        ["Campus Recruitment", "Data Analyst", "ignored"],
        opener_factory=lambda *_handlers: FakeOpener(),
    )
    assert result.keyword_hits == ["Campus Recruitment", "Data Analyst"]
    assert len(result.fingerprint) == 64
    assert result.url == "https://careers.example.com/campus"

    class OversizedOpener:
        def open(self, _request, timeout):
            del timeout
            response = FakeResponse()
            response.headers.replace_header("Content-Length", "2000000")
            return response

    try:
        fetch_watch_page(
            "https://careers.example.com/campus",
            ["校园招聘"],
            max_bytes=1024,
            opener_factory=lambda *_handlers: OversizedOpener(),
        )
    except recruitment_watch.WatchFetchError:
        pass
    else:
        raise AssertionError("Oversized watch page was accepted")

    for unsafe_url in (
        "http://example.com/jobs",
        "https://127.0.0.1/jobs",
        "https://localhost/jobs",
        "https://10.0.0.8/jobs",
    ):
        try:
            recruitment_watch.validate_public_https_url(unsafe_url, resolve_dns=False)
        except recruitment_watch.WatchFetchError:
            pass
        else:
            raise AssertionError(f"Unsafe watch URL was accepted: {unsafe_url}")

    monkeypatch.setattr(
        recruitment_watch,
        "_resolved_addresses",
        lambda _hostname: {recruitment_watch.ipaddress.ip_address("127.0.0.1")},
    )
    try:
        recruitment_watch.validate_public_https_url(
            "https://public-name.example/jobs",
            resolve_dns=True,
        )
    except recruitment_watch.WatchFetchError:
        pass
    else:
        raise AssertionError("Hostname resolving to a private address was accepted")


def test_recruitment_watch_crud_refresh_and_real_status(monkeypatch):
    fingerprint = {"value": "a" * 64}
    monkeypatch.setattr(main, "WATCH_REFRESH_COOLDOWN_SECONDS", 0)

    def fake_fetch(url, keywords):
        return WatchFetchResult(
            url=url,
            final_url=url,
            fingerprint=fingerprint["value"],
            keyword_hits=[keywords[0]],
            content_bytes=128,
            http_status=200,
        )

    monkeypatch.setattr(main, "fetch_watch_page", fake_fetch)
    with TestClient(main.app) as client:
        token, _ = register(client, "watch-owner")
        created = client.post(
            "/api/recruitment/watches",
            headers=auth(token),
            json={
                "name": "目标企业校招页",
                "url": "https://careers.example.com/campus",
                "keywords": ["校园招聘", "数据"],
            },
        )
        assert created.status_code == 201
        watch_id = created.json()["id"]
        assert created.json()["last_status"] == "baseline"

        first = client.post("/api/recruitment/watches/refresh", headers=auth(token))
        assert first.status_code == 200
        assert first.json()["counts"]["unchanged"] == 1
        assert first.json()["model_tokens_used"] == 0

        fingerprint["value"] = "b" * 64
        second = client.post("/api/recruitment/watches/refresh", headers=auth(token))
        assert second.status_code == 200
        assert second.json()["counts"]["changed"] == 1

        # An unchanged follow-up scan must keep the change visible until the
        # user explicitly acknowledges it.
        third = client.post("/api/recruitment/watches/refresh", headers=auth(token))
        assert third.status_code == 200
        assert third.json()["counts"]["unchanged"] == 1
        listed = client.get("/api/recruitment/watches", headers=auth(token)).json()
        assert listed["summary"]["changed"] == 1
        assert listed["watches"][0]["change_pending"] is True
        assert listed["watches"][0]["last_keyword_hits"] == ["校园招聘"]

        observed_version = listed["watches"][0]["change_version"]
        fingerprint["value"] = "c" * 64
        newest = client.post("/api/recruitment/watches/refresh", headers=auth(token))
        assert newest.status_code == 200
        stale_acknowledgement = client.post(
            f"/api/recruitment/watches/{watch_id}/acknowledge",
            headers=auth(token),
            json={"change_version": observed_version},
        )
        assert stale_acknowledgement.status_code == 409
        latest_watch = client.get(
            "/api/recruitment/watches",
            headers=auth(token),
        ).json()["watches"][0]

        acknowledged = client.post(
            f"/api/recruitment/watches/{watch_id}/acknowledge",
            headers=auth(token),
            json={"change_version": latest_watch["change_version"]},
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["change_pending"] is False
        assert client.get(
            "/api/recruitment/watches",
            headers=auth(token),
        ).json()["summary"]["changed"] == 0

        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()
        assert jobs["data_status"]["watches"]["total"] == 1
        assert jobs["data_status"]["last_sync"] is not None
        assert jobs["data_status"]["model_tokens_used"] == 0

        deleted = client.delete(
            f"/api/recruitment/watches/{watch_id}",
            headers=auth(token),
        )
        assert deleted.status_code == 204
        assert client.get("/api/recruitment/watches", headers=auth(token)).json()["watches"] == []


def test_recruitment_watch_preserves_fragment_links_and_enforces_cap(monkeypatch):
    fetched_urls: list[str] = []

    def fake_fetch(url, keywords):
        fetched_urls.append(url)
        return WatchFetchResult(
            url=url,
            final_url=url,
            fingerprint="d" * 64,
            keyword_hits=list(keywords[:1]),
            content_bytes=64,
            http_status=200,
        )

    monkeypatch.setattr(main, "fetch_watch_page", fake_fetch)
    monkeypatch.setattr(main, "WATCH_CREATE_LIMIT", 100)
    with TestClient(main.app) as client:
        token, _ = register(client, "fragment-watch-owner")
        for index in range(12):
            created = client.post(
                "/api/recruitment/watches",
                headers=auth(token),
                json={
                    "name": f"目标企业岗位 {index}",
                    "url": f"https://app.mokahr.com/campus_apply/example#/job/{index}",
                    "keywords": [f"岗位 {index}"],
                },
            )
            assert created.status_code == 201
            assert created.json()["url"].endswith(f"#/job/{index}")
        assert all("#" not in url for url in fetched_urls)

        over_cap = client.post(
            "/api/recruitment/watches",
            headers=auth(token),
            json={
                "name": "第十三条",
                "url": "https://example.com/campus#/job/13",
                "keywords": ["校园招聘"],
            },
        )
        assert over_cap.status_code == 409


def test_company_only_watch_uses_recruitment_pool_without_url_or_keywords():
    with TestClient(main.app) as client:
        bootstrap_job = next(
            job for job in main.CURATED_CAMPUS_JOBS
            if "BytePlus" in job["company"]
        )
        database.upsert_recruitment_jobs([bootstrap_job])
        token, _ = register(client, "company-watch-owner")
        created = client.post(
            "/api/recruitment/watches",
            headers=auth(token),
            json={"company_name": "BytePlus"},
        )
        assert created.status_code == 201
        item = created.json()
        assert item["watch_type"] == "company"
        assert item["company_name"] == "BytePlus"
        assert item["url"] == ""
        assert item["last_status"] == "baseline"
        assert "Strategy Manager Graduate" in item["last_keyword_hits"][0]


def test_recruitment_jobs_hide_today_deadline_and_keep_tomorrow(monkeypatch):
    today = date.today()
    base_job = {
        "company": "测试机构",
        "employer_type": "互联网企业",
        "city": "北京",
        "industry": "互联网",
        "url": "https://example.com/campus",
        "source": "动态监控 API",
        "opening_date": None,
        "requirements": "2027届校园招聘",
        "tags": ["校园招聘", "动态监控", "链接已验证", "标题已验证"],
        "historical_applicants": None,
        "historical_offers": None,
        "last_verified_at": "2099-01-01T00:00:00+00:00",
        "status": "open",
    }
    monkeypatch.setattr(
        database,
        "list_recruitment_jobs",
        lambda: [
            {
                **base_job,
                "id": "tomorrow",
                "title": "2027届校园招聘数据岗 B",
                "closing_date": (today + timedelta(days=1)).isoformat(),
            },
            {
                **base_job,
                "id": "today",
                "title": "2027届校园招聘数据岗 A",
                "closing_date": today.isoformat(),
            },
        ],
    )
    with TestClient(main.app) as client:
        token, _ = register(client, "deadline-sort-owner")
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        assert [job["id"] for job in jobs] == ["tomorrow"]


def test_company_search_batches_cover_every_left_list_entry_once():
    targets = recruitment_search.build_employer_search_targets()
    batches = recruitment_search.build_employer_search_batches()
    raw_entries = [
        (pool["id"], employer)
        for pool in recruitment_search.PERSONAL_MONITOR_POOLS
        for employer in pool["employers"]
    ]

    assigned_ids = [target.id for batch in batches for target in batch.targets]
    assert len(assigned_ids) == len(set(assigned_ids)) == len(targets)
    assert set(assigned_ids) == {target.id for target in targets}
    assert len(batches) == len(targets)
    assert all(len(batch.targets) == 1 for batch in batches)
    assert all(
        len(batch.targets) == 1
        for batch in recruitment_search.build_employer_search_batches(batch_size=100)
    )
    for pool_id, employer in raw_entries:
        assert sum(
            target.pool_id == pool_id and employer in target.aliases
            for target in targets
        ) == 1

    # Obvious aliases collapse, while similarly named legal entities do not.
    assert len(targets) < len(raw_entries)
    assert any(
        {"大疆", "DJI"}.issubset(set(target.aliases)) for target in targets
    )
    assert {"中国电子", "中国电子科技集团"}.issubset(
        {target.canonical_name for target in targets}
    )


def test_company_search_prompt_explicitly_targets_the_graduating_cohort(monkeypatch):
    class AugustDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 30)

    monkeypatch.setattr(recruitment_search, "date", AugustDate)
    batch = recruitment_search.build_employer_search_batches()[0]
    prompt = recruitment_search._search_prompt(batch)
    assert "目标毕业届别是 2027 届" in prompt
    assert "title 保留原公告中的具体岗位名称" in prompt
    assert "requirements 中保留原文证实的适用毕业届别" in prompt


def test_company_search_batch_rejects_incomplete_coverage_report():
    pool = employer_only_pool(
        next(
            item for item in recruitment_search.PERSONAL_MONITOR_POOLS
            if item["primary_category"] == "internet_tech"
        ),
        "百度",
    )
    batch = recruitment_search.build_employer_search_batches([pool])[0]
    incomplete_payload = {
        "checked_employers": [],
        "jobs": [],
    }

    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                output_text=__import__("json").dumps(incomplete_payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[]),
                )],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            )

    try:
        recruitment_search._search_batch(
            SimpleNamespace(responses=FakeResponses()), batch
        )
    except RuntimeError as exc:
        assert "incomplete coverage" in str(exc)
    else:
        raise AssertionError("An incomplete employer coverage report was accepted")


def test_company_search_rejects_multi_employer_self_report_before_request():
    pool = next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    )
    multi_target_batch = recruitment_search.EmployerSearchBatch(
        id="invalid-multi-employer",
        pool=pool,
        targets=recruitment_search.build_employer_search_targets([pool])[:8],
    )

    class NeverCalledResponses:
        def create(self, **_kwargs):
            raise AssertionError("A multi-employer request was sent")

    with pytest.raises(RuntimeError, match="exactly one target"):
        recruitment_search._search_batch(
            SimpleNamespace(responses=NeverCalledResponses()), multi_target_batch
        )


def test_company_search_tracks_failed_employer_not_whole_category(monkeypatch):
    pool = dict(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ))
    pool["employers"] = ["百度", "腾讯", "拼多多"]
    targets = recruitment_search.build_employer_search_targets([pool])
    failed_target = next(target for target in targets if target.canonical_name == "腾讯")
    requested_target_ids = []
    request_lock = Lock()

    class FakeResponses:
        def create(self, **kwargs):
            schema = kwargs["text"]["format"]["schema"]
            target_ids = schema["properties"]["jobs"]["items"]["properties"]["target_id"]["enum"]
            assert len(target_ids) == 1
            target_id = target_ids[0]
            with request_lock:
                requested_target_ids.append(target_id)
            assert kwargs["tool_choice"] == "required"
            assert kwargs["tools"][0]["type"] == "web_search"
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps({
                    "checked_employers": [{
                        "target_id": target_id, "status": "no_current_opening",
                    }],
                    "jobs": [],
                }),
                # A self-reported no-result row cannot stand in for actually
                # calling the search tool for this employer.
                output=[] if target_id == failed_target.id else [SimpleNamespace(
                    type="web_search_call", status="completed",
                    action=SimpleNamespace(sources=[]),
                )],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            )

    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", [pool])
    result = recruitment_search.search_current_recruitment_jobs(
        SimpleNamespace(responses=FakeResponses())
    )
    assert sorted(requested_target_ids) == sorted(target.id for target in targets)
    assert result.search_batches == result.target_count == 3
    assert set(result.searched_employers) == {"百度", "拼多多"}
    assert result.failed_employers == ("腾讯",)
    assert result.searched_count == result.tool_calls == 2
    assert result.coverage_percent == 66.67
    assert len(result.failed_batches) == 1


def test_company_search_bounds_independent_requests_to_eight_workers(monkeypatch):
    pool = dict(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ))
    pool["employers"] = pool["employers"][:12]
    release_requests = Event()
    request_lock = Lock()
    concurrency = {"active": 0, "peak": 0, "calls": 0}

    class FakeResponses:
        def create(self, **kwargs):
            target_ids = kwargs["text"]["format"]["schema"]["properties"]["jobs"]["items"]["properties"]["target_id"]["enum"]
            assert len(target_ids) == 1
            with request_lock:
                concurrency["calls"] += 1
                concurrency["active"] += 1
                concurrency["peak"] = max(concurrency["peak"], concurrency["active"])
                if concurrency["active"] == 8:
                    release_requests.set()
            assert release_requests.wait(timeout=5)
            with request_lock:
                concurrency["active"] -= 1
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps({
                    "checked_employers": [{
                        "target_id": target_ids[0], "status": "no_current_opening",
                    }],
                    "jobs": [],
                }),
                output=[SimpleNamespace(
                    type="web_search_call", status="completed",
                    action=SimpleNamespace(sources=[]),
                )],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            )

    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", [pool])
    result = recruitment_search.search_current_recruitment_jobs(
        SimpleNamespace(responses=FakeResponses())
    )
    assert concurrency["calls"] == result.target_count == 12
    assert concurrency["peak"] == 8
    assert result.searched_count == result.tool_calls == 12
    assert result.coverage_percent == 100


@pytest.mark.parametrize("status,incomplete_details", [
    ("incomplete", SimpleNamespace(reason="max_output_tokens")),
    ("completed", SimpleNamespace(reason="max_output_tokens")),
    ("failed", None),
])
def test_company_search_rejects_truncated_response_even_if_json_is_valid(
    status, incomplete_details,
):
    pool = employer_only_pool(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ), "百度")
    batch = recruitment_search.build_employer_search_batches([pool])[0]

    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                status=status, incomplete_details=incomplete_details,
                output_text=json.dumps(coverage_payload(pool, [])),
                output=[SimpleNamespace(
                    type="web_search_call", status="completed",
                    action=SimpleNamespace(sources=[]),
                )],
            )

    with pytest.raises(RuntimeError, match="incomplete response"):
        recruitment_search._search_batch(
            SimpleNamespace(responses=FakeResponses()), batch
        )


def test_company_search_keeps_all_discovered_rows_beyond_four(monkeypatch):
    pool = employer_only_pool(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ), "百度")
    rows = [{
        "company": "百度",
        "title": f"{CURRENT_RECRUITMENT_COHORT}校园招聘工程岗位 {index}",
        "city": "北京", "industry": "互联网",
        "official_url": f"https://talent.baidu.com/campus/jobs/{index}",
        "opening_date": None, "closing_date": None,
        "requirements": f"面向{CURRENT_RECRUITMENT_COHORT}毕业生",
        "category": "互联网企业",
    } for index in range(40)]

    class FakeResponses:
        def create(self, **kwargs):
            assert "maxItems" not in kwargs["text"]["format"]["schema"]["properties"]["jobs"]
            assert kwargs["max_output_tokens"] == recruitment_search.SEARCH_MAX_OUTPUT_TOKENS
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps(coverage_payload(pool, rows)),
                output=[SimpleNamespace(
                    type="web_search_call", status="completed",
                    action=SimpleNamespace(sources=[SimpleNamespace(
                        url="https://talent.baidu.com/campus"
                    )]),
                )],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            )

    monkeypatch.setattr(
        recruitment_search, "_inspect_official_candidate_page",
        lambda _job: recruitment_search.CandidatePageEvidence(False, False),
    )
    result = recruitment_search._search_pool(
        SimpleNamespace(responses=FakeResponses()), pool
    )
    assert len(result.jobs) == 40
    assert len({job["id"] for job in result.jobs}) == 40
    assert all("待官方核验" in job["tags"] for job in result.jobs)


@pytest.mark.parametrize("target_name,company,expected", [
    ("工商银行", "中国工商银行股份有限公司", True),
    ("农业银行", "中国农业银行", True),
    ("建设银行", "中国建设银行", True),
    ("邮储银行", "中国邮政储蓄银行", True),
    ("中国银行", "中国银行股份有限公司上海市分行", True),
    ("中国银行", "中国银行佛山分行", True),
    ("中国银行", "中国银行总行", True),
    ("中国移动", "中国移动通信集团广东有限公司", True),
    ("中国移动", "中国移动研究院", True),
    ("腾讯", "腾讯科技（深圳）有限公司", True),
    ("腾讯", "深圳市腾讯计算机系统有限公司", True),
    ("中国银行", "中国农业银行", False),
    ("中国移动", "中国联通广东分公司", False),
    ("中国电子", "中国电子科技集团", False),
    ("腾讯", "腾讯以外科技有限公司", False),
    ("腾讯", "腾讯培训机构", False),
    ("腾讯", "百度", False),
    ("安永", "Kearney", False),
    ("安永", "EYES technology", False),
])
def test_company_search_matches_known_legal_names_and_controlled_branches(
    target_name, company, expected,
):
    target = next(
        item for item in recruitment_search.build_employer_search_targets()
        if item.canonical_name == target_name
    )
    assert recruitment_search._company_matches_target(company, target) is expected


def test_company_search_does_not_truncate_accepted_updates_at_one_hundred(monkeypatch):
    def fake_search_batch(_client, batch):
        target_names = tuple(target.canonical_name for target in batch.targets)
        jobs = [{
            "id": f"{batch.id}-{index}",
            "company": batch.targets[0].canonical_name,
            "title": f"{CURRENT_RECRUITMENT_COHORT}校园招聘岗位 {batch.id}-{index}",
            "city": "全国",
            "url": f"https://example.com/{batch.id}/{index}",
            "tags": ["待官方核验"],
        } for index in range(4)]
        return recruitment_search.WebRecruitmentSearchResult(
            jobs=jobs,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            tool_calls=1,
            model="test-model",
            target_employers=target_names,
            searched_employers=target_names,
            employers_with_candidates=(batch.targets[0].canonical_name,),
            search_batches=1,
        )

    monkeypatch.setattr(recruitment_search, "_search_batch", fake_search_batch)
    result = recruitment_search.search_current_recruitment_jobs(SimpleNamespace())

    assert result.search_batches > 20
    assert len(result.jobs) == result.search_batches * 4
    assert len(result.jobs) > 100
    assert result.coverage_percent == 100.0
    assert result.failed_batches == ()


def test_bounded_web_search_normalizes_priority_jobs_and_rejects_noise(monkeypatch):
    raw_jobs = [
            {
                "company": "拼多多",
                "title": "2027届校园招聘产品策略岗",
                "city": "上海",
                "industry": "互联网",
                "official_url": "https://careers.pddglobalhr.com/campus/grad/product",
                "opening_date": "2026-08-20",
                "closing_date": "2099-09-01",
                "requirements": "面向2027届毕业生",
                "category": "互联网企业",
            },
            {
                "company": "未知小公司",
                "title": "2027届校园招聘",
                "city": "上海",
                "industry": "互联网",
                "official_url": "https://example.com/campus",
                "opening_date": None,
                "closing_date": None,
                "requirements": "面向2027届毕业生",
                "category": "互联网企业",
            },
            {
                "company": "腾讯",
                "title": "2027 campus graduate program",
                "city": "深圳",
                "industry": "互联网",
                "official_url": "https://www.linkedin.com/jobs/view/123",
                "opening_date": None,
                "closing_date": None,
                "requirements": "graduate role",
                "category": "互联网企业",
            },
        ]

    internet_pool = employer_only_pool(next(
        pool
        for pool in recruitment_search.PERSONAL_MONITOR_POOLS
        if pool["primary_category"] == "internet_tech"
    ), "拼多多")
    payload = coverage_payload(internet_pool, raw_jobs)

    class FakeResponses:
        def create(self, **kwargs):
            assert kwargs["tools"][0]["type"] == "web_search"
            assert kwargs["tool_choice"] == "required"
            assert kwargs["max_tool_calls"] == recruitment_search.settings.recruitment_web_search_max_tool_calls
            assert kwargs["parallel_tool_calls"] is True
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[SimpleNamespace(
                        url="https://careers.pddglobalhr.com/campus/grad/product"
                    )]),
                )],
                usage=SimpleNamespace(input_tokens=800, output_tokens=160, total_tokens=960),
                model="gpt-4o-mini",
            )

    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", [internet_pool])
    monkeypatch.setattr(
        recruitment_search,
        "_inspect_official_candidate_page",
        lambda _job: recruitment_search.CandidatePageEvidence(True, True),
    )
    result = recruitment_search.search_current_recruitment_jobs(
        SimpleNamespace(responses=FakeResponses())
    )
    assert len(result.jobs) == 1
    assert result.jobs[0]["company"] == "拼多多"
    assert result.jobs[0]["employer_type"] == "互联网企业"
    assert "动态监控" in result.jobs[0]["tags"]
    assert "链接已验证" in result.jobs[0]["tags"]
    assert "标题已验证" in result.jobs[0]["tags"]
    assert "待官方核验" not in result.jobs[0]["tags"]
    assert result.tool_calls == 1
    assert result.total_tokens == 960
    assert result.failed_pools == ()
    assert result.target_employers == ("拼多多",)
    assert result.searched_employers == ("拼多多",)
    assert result.employers_with_candidates == ("拼多多",)
    assert result.coverage_percent == 100.0


def test_candidate_page_rejects_title_match_on_unrelated_https_host(monkeypatch):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    title = f"{target_year}届校园招聘数据分析师"
    monkeypatch.setattr(
        recruitment_search,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=f"百度 {title} 立即申请",
            final_url="https://example.com/not-an-official-employer-page",
        ),
    )

    evidence = recruitment_search._inspect_official_candidate_page({
        "url": "https://example.com/not-an-official-employer-page",
        "company": "百度",
        "title": title,
        "closing_date": None,
    })

    assert evidence.readable is True
    assert evidence.employer_confirmed is True
    assert evidence.identity_confirmed is True
    assert evidence.cohort_confirmed is True
    assert evidence.open_confirmed is True
    assert evidence.domain_confirmed is False
    assert evidence.title_confirmed is False


def test_candidate_page_verifies_baidu_recruitment_host_and_all_evidence(monkeypatch):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    deadline = date.today() + timedelta(days=30)
    deadline_text = f"{deadline.year}年{deadline.month}月{deadline.day}日"
    title = f"{target_year}届校园招聘数据分析师"
    official_url = "https://talent.baidu.com/external/baidu/index.html#/job/123"
    monkeypatch.setattr(
        recruitment_search,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=f"百度 {title} 投递截止：{deadline_text}",
            final_url=official_url,
        ),
    )

    evidence = recruitment_search._inspect_official_candidate_page({
        "url": official_url,
        "company": "百度",
        "title": title,
        "closing_date": deadline.isoformat(),
    })

    assert recruitment_search._safe_official_url(official_url) is not None
    assert recruitment_search._safe_official_url(
        "https://www.baidu.com/s?wd=campus+recruitment"
    ) is None
    assert evidence.domain_confirmed is True
    assert evidence.employer_confirmed is True
    assert evidence.identity_confirmed is True
    assert evidence.cohort_confirmed is True
    assert evidence.open_confirmed is True
    assert evidence.closed is False
    assert evidence.title_confirmed is True


def test_candidate_page_requires_employer_identity_even_on_known_ats(monkeypatch):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    title = f"{target_year}届校园招聘数据分析师"
    ats_url = "https://app.mokahr.com/campus_apply/tenant/123"
    monkeypatch.setattr(
        recruitment_search,
        "fetch_watch_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=f"{title} 立即申请",
            final_url=ats_url,
        ),
    )

    evidence = recruitment_search._inspect_official_candidate_page({
        "url": ats_url,
        "company": "百度",
        "title": title,
        "closing_date": None,
    })

    assert evidence.domain_confirmed is True
    assert evidence.employer_confirmed is False
    assert evidence.title_confirmed is False


def test_candidate_page_uses_exact_maintained_brand_alias_without_parent_leakage():
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    title = f"{target_year}届校园招聘投资分析师"
    ats_url = "https://cicc.zhiye.com/campus/jobs/123"
    page = f"CICC {title} 上海 立即申请"

    evidence = recruitment_search._evaluate_official_candidate_page(
        {
            "url": ats_url,
            "company": "中国国际金融股份有限公司",
            "title": title,
        },
        page,
        ats_url,
    )
    branch_evidence = recruitment_search._evaluate_official_candidate_page(
        {
            "url": ats_url,
            "company": "中国国际金融股份有限公司上海分公司",
            "title": title,
        },
        page,
        ats_url,
    )

    assert evidence.domain_confirmed is True
    assert evidence.employer_confirmed is True
    assert evidence.title_confirmed is True
    assert branch_evidence.employer_confirmed is False
    assert branch_evidence.title_confirmed is False


def test_every_verified_registry_source_has_a_recognized_exact_host():
    from backend.future_radar.seeds import VERIFIED_OFFICIAL_SOURCES

    for source in VERIFIED_OFFICIAL_SOURCES:
        company = source.get("company", "")
        assert recruitment_search._official_domain_confirmed(
            company,
            source.get("url", ""),
            recruitment_search._maintained_employer_aliases(company),
        ), source["id"]


def test_recruitment_dates_require_application_semantics():
    application_date = "2099-09-10"

    assert recruitment_search._date_appears_in_page(
        "发布日期：2099年9月10日", application_date
    ) is False
    assert recruitment_search._semantic_date_appears_in_page(
        "投递截止日期：2099年9月10日",
        application_date,
        semantic="closing",
    ) is True
    assert recruitment_search._semantic_date_appears_in_page(
        "投递截止日期：2099年9月10日",
        application_date,
        semantic="opening",
    ) is False
    assert recruitment_search._semantic_date_appears_in_page(
        "开放申请日期：2099-09-10",
        application_date,
        semantic="opening",
    ) is True


def test_web_search_keeps_incomplete_attestation_pending(monkeypatch):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    internet_pool = employer_only_pool(next(
        pool for pool in recruitment_search.PERSONAL_MONITOR_POOLS
        if pool["primary_category"] == "internet_tech"
    ), "百度")
    payload = coverage_payload(internet_pool, [{
        "company": "百度",
        "title": f"{target_year}届校园招聘数据分析师",
        "city": "北京",
        "industry": "互联网",
        "official_url": "https://talent.baidu.com/external/baidu/index.html#/job/123",
        "opening_date": None,
        "closing_date": None,
        "requirements": f"面向{target_year}届毕业生",
        "category": "互联网企业",
    }])

    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[SimpleNamespace(
                        url="https://talent.baidu.com/external/baidu/index.html"
                    )]),
                )],
                usage=SimpleNamespace(input_tokens=8, output_tokens=2, total_tokens=10),
                model="test-model",
            )

    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", [internet_pool])
    monkeypatch.setattr(
        recruitment_search,
        "_inspect_official_candidate_page",
        lambda _job: recruitment_search.CandidatePageEvidence(
            readable=True,
            title_confirmed=False,
            page_text=f"百度 {target_year}届校园招聘",
            employer_confirmed=True,
            domain_confirmed=True,
            cohort_confirmed=True,
            open_confirmed=False,
            identity_confirmed=False,
        ),
    )

    result = recruitment_search.search_current_recruitment_jobs(
        SimpleNamespace(responses=FakeResponses())
    )

    assert len(result.jobs) == 1
    assert "待官方核验" in result.jobs[0]["tags"]
    assert "标题已验证" not in result.jobs[0]["tags"]
    assert "链接已验证" not in result.jobs[0]["tags"]


def test_web_search_rejects_pool_without_completed_tool_call():
    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                output_text='{"jobs": []}',
                output=[SimpleNamespace(type="message", status="completed")],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            )

    pool = employer_only_pool(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ), "百度")
    try:
        recruitment_search._search_pool(
            SimpleNamespace(responses=FakeResponses()), pool
        )
    except RuntimeError as exc:
        assert "completed web_search_call" in str(exc)
    else:
        raise AssertionError("A response without completed web search was accepted")


def test_web_search_keeps_uncited_structured_job_as_unverified_lead(monkeypatch):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    pool = employer_only_pool(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ), "百度")
    payload = coverage_payload(pool, [{
        "company": "百度",
        "title": f"{target_year}届校园招聘数据分析师",
        "city": "北京",
        "industry": "互联网",
        "official_url": "https://talent.baidu.com/external/baidu/index.html#/job/123",
        "opening_date": None,
        "closing_date": None,
        "requirements": f"面向{target_year}届毕业生",
        "category": "互联网企业",
    }])

    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[SimpleNamespace(
                        url="https://careers.pddglobalhr.com/campus"
                    )]),
                )],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                model="test-model",
            )

    monkeypatch.setattr(
        recruitment_search,
        "_inspect_official_candidate_page",
        lambda _job: (_ for _ in ()).throw(
            AssertionError("uncited job reached official-page inspection")
        ),
    )

    result = recruitment_search._search_pool(
        SimpleNamespace(responses=FakeResponses()), pool
    )

    assert len(result.jobs) == 1
    assert "搜索引用待确认" in result.jobs[0]["tags"]
    assert "待官方核验" in result.jobs[0]["tags"]
    assert "标题已验证" not in result.jobs[0]["tags"]
    assert result.jobs[0]["opening_date"] is None
    assert result.jobs[0]["closing_date"] is None
    assert result.tool_calls == 1


def test_web_search_marks_policy_bank_management_trainee_claims_for_review():
    today = date.today()
    target_year = today.year + 1 if today.month >= 6 else today.year
    base = {
        "city": "北京",
        "industry": "金融",
        "official_url": "https://example.com/campus/role",
        "opening_date": None,
        "closing_date": "2099-09-01",
        "requirements": "面向应届毕业生的校园招聘",
        "category": "银行/金融",
    }
    agriculture_job = recruitment_search._normalize_job({
        **base,
        "company": "中国农业发展银行",
        "title": f"{target_year}年管培生招聘",
    })
    central_bank_job = recruitment_search._normalize_job({
        **base,
        "company": "中国人民银行",
        "title": f"{target_year}年管理培训生招聘",
    })
    assert agriculture_job and "待官方核验" in agriculture_job["tags"]
    assert central_bank_job and "待官方核验" in central_bank_job["tags"]


def test_web_search_rejects_old_cohort_today_deadline_and_future_opening():
    today = date.today()
    target_year = today.year + 1 if today.month >= 6 else today.year
    base = {
        "company": "拼多多",
        "title": f"{target_year}届校园招聘产品岗",
        "city": "上海",
        "industry": "互联网",
        "official_url": "https://careers.pddglobalhr.com/campus/role",
        "opening_date": None,
        "closing_date": "2099-09-01",
        "requirements": f"面向{target_year}届毕业生",
        "category": "互联网企业",
    }
    assert recruitment_search._normalize_job({
        **base,
        "title": f"{target_year - 1}届校园招聘产品岗",
        "requirements": f"面向{target_year - 1}届毕业生",
    }) is None
    assert recruitment_search._normalize_job({
        **base,
        "closing_date": today.isoformat(),
    }) is None
    assert recruitment_search._normalize_job({
        **base,
        "opening_date": (today + timedelta(days=1)).isoformat(),
    }) is None


def test_web_search_keeps_unreadable_official_candidate_pending(monkeypatch):
    internet_pool = employer_only_pool(next(
        pool
        for pool in recruitment_search.PERSONAL_MONITOR_POOLS
        if pool["primary_category"] == "internet_tech"
    ), "拼多多")
    payload = coverage_payload(internet_pool, [{
            "company": "拼多多",
            "title": "2027届校园招聘产品策略岗",
            "city": "上海",
            "industry": "互联网",
            "official_url": "https://careers.pddglobalhr.com/campus/grad/product",
            "opening_date": "2026-08-20",
            "closing_date": "2099-09-01",
            "requirements": "面向2027届毕业生",
            "category": "互联网企业",
        }])

    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[SimpleNamespace(
                        url="https://careers.pddglobalhr.com/campus/grad/product"
                    )]),
                )],
                usage=SimpleNamespace(input_tokens=8, output_tokens=2, total_tokens=10),
                model="gpt-4o-mini",
            )

    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", [internet_pool])
    monkeypatch.setattr(
        recruitment_search,
        "_inspect_official_candidate_page",
        lambda _job: recruitment_search.CandidatePageEvidence(False, False),
    )
    result = recruitment_search.search_current_recruitment_jobs(
        SimpleNamespace(responses=FakeResponses())
    )
    assert len(result.jobs) == 1
    assert result.jobs[0]["opening_date"] is None
    assert result.jobs[0]["closing_date"] is None
    assert "官方页暂不可读" in result.jobs[0]["tags"]
    assert "待官方核验" in result.jobs[0]["tags"]
    assert "标题已验证" not in result.jobs[0]["tags"]


def test_web_search_keeps_successful_pools_when_one_pool_fails(monkeypatch):
    original_pools = recruitment_search.PERSONAL_MONITOR_POOLS[:2]
    pools = [
        employer_only_pool(pool, pool["employers"][0])
        for pool in original_pools
    ]
    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", pools)

    def fake_search_batch(_client, batch):
        if batch.pool["id"] == pools[0]["id"]:
            raise RuntimeError("temporary search failure")
        target_names = tuple(target.canonical_name for target in batch.targets)
        return recruitment_search.WebRecruitmentSearchResult(
            jobs=[],
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            tool_calls=1,
            model="gpt-4o-mini",
            target_employers=target_names,
            searched_employers=target_names,
            search_batches=1,
        )

    monkeypatch.setattr(recruitment_search, "_search_batch", fake_search_batch)
    result = recruitment_search.search_current_recruitment_jobs(SimpleNamespace())
    assert result.total_tokens == 15
    assert result.failed_pools == (pools[0]["id"],)
    assert len(result.target_employers) == 2
    assert len(result.searched_employers) == 1
    assert result.failed_employers == (pools[0]["employers"][0],)
    assert result.coverage_percent == 50.0
    assert result.search_batches == 2
    assert result.failed_batches == (f"{pools[0]['id']}:1",)


def test_web_search_keeps_multiple_roles_from_one_official_campaign_page(monkeypatch):
    pool = employer_only_pool(next(
        item for item in recruitment_search.PERSONAL_MONITOR_POOLS
        if item["primary_category"] == "internet_tech"
    ), "拼多多")
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    shared_url = "https://careers.pddglobalhr.com/campus/graduate"
    jobs = []
    for title, city in (
        (f"{target_year}届校园招聘产品策略岗", "上海"),
        (f"{target_year}届校园招聘数据分析岗", "深圳"),
    ):
        job = recruitment_search._normalize_job({
            "company": "拼多多",
            "title": title,
            "city": city,
            "industry": "互联网",
            "official_url": shared_url,
            "opening_date": None,
            "closing_date": "2099-09-01",
            "requirements": f"面向{target_year}届毕业生",
            "category": "互联网企业",
        }, pool)
        assert job is not None
        jobs.append(job)

    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", [pool])
    monkeypatch.setattr(
        recruitment_search,
        "_search_batch",
        lambda _client, batch: recruitment_search.WebRecruitmentSearchResult(
            jobs=jobs,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            tool_calls=1,
            model="test-model",
            target_employers=tuple(
                target.canonical_name for target in batch.targets
            ),
            searched_employers=tuple(
                target.canonical_name for target in batch.targets
            ),
            employers_with_candidates=("拼多多",),
            search_batches=1,
        ),
    )

    result = recruitment_search.search_current_recruitment_jobs(SimpleNamespace())
    assert len(result.jobs) == 2
    assert len({job["id"] for job in result.jobs}) == 2
    assert {job["url"] for job in result.jobs} == {shared_url}


def test_docx_extraction_preserves_readable_content():
    from docx import Document

    document = Document()
    document.add_paragraph("Termination requires 30 days written notice.")
    buffer = BytesIO()
    document.save(buffer)

    content, chunks = extract_document("agreement.docx", buffer.getvalue())
    assert "30 days" in content
    assert chunks[0]["page"] is None


def test_pdf_extraction_preserves_page_number():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Revenue was 120 million.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)

    content, chunks = extract_document("results.pdf", buffer.getvalue())
    assert "Revenue was 120 million" in content
    assert chunks[0]["page"] == 1


def test_legacy_database_schema_is_migrated(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY,
                document_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding TEXT NOT NULL
            );
            """
        )

    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=legacy_path))
    database.init_db()

    with sqlite3.connect(legacy_path) as connection:
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        document_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
        chunk_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(chunks)")
        }
    assert "workspace" in session_columns
    assert {"workspace", "file_type"} <= document_columns
    assert "page" in chunk_columns


def test_rag_filters_unrelated_chunks(monkeypatch):
    monkeypatch.setattr(ai_service.database, "list_documents", lambda *_: [{"id": "doc"}])
    monkeypatch.setattr(ai_service, "create_embeddings", lambda _: [[1.0, 0.0]])
    monkeypatch.setattr(
        ai_service.database,
        "search_chunks",
        lambda *_args, **_kwargs: [
            {"name": "unrelated.md", "score": 0.21},
            {"name": "relevant.md", "score": 0.61},
        ],
    )
    results = ai_service.retrieve_context(1, "query")
    assert [item["name"] for item in results] == ["relevant.md"]
