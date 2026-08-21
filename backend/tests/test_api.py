import os
import tempfile
from pathlib import Path


TEST_DIRECTORY = Path(tempfile.mkdtemp(prefix="ai-chat-tests-"))
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-tests"
os.environ["DATABASE_PATH"] = str(TEST_DIRECTORY / "test.db")

from fastapi.testclient import TestClient

from backend import main
from backend import ai_service
from backend.ai_service import calculate, split_document


def register(client: TestClient, username: str) -> tuple[str, dict]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "correct-horse-123"},
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
            json={"username": "ALICE", "password": "correct-horse-123"},
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


def test_persistent_chat_and_session_history(monkeypatch):
    monkeypatch.setattr(main, "retrieve_context", lambda *_: [])
    monkeypatch.setattr(
        main,
        "run_agent",
        lambda _: ("Persisted assistant reply", ["calculate"]),
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
        monkeypatch.setattr(main, "run_agent", lambda _: ("Aurora", []))
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

    def fake_stream(_):
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


def test_safe_calculator_and_chunking():
    assert calculate("(12 + 3) * 2")["result"] == 30
    assert len(split_document("A" * 2000)) >= 2

    try:
        calculate("__import__('os').system('echo unsafe')")
    except ValueError:
        pass
    else:
        raise AssertionError("Unsafe expression was accepted")


def test_rag_filters_unrelated_chunks(monkeypatch):
    monkeypatch.setattr(ai_service.database, "list_documents", lambda _: [{"id": "doc"}])
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
