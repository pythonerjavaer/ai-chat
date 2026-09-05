import io
import json
import pathlib
import stat
import urllib.error
from unittest import mock

import pytest

from scripts import frostfire_chatgpt_bridge as bridge


def uuid_like_placeholder() -> str:
    # Assemble at runtime so repository scanners do not mistake synthetic test
    # data for a real conversation identifier.
    return "-".join(("12345678", "1234", "4123", "8123", "123456789abc"))


def credential_like_placeholder() -> str:
    return "sk" + "-proj-" + "NOT_A_REAL_KEY_TEST_VALUE_123456"


def job(index: int = 1, **overrides):
    value = {
        "source_item_id": f"row-{index}",
        "company": f"示例企业 {index}",
        "title": f"2027 校园招聘岗位 {index}",
        "city": "上海",
        "official_url": f"https://careers.example.com/jobs/{index}",
        "tags": ["校招"],
        "evidence": ["官方招聘页列出该岗位，日期仍待服务端核验。"],
    }
    value.update(overrides)
    return value


def browser_message(rows=None, *, message_id="message-42", **overrides):
    value = {
        "source_id": "chatgpt-radar-01",
        "message_id": message_id,
        "rows": [job()] if rows is None else rows,
    }
    value.update(overrides)
    return value


def run_main(monkeypatch, value, argv):
    stdin = io.StringIO(json.dumps(value, ensure_ascii=False))
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(bridge.sys, "stdin", stdin)
    monkeypatch.setattr(bridge.sys, "stdout", stdout)
    monkeypatch.setattr(bridge.sys, "stderr", stderr)
    code = bridge.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def test_builds_discovery_only_sync_without_message_metadata():
    raw_message_id = "logical-message-20260828-001"
    source_id, digest, rows = bridge.parse_browser_message(
        browser_message(
            message_id=raw_message_id,
            rows=[job(tags=["官方已核验", "链接已验证", "校招"])],
        )
    )
    payload = bridge.build_batches(source_id, digest, rows)[0]
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["version"] == "FROSTFIRE_SYNC_V1"
    assert payload["source_id"] == "chatgpt-radar-01"
    assert payload["snapshot_complete"] is False
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["verification_status"] == "pending"
    assert payload["jobs"][0]["confidence_score"] == 0.55
    assert "待官方核验" in payload["jobs"][0]["tags"]
    assert "官方已核验" not in payload["jobs"][0]["tags"]
    assert "链接已验证" not in payload["jobs"][0]["tags"]
    assert raw_message_id not in serialized
    assert "message_id" not in serialized
    assert payload["batch_id"].startswith("bridge-")


def test_stable_ids_and_large_input_is_split_without_truncation():
    source_id, digest, rows = bridge.parse_browser_message(
        browser_message(rows=[job(index) for index in range(123)])
    )
    first = bridge.build_batches(source_id, digest, rows)
    second = bridge.build_batches(source_id, digest, rows)

    assert [len(item["jobs"]) for item in first] == [25, 25, 25, 25, 23]
    wider = bridge.build_batches(source_id, digest, rows, batch_size=100)
    assert [len(item["jobs"]) for item in wider] == [100, 23]
    assert [row["external_id"] for batch in first for row in batch["jobs"]] == [
        row["external_id"] for batch in wider for row in batch["jobs"]
    ]
    assert [item["batch_id"] for item in first] == [
        item["batch_id"] for item in second
    ]
    assert [
        item["external_id"] for batch in first for item in batch["jobs"]
    ] == [
        item["external_id"] for batch in second for item in batch["jobs"]
    ]


def test_empty_rows_create_one_safe_heartbeat():
    source_id, digest, rows = bridge.parse_browser_message(browser_message(rows=[]))
    batches = bridge.build_batches(source_id, digest, rows)

    assert len(batches) == 1
    assert batches[0]["jobs"] == []
    assert batches[0]["snapshot_complete"] is False


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda value: value.update({"conversation_url": "https://example.com"}),
            "only source_id",
        ),
        (
            lambda value: value["rows"][0].update({"source_thread_id": "private"}),
            "unsupported properties",
        ),
        (
            lambda value: value["rows"][0].update(
                {"official_url": "https://chatgpt.com/c/private-placeholder"}
            ),
            "cannot be persisted",
        ),
        (
            lambda value: value["rows"][0].update(
                {"official_url": "http://careers.example.com/jobs/1"}
            ),
            "public HTTPS URL",
        ),
        (
            lambda value: value["rows"][0].update(
                {"evidence": ["line one\nline two"]}
            ),
            "single-line",
        ),
    ],
)
def test_strict_shape_and_url_validation(mutation, error):
    value = browser_message()
    mutation(value)
    with pytest.raises(bridge.BridgeError, match=error):
        source_id, digest, rows = bridge.parse_browser_message(value)
        bridge.build_batches(source_id, digest, rows)


def test_rejects_conversation_uuid_as_message_id_without_echoing_it():
    raw_uuid = uuid_like_placeholder()
    with pytest.raises(bridge.BridgeError, match="conversation UUID") as captured:
        bridge.parse_browser_message(browser_message(message_id=raw_uuid))
    assert raw_uuid not in str(captured.value)


def test_rejects_secrets_and_contacts_without_echoing_values():
    secret = credential_like_placeholder()
    value = browser_message(rows=[job(description=f"内部材料 {secret}")])
    with pytest.raises(bridge.BridgeError, match="credential-like") as captured:
        source_id, digest, rows = bridge.parse_browser_message(value)
        bridge.build_batches(source_id, digest, rows)
    assert secret not in str(captured.value)

    phone = "138" + "0013" + "8000"
    value = browser_message(rows=[job(requirements=f"联系 {phone}")])
    with pytest.raises(bridge.BridgeError, match="contact information") as captured:
        source_id, digest, rows = bridge.parse_browser_message(value)
        bridge.build_batches(source_id, digest, rows)
    assert phone not in str(captured.value)


def test_unknown_secret_property_name_is_never_echoed():
    malicious_key = "cookie_" + credential_like_placeholder()
    value = browser_message()
    value["rows"][0][malicious_key] = "hidden"
    with pytest.raises(bridge.BridgeError) as captured:
        bridge.parse_browser_message(value)
    assert malicious_key not in str(captured.value)


def test_dry_run_uses_no_keychain_network_or_cursor_write(monkeypatch, tmp_path):
    cursor = tmp_path / "cursor.json"
    keychain = mock.Mock(return_value="not-used")
    submit = mock.Mock()
    save = mock.Mock()
    monkeypatch.setattr(bridge, "read_keychain_token", keychain)
    monkeypatch.setattr(bridge, "submit_payload", submit)
    monkeypatch.setattr(bridge, "save_cursor", save)

    code, stdout, stderr = run_main(
        monkeypatch,
        browser_message(rows=[job(1), job(2)]),
        ["--dry-run", "--cursor-file", str(cursor)],
    )
    result = json.loads(stdout)
    assert code == 0
    assert not stderr
    assert result == {
        "batch_sizes": [2],
        "batches": 1,
        "dry_run": True,
        "heartbeat": False,
        "rows": 2,
        "source_id": "chatgpt-radar-01",
        "status": "ready",
    }
    keychain.assert_not_called()
    submit.assert_not_called()
    save.assert_not_called()
    assert not cursor.exists()


def test_submit_uses_keychain_and_hash_only_atomic_cursor(monkeypatch, tmp_path):
    cursor = tmp_path / "state" / "cursor.json"
    raw_message_id = "logical-message-20260828-002"
    token = "local-keychain-only-secret"
    keychain = mock.Mock(return_value=token)
    submit = mock.Mock(
        side_effect=[
            {"accepted": 25, "token": token},
            {"accepted": 2, "detail": "safe"},
        ]
    )
    monkeypatch.setattr(bridge, "read_keychain_token", keychain)
    monkeypatch.setattr(bridge, "submit_payload", submit)

    value = browser_message(
        rows=[job(index) for index in range(27)], message_id=raw_message_id
    )
    code, stdout, stderr = run_main(
        monkeypatch,
        value,
        ["--submit", "--cursor-file", str(cursor)],
    )
    output = json.loads(stdout)
    cursor_text = cursor.read_text(encoding="utf-8")
    assert code == 0
    assert not stderr
    assert output["status"] == "submitted"
    assert output["batches"] == 2
    assert "token" not in json.dumps(output)
    assert raw_message_id not in cursor_text
    assert "careers.example.com" not in cursor_text
    assert json.loads(cursor_text)["sources"]["chatgpt-radar-01"][
        "message_digest"
    ] == bridge._message_digest("chatgpt-radar-01", raw_message_id)
    assert stat.S_IMODE(cursor.stat().st_mode) == 0o600
    keychain.assert_called_once_with()
    assert submit.call_count == 2
    assert all(call.args[1] == token for call in submit.call_args_list)
    assert all(len(call.args[0]["jobs"]) <= 25 for call in submit.call_args_list)

    keychain.reset_mock()
    submit.reset_mock()
    code, stdout, stderr = run_main(
        monkeypatch,
        value,
        ["--submit", "--cursor-file", str(cursor)],
    )
    assert code == 0
    assert not stderr
    assert json.loads(stdout)["status"] == "unchanged"
    keychain.assert_not_called()
    submit.assert_not_called()


def test_partial_submission_never_advances_cursor(monkeypatch, tmp_path):
    cursor = tmp_path / "cursor.json"
    submit = mock.Mock(
        side_effect=[
            {"accepted": 25},
            bridge.SourceImportError("server returned HTTP 500"),
        ]
    )
    monkeypatch.setattr(bridge, "read_keychain_token", lambda: "local-secret")
    monkeypatch.setattr(bridge, "submit_payload", submit)

    code, stdout, stderr = run_main(
        monkeypatch,
        browser_message(rows=[job(index) for index in range(27)]),
        ["--submit", "--cursor-file", str(cursor)],
    )
    assert code == 2
    assert not stdout
    assert "HTTP 500" in stderr
    assert not cursor.exists()
    assert submit.call_count == 2


def test_http_error_is_reduced_to_safe_status_without_body_or_traceback(
    monkeypatch, tmp_path
):
    cursor = tmp_path / "cursor.json"
    token = "local-keychain-only-secret"
    private_url = "https://private.example.test/path?token=must-not-leak"
    response_body = f'{{"detail":"{token} {private_url}"}}'.encode()
    error = urllib.error.HTTPError(
        private_url,
        401,
        f"unauthorized {token}",
        {"Authorization": token},
        io.BytesIO(response_body),
    )
    monkeypatch.setattr(bridge, "read_keychain_token", lambda: token)
    monkeypatch.setattr(bridge, "submit_payload", mock.Mock(side_effect=error))

    code, stdout, stderr = run_main(
        monkeypatch,
        browser_message(),
        ["--submit", "--cursor-file", str(cursor)],
    )
    assert code == 2
    assert not stdout
    assert stderr.strip() == "bridge error: submission failed (HTTP 401)"
    assert token not in stderr
    assert private_url not in stderr
    assert "Traceback" not in stderr
    assert not cursor.exists()


@pytest.mark.parametrize(
    "transport_error",
    [
        urllib.error.URLError("private URL and token must not leak"),
        TimeoutError("private URL and token must not leak"),
    ],
)
def test_network_errors_are_generic_and_do_not_advance_cursor(
    monkeypatch, tmp_path, transport_error
):
    cursor = tmp_path / "cursor.json"
    monkeypatch.setattr(bridge, "read_keychain_token", lambda: "local-secret")
    monkeypatch.setattr(
        bridge, "submit_payload", mock.Mock(side_effect=transport_error)
    )

    code, stdout, stderr = run_main(
        monkeypatch,
        browser_message(),
        ["--submit", "--cursor-file", str(cursor)],
    )
    assert code == 2
    assert not stdout
    assert stderr.strip() == (
        "bridge error: submission endpoint is temporarily unavailable"
    )
    assert "private URL" not in stderr
    assert "local-secret" not in stderr
    assert "Traceback" not in stderr
    assert not cursor.exists()


def test_cursor_rejects_non_hash_state_and_private_source_identifiers(tmp_path):
    cursor = tmp_path / "cursor.json"
    raw_uuid = uuid_like_placeholder()
    cursor.write_text(
        json.dumps(
            {
                "version": bridge.CURSOR_VERSION,
                "sources": {raw_uuid: {"message_digest": "a" * 64}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(bridge.BridgeError, match="unsupported format") as captured:
        bridge.load_cursor(cursor)
    assert raw_uuid not in str(captured.value)

    cursor.write_text(
        json.dumps(
            {
                "version": bridge.CURSOR_VERSION,
                "sources": {
                    "chatgpt-radar-01": {
                        "message_digest": "not-a-hash",
                        "message_id": "must-not-be-retained",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(bridge.BridgeError, match="unsupported format"):
        bridge.load_cursor(cursor)


def test_default_emit_does_not_advance_cursor(monkeypatch, tmp_path):
    cursor = tmp_path / "cursor.json"
    code, stdout, stderr = run_main(
        monkeypatch,
        browser_message(),
        ["--cursor-file", str(cursor)],
    )
    payload = json.loads(stdout)
    assert code == 0
    assert not stderr
    assert payload["version"] == "FROSTFIRE_SYNC_V1"
    assert not cursor.exists()


def test_oversized_rows_are_rejected_instead_of_truncated():
    value = browser_message(rows=[job(company="企" * 161)])
    with pytest.raises(bridge.BridgeError, match="maximum length"):
        bridge.parse_browser_message(value)


@pytest.mark.parametrize("source", ["chatgpt-radar-04", "chatgpt-radar-05", "chatgpt-radar-10"])
def test_retired_or_unknown_monitor_cannot_submit_new_rows(source):
    with pytest.raises(bridge.BridgeError, match="active"):
        bridge.parse_browser_message(browser_message(source_id=source))


@pytest.mark.parametrize("source", ["chatgpt-radar-07", "chatgpt-radar-08", "chatgpt-radar-09"])
def test_new_monitor_labels_accept_individual_rendered_job_rows(source):
    source_id, digest, rows = bridge.parse_browser_message(browser_message(source_id=source))
    assert bridge.build_batches(source_id, digest, rows)[0]["source_id"] == source


def test_source_rating_survives_both_sync_and_ingest_with_scope_and_exact_value():
    rating = {"scope": "company", "tier_code": "T0.5", "score": 92.25, "reason": "原表公司层面的评分"}
    source_id, digest, rows = bridge.parse_browser_message(browser_message(rows=[job(source_rating=rating)]))
    payload = bridge.build_batches(source_id, digest, rows)[0]
    assert payload["jobs"][0]["source_rating"] == rating
    assert bridge._legacy_ingest_batch(payload)["jobs"][0]["source_rating"] == rating
    assert "source_rating" not in bridge._normalize_row(job(), source_id)


def test_corrected_rating_changes_batch_id_and_same_message_cursor_content(monkeypatch, tmp_path):
    path = tmp_path / "cursor.json"
    first = browser_message(rows=[job(source_rating={"scope": "job", "score": 89})])
    corrected = browser_message(rows=[job(source_rating={"scope": "job", "score": 91.5})])
    monkeypatch.setattr(bridge, "read_keychain_token", lambda: "synthetic")
    submit = mock.Mock(return_value={"accepted": 1})
    monkeypatch.setattr(bridge, "submit_payload", submit)
    batch_ids = []
    for value in (first, corrected):
        source_id, digest, rows = bridge.parse_browser_message(value)
        batch_ids.append(bridge.build_batches(source_id, digest, rows)[0]["batch_id"])
        code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--cursor-file", str(path)])
        assert code == 0 and not stderr and json.loads(stdout)["status"] == "submitted"
    assert batch_ids[0] != batch_ids[1]
    assert submit.call_count == 2
    assert submit.call_args.args[0]["jobs"][0]["source_rating"] == {"scope": "job", "score": 91.5}
    assert run_main(monkeypatch, corrected, ["--submit", "--cursor-file", str(path)])[0] == 0
    assert submit.call_count == 2


def test_retired_hash_only_cursors_remain_unchanged_after_new_source_save(tmp_path):
    path = tmp_path / "cursor.json"
    prior = {"version": 1, "sources": {
        "chatgpt-radar-04": {"message_digest": "a" * 64},
        "chatgpt-radar-05": {"message_digest": "b" * 64},
    }}
    path.write_text(json.dumps(prior))
    loaded = bridge.load_cursor(path)
    bridge.save_cursor(path, loaded, "chatgpt-radar-09", "c" * 64, "d" * 64)
    result = bridge.load_cursor(path)
    assert result["sources"]["chatgpt-radar-04"] == prior["sources"]["chatgpt-radar-04"]
    assert result["sources"]["chatgpt-radar-05"] == prior["sources"]["chatgpt-radar-05"]
    assert bridge.cursor_has_message(loaded, "chatgpt-radar-04", "a" * 64, "e" * 64)
