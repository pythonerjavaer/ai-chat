import importlib.util
import json
import pathlib
import urllib.error
from types import SimpleNamespace

import pytest


SCRIPT_PATH = pathlib.Path(__file__).parents[2] / "scripts" / "frostfire_source_import.py"
SPEC = importlib.util.spec_from_file_location("frostfire_source_import", SCRIPT_PATH)
source_import = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source_import)


def uuid_like_placeholder():
    # Assemble at runtime so repository scanners do not mistake test data for a
    # real conversation identifier while preserving UUID-shape validation.
    return "-".join(("12345678", "1234", "4123", "8123", "123456789abc"))


def credential_like_placeholder():
    # Deliberately synthetic and split so secret scanners do not flag the test.
    return "sk" + "-proj-" + "NOT_A_REAL_KEY_TEST_VALUE_123456"


def sync_payload(source_id="untrusted-source"):
    return {
        "version": "FROSTFIRE_SYNC_V1",
        "source_id": source_id,
        "source_name": "不应进入结果的聊天标题",
        "articles": [{
            "article_external_id": "signal-001",
            "publisher": "公开信源",
            "article_title": "某集团 2027 校园招聘",
            "article_url": "https://careers.example.com/campus/2027",
            "is_recruitment": True,
            "recruitment_year": 2027,
            "classification": "recruitment_signal",
        }],
    }


def test_latest_fenced_sync_is_selected_and_local_source_id_wins(monkeypatch):
    old = json.dumps({"version": "FROSTFIRE_SYNC_V1", "source_id": "old"})
    latest = json.dumps(sync_payload())
    page = SimpleNamespace(
        raw_text=f"```json\n{old}\n```\n一些文字\n```json\n{latest}\n```",
        text="",
    )
    monkeypatch.setattr(source_import, "_safe_chatgpt_share_url", lambda url: url)
    monkeypatch.setattr(source_import, "fetch_watch_page", lambda *_args, **_kwargs: page)

    result = source_import.payload_from_chatgpt_share(
        "https://chatgpt.com/share/public-example", "chatgpt-share-01", timeout=5
    )
    assert result["source_id"] == "chatgpt-share-01"
    assert result["articles"][0]["article_external_id"] == "signal-001"
    assert "source_name" not in result
    assert result["batch_id"].startswith("import-")
    assert "chatgpt.com" not in json.dumps(result)


def test_private_chatgpt_conversation_url_is_rejected(monkeypatch):
    monkeypatch.setattr(
        source_import,
        "validate_public_https_url",
        lambda value, resolve_dns=True: value,
    )
    with pytest.raises(source_import.ImportError, match="/share/"):
        source_import._safe_chatgpt_share_url("https://chatgpt.com/c/private-chat")


def test_structured_sync_rejects_contact_evidence():
    payload = sync_payload()
    payload["jobs"] = [{
        "company": "示例企业",
        "title": "2027 校园招聘",
        "official_url": "https://careers.example.com/jobs/1",
        "evidence": ["联系人 test@example.com"],
    }]
    with pytest.raises(source_import.ImportError, match="contact information") as captured:
        source_import._validated_payload(payload, "local-source")
    assert "test@example.com" not in str(captured.value)


@pytest.mark.parametrize(
    "field,value,error",
    [
        (
            "article_url",
            "https://chatgpt.com/c/private-conversation-placeholder",
            "cannot be persisted",
        ),
        (
            "article_url",
            "https://chatgpt.com/share/public-snapshot-placeholder",
            "cannot be persisted",
        ),
        (
            "article_url",
            "https://example.com/article?token=local-secret-value",
            "query parameters",
        ),
    ],
)
def test_sync_never_persists_chatgpt_or_credential_urls(field, value, error):
    payload = sync_payload()
    payload["articles"][0][field] = value
    with pytest.raises(source_import.ImportError, match=error) as captured:
        source_import._validated_payload(payload, "local-source")
    assert "private-conversation-placeholder" not in str(captured.value)
    assert "public-snapshot-placeholder" not in str(captured.value)
    assert "local-secret-value" not in str(captured.value)


def test_sync_rejects_secrets_and_pii_outside_evidence_without_echoing_values():
    payload = sync_payload()
    secret = credential_like_placeholder()
    payload["jobs"] = [{
        "company": "示例企业",
        "title": "2027 校园招聘",
        "official_url": "https://careers.example.com/jobs/1",
        "description": f"内部值 {secret}",
    }]
    with pytest.raises(source_import.ImportError, match="credential-like") as captured:
        source_import._validated_payload(payload, "local-source")
    assert secret not in str(captured.value)

    payload["jobs"][0]["description"] = "联系人 13800138000"
    with pytest.raises(source_import.ImportError, match="contact information") as captured:
        source_import._validated_payload(payload, "local-source")
    assert "13800138000" not in str(captured.value)


def test_rejected_secret_property_and_unknown_field_names_are_not_echoed():
    payload = sync_payload()
    malicious_key = "cookie_" + credential_like_placeholder()
    payload[malicious_key] = "hidden"
    with pytest.raises(source_import.ImportError) as captured:
        source_import._validated_payload(payload, "local-source")
    assert malicious_key not in str(captured.value)

    payload = sync_payload()
    malicious_key = "private-field-" + credential_like_placeholder()
    payload[malicious_key] = "hidden"
    with pytest.raises(source_import.ImportError, match="validation failed") as captured:
        source_import._validated_payload(payload, "local-source")
    assert malicious_key not in str(captured.value)


def test_source_id_must_not_be_a_conversation_uuid():
    with pytest.raises(SystemExit):
        source_import.parse_args([
            "--source-id", uuid_like_placeholder(),
            "--structured-json", "-",
        ])


def test_uuid_shaped_upstream_identifiers_are_pseudonymized_stably():
    raw_uuid = uuid_like_placeholder()
    payload = sync_payload()
    payload["articles"][0]["article_external_id"] = raw_uuid
    first = source_import._validated_payload(payload, "logical-source-01")
    second = source_import._validated_payload(payload, "logical-source-01")
    identifier = first["articles"][0]["article_external_id"]
    assert raw_uuid not in json.dumps(first)
    assert identifier == second["articles"][0]["article_external_id"]
    assert identifier.startswith("article_external_id-")


def test_public_article_is_discovery_only(monkeypatch):
    page = SimpleNamespace(
        final_url="https://mp.weixin.qq.com/s/public-article",
        fingerprint="article-hash",
        text="某企业 2027 届校园招聘现已启动，申请条件以招聘官网为准。",
    )
    monkeypatch.setattr(source_import, "fetch_watch_page", lambda *_args, **_kwargs: page)
    result = source_import.payload_from_article(
        page.final_url,
        "wechat-public-01",
        title="某企业 2027 届校园招聘",
        publisher="公开公众号",
        published_at="2026-08-28T01:00:00+00:00",
        timeout=5,
    )
    assert result["jobs"] == []
    assert result["articles"][0]["is_recruitment"] is True
    assert result["articles"][0]["recruitment_year"] == 2027
    assert result["articles"][0]["article_url"].startswith("https://mp.weixin.qq.com/")


def test_public_article_redacts_untrusted_contacts_credentials_and_uuids(monkeypatch):
    page = SimpleNamespace(
        final_url="https://mp.weixin.qq.com/s/public-article",
        fingerprint="article-hash",
        text=(
            "校园招聘 联系人 test@example.com 13800138000 "
            f"api_key={credential_like_placeholder()} "
            f"{uuid_like_placeholder()}"
        ),
    )
    monkeypatch.setattr(source_import, "fetch_watch_page", lambda *_args, **_kwargs: page)
    result = source_import.payload_from_article(
        page.final_url,
        "wechat-public-01",
        title="公开招聘文章",
        publisher="公开公众号",
        published_at=None,
        timeout=5,
    )
    serialized = json.dumps(result, ensure_ascii=False)
    assert "test@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "sk" + "-proj-" not in serialized
    assert "12345678" not in serialized
    assert "[redacted-email]" in serialized


def test_submit_and_response_errors_never_echo_token_or_pii(monkeypatch):
    token = "local-ingest-secret"

    def failed(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            source_import.ENDPOINT, 500, token, hdrs=None, fp=None
        )

    monkeypatch.setattr(source_import.urllib.request, "urlopen", failed)
    with pytest.raises(source_import.ImportError, match="HTTP 500") as captured:
        source_import.submit_payload(
            {"batch_id": "import-test", "version": "FROSTFIRE_SYNC_V1"}, token, 5
        )
    assert token not in str(captured.value)

    response = source_import._safe_response({
        "token": token,
        "detail": f"test@example.com 13800138000 {uuid_like_placeholder()}",
    })
    serialized = json.dumps(response)
    assert token not in serialized
    assert "test@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "12345678" not in serialized
