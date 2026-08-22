import os
import sqlite3
import tempfile
from datetime import date, timedelta
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace


TEST_DIRECTORY = Path(tempfile.mkdtemp(prefix="ai-chat-tests-"))
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-tests"
os.environ["DATABASE_PATH"] = str(TEST_DIRECTORY / "test.db")
os.environ["RECRUITMENT_INGEST_TOKEN"] = "test-recruitment-ingest-token"
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
        lambda *_: ("Persisted assistant reply", ["calculate"]),
    )

    with TestClient(main.app) as client:
        token, _ = register(client, "persistent-user")
        response = client.post(
            "/api/chat",
            headers=auth(token),
            json={"message": "Remember this message"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["reply"] == "Persisted assistant reply"
        assert payload["tools_used"] == ["calculate"]

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
        }

    monkeypatch.setattr(main, "stream_agent", fake_stream)

    with TestClient(main.app) as client:
        token, _ = register(client, "stream-user")
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
        assert set(payload["data_status"]["tier_counts"]) == {"T0", "T1", "T2", "T3"}
        assert len(payload["monitor_pools"]) == 8
        assert payload["jobs"]
        titles = {job["title"] for job in payload["jobs"]}
        assert "拼多多 2027届校园招聘提前批" in titles
        assert "2027 Business Analyst (General Practice)_Campus" in titles
        assert sum("梧桐计划" in title for title in titles) == 8
        assert all(job["url"].startswith("https://") for job in payload["jobs"])
        assert not any(job["id"].startswith("sample-") for job in payload["jobs"])
        first = payload["jobs"][0]
        assert "match_score" in first
        assert "estimated_rate" not in first
        assert "historical_rate" not in first
        assert "tier_label" not in first
        assert "days_left" in first
        assert first["tier_code"] in {"T0", "T1", "T2", "T3"}
        assert "composite_fit" not in first


def test_recruitment_ingest_accepts_live_campus_jobs_and_rejects_expired_jobs():
    payload = {
        "jobs": [
            {
                "company": "测试重点机构",
                "title": "2099届校园招聘数据岗",
                "city": "北京",
                "official_url": "https://example.com/campus/data-2099",
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
        assert any(job["title"] == "2099届校园招聘数据岗" for job in jobs)
        assert not any(job["title"] == "2020届校园招聘岗位" for job in jobs)
        closed = client.post(
            "/api/recruitment/ingest",
            headers={"X-Recruitment-Token": "test-recruitment-ingest-token"},
            json={"jobs": [{**payload["jobs"][0], "status": "closed"}]},
        )
        assert closed.status_code == 200
        assert closed.json()["skipped"] == [
            {"title": "2099届校园招聘数据岗", "reason": "closed"}
        ]
        jobs = client.get("/api/recruitment/jobs", headers=auth(token)).json()["jobs"]
        assert not any(job["title"] == "2099届校园招聘数据岗" for job in jobs)


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
                    "name": f"九坤岗位 {index}",
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


def test_recruitment_jobs_sort_today_deadline_before_tomorrow(monkeypatch):
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
        "tags": ["校园招聘", "动态监控"],
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
        assert [job["id"] for job in jobs[:2]] == ["today", "tomorrow"]


def test_bounded_web_search_normalizes_priority_jobs_and_rejects_noise(monkeypatch):
    payload = {
        "jobs": [
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
    }

    class FakeResponses:
        def create(self, **kwargs):
            assert kwargs["tools"][0]["type"] == "web_search"
            assert kwargs["max_tool_calls"] <= 6
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(type="web_search_call")],
                usage=SimpleNamespace(input_tokens=800, output_tokens=160, total_tokens=960),
                model="gpt-4o-mini",
            )

    monkeypatch.setattr(
        recruitment_search,
        "PERSONAL_MONITOR_POOLS",
        recruitment_search.PERSONAL_MONITOR_POOLS[:1],
    )
    result = recruitment_search.search_current_recruitment_jobs(
        SimpleNamespace(responses=FakeResponses())
    )
    assert len(result.jobs) == 1
    assert result.jobs[0]["company"] == "拼多多"
    assert result.jobs[0]["employer_type"] == "互联网企业"
    assert "动态监控" in result.jobs[0]["tags"]
    assert result.tool_calls == 1
    assert result.total_tokens == 960
    assert result.failed_pools == ()


def test_web_search_keeps_successful_pools_when_one_pool_fails(monkeypatch):
    pools = recruitment_search.PERSONAL_MONITOR_POOLS[:2]
    monkeypatch.setattr(recruitment_search, "PERSONAL_MONITOR_POOLS", pools)

    def fake_search_pool(_client, pool):
        if pool["id"] == pools[0]["id"]:
            raise RuntimeError("temporary search failure")
        return recruitment_search.WebRecruitmentSearchResult(
            jobs=[],
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            tool_calls=1,
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(recruitment_search, "_search_pool", fake_search_pool)
    result = recruitment_search.search_current_recruitment_jobs(SimpleNamespace())
    assert result.total_tokens == 15
    assert result.failed_pools == (pools[0]["id"],)


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
