"""The real application boots without paid credentials or provider clients."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def test_zero_token_application_starts_without_a_model_key(tmp_path):
    environment = os.environ.copy()
    for name in (
        "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "SERPAPI_API_KEY", "SERPER_API_KEY", "BING_SEARCH_API_KEY",
        "GOOGLE_API_KEY", "DATABASE_URL", "RENDER", "RENDER_SERVICE_ID",
    ):
        environment.pop(name, None)
    environment.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "JWT_SECRET": "zero-token-startup-fixture-secret-at-least-32-characters",
        "DATABASE_BACKEND": "sqlite",
        "DATABASE_SCHEMA": "frostfire",
        "DATABASE_PATH": str(tmp_path / "zero-token.db"),
        "RECRUITMENT_REFRESH_MINUTES": "0",
        "RECRUITMENT_WEB_SEARCH_ENABLED": "false",
        "FUTURE_RADAR_ENABLED": "false",
    })
    code = textwrap.dedent("""
        import openai
        import urllib.request

        def forbidden(*args, **kwargs):
            raise AssertionError("Zero-token startup attempted a provider or public network call")

        openai.OpenAI = forbidden
        urllib.request.urlopen = forbidden
        urllib.request.OpenerDirector.open = forbidden

        from fastapi.testclient import TestClient
        from backend import ai_service, main
        from backend.config import load_settings

        assert load_settings().openai_api_key == ""
        assert ai_service.client is None
        assert ai_service.create_embeddings([]) == []
        assert ai_service.calculate("2 + 3")["result"] == 5
        assert ai_service.split_document("Recruitment title") == ["Recruitment title"]

        calls = (
            lambda: ai_service.create_embeddings(["private test text"]),
            lambda: ai_service.run_space("system", "input"),
            lambda: ai_service.run_cross_exam("focus", [], []),
            lambda: ai_service.run_agent([], tools_enabled=False),
            lambda: next(ai_service.stream_agent([])),
        )
        for invoke in calls:
            try:
                invoke()
            except ai_service.ModelUnavailableError as exc:
                assert str(exc) == ai_service.MODEL_UNAVAILABLE_MESSAGE
            else:
                raise AssertionError("Paid capability silently accepted missing credentials")

        with TestClient(main.app) as client:
            assert client.get("/api/health").status_code == 200
            registered = client.post("/api/auth/register", json={
                "username": "zero-token-user", "password": "local-fixture-password-123",
                "privacy_accepted": True,
            })
            assert registered.status_code == 201, registered.text
            headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
            assert client.get("/api/auth/me", headers=headers).status_code == 200
            assert client.get("/api/future-radar/sources", headers=headers).status_code == 200
            assert client.get("/api/future-radar/dashboard", headers=headers).status_code == 200
            from backend.future_radar.wechat.models import WechatArticleMetadata
            async def public_metadata(url, expected_source_name=None):
                return WechatArticleMetadata(
                    url=url, normalized_url=url, title="2027届校园招聘启动",
                    source_name="国聘", source_name_detection="page", fetch_status="success",
                )
            main.wechat_title_service.parser = public_metadata
            assert client.get("/api/sources/wechat").status_code == 401
            assert len(client.get("/api/sources/wechat", headers=headers).json()["items"]) == 5
            imported = client.post("/api/sources/wechat/article", headers=headers, json={
                "url": "https://mp.weixin.qq.com/s/zero-key-fixture",
            })
            assert imported.status_code == 200, imported.text
            assert imported.json()["verification_status"] == "unverified"
            assert imported.json()["lead_created"] is True
            assert client.get("/api/sources/wechat/articles", headers=headers).json()["total"] == 1
            monitor = client.post("/api/sources/wechat/monitor", headers=headers).json()
            assert monitor["status"] == "provider_pending"
            assert monitor["ai_calls"] == monitor["model_tokens_used"] == 0
        print("zero-token-startup-ok")
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "zero-token-startup-ok" in result.stdout


def test_configured_model_client_retains_existing_usage_contract(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("JWT_SECRET", "optional-provider-fixture-secret-at-least-32-characters")
    from backend import ai_service

    requests = []

    def complete(**request):
        requests.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="fixture reply", tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=complete)),
        embeddings=SimpleNamespace(create=lambda **_: SimpleNamespace(data=[
            SimpleNamespace(index=1, embedding=[2.0]),
            SimpleNamespace(index=0, embedding=[1.0]),
        ])),
    )
    monkeypatch.setattr(ai_service, "client", fake_client)
    assert ai_service.create_embeddings(["a", "b"]) == [[1.0], [2.0]]
    reply, tools, usage = ai_service.run_agent([], tools_enabled=False)
    assert (reply, tools, usage) == (
        "fixture reply", [], {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    )
    assert "tools" not in requests[-1]
    reply, usage = ai_service.run_space("system", "message")
    assert reply == "fixture reply"
    assert usage["total_tokens"] == 6
    assert all(request["store"] is False for request in requests)
