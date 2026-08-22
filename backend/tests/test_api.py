import os
import sqlite3
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace


TEST_DIRECTORY = Path(tempfile.mkdtemp(prefix="ai-chat-tests-"))
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-tests"
os.environ["DATABASE_PATH"] = str(TEST_DIRECTORY / "test.db")

from fastapi.testclient import TestClient

from backend import main
from backend import ai_service
from backend import database
from backend.ai_service import (
    build_messages,
    calculate,
    calculate_financial_metric,
    extract_document,
    split_document,
    tools_for_workspace,
)


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
        assert set(profile.json()) == {"desired_roles", "industries", "employer_types"}
        jobs = client.get("/api/recruitment/jobs", headers=auth(token))
        assert jobs.status_code == 200
        payload = jobs.json()
        assert payload["data_status"]["mode"] == "sample"
        assert len(payload["monitor_pools"]) == 3
        assert payload["jobs"]
        first = payload["jobs"][0]
        assert "match_score" in first
        assert "estimated_rate" not in first
        assert "historical_rate" not in first
        assert "tier_label" not in first
        assert "days_left" in first
        assert first["tier_code"] in {"T0", "T1", "T2", "T3"}
        assert "composite_fit" not in first


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
