import hashlib
import io
import json
import socket
import stat
import subprocess
import urllib.error
from datetime import date
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts import frostfire_chatgpt_history as history


TODAY = date(2026, 8, 31)


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def job(index=1, **changes):
    value = {
        "external_id": f"public-ats-{index}",
        "company": f"示例雇主{index}",
        "title": f"2027届校园招聘分析岗位{index}",
        "city": "上海",
        "official_url": f"https://careers.example.com/jobs/{index}",
        "status": "open",
        "evidence": ["公开招聘公告列出此岗位，仍由服务器复核。"],
    }
    value.update(changes)
    return value


def message(label, rows):
    return {"message_digest": digest(label), "rows": rows}


def document(messages=None, *, source="chatgpt-radar-01", complete=False):
    return {
        "source_id": source,
        "history_complete": complete,
        "messages": messages if messages is not None else [message("one", [job()])],
    }


def empty_ledger():
    return {"version": history.LEDGER_VERSION, "sources": {}}


def plan(value, ledger=None):
    return history.prepare_history(history.parse_history(value), ledger or empty_ledger(), today=TODAY)


def receipt(payload, _token, _timeout):
    return 200, json.dumps({"received": len(payload["jobs"]), "pending": len(payload["jobs"])}).encode()


@pytest.fixture(autouse=True)
def no_network_or_real_keychain(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", mock.Mock(side_effect=AssertionError("offline test")))
    monkeypatch.setattr(socket, "create_connection", mock.Mock(side_effect=AssertionError("offline test")))
    monkeypatch.setattr(history, "read_keychain_token", mock.Mock(side_effect=AssertionError("no real Keychain access")))
    monkeypatch.setattr(history, "submit_payload", mock.Mock(side_effect=AssertionError("no real submission")))


def run_main(monkeypatch, value, argv):
    stdin = io.StringIO(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(history.sys, "stdin", stdin)
    monkeypatch.setattr(history.sys, "stdout", stdout)
    monkeypatch.setattr(history.sys, "stderr", stderr)
    original = history.prepare_history
    monkeypatch.setattr(history, "prepare_history", partial(original, today=TODAY))
    try:
        code = history.main(argv)
    finally:
        monkeypatch.setattr(history, "prepare_history", original)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("source", ["chatgpt-radar-00", "chatgpt-radar-07", "arbitrary", None, [], "https://example.com"])
def test_only_six_logical_sources(source):
    with pytest.raises(history.HistoryError, match="source_id"):
        history.parse_history(document(source=source))


@pytest.mark.parametrize("field", ["source_thread_id", "conversation_url", "cookies", "profile", "raw_text", "message_id"])
def test_top_level_private_properties_rejected_without_value_echo(field):
    value = document()
    value[field] = "do-not-echo-this-private-content"
    with pytest.raises(history.HistoryError) as error:
        history.parse_history(value)
    assert "do-not-echo" not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"source_id": "chatgpt-radar-06"},
    {"source_thread_id": "private"},
    {"profile": {"university": "private"}},
    {"requirements": "contact person@example.com"},
    {"evidence": ["联系人 13800138000"]},
    {"official_url": "https://chatgpt.com/c/not-a-real-conversation"},
    {"description": "https%3A%2F%2Fchatgpt.com%2Fc%2Fnot-a-real-conversation"},
    {"requirements": "password=not-a-real-password"},
    {"official_url": "https://careers.example.com/jobs/1?token=private-value"},
    {"verification_status": "verified"},
])
def test_private_or_unsupported_row_fields_rejected(changes):
    with pytest.raises(history.HistoryError, match="validation failed") as error:
        plan(document([message("one", [job(**changes)])]))
    assert "private-value" not in str(error.value)
    assert "person@example.com" not in str(error.value)


def test_private_fields_in_held_history_are_rejected_not_silently_dropped():
    with pytest.raises(history.HistoryError):
        plan(document([message("old", [job(status="closed", evidence=["contact person@example.com"])])]))


@pytest.mark.parametrize("bad", ["message-1", "a" * 63, "g" * 64, "A" * 64, None])
def test_message_requires_irreversible_sha256(bad):
    value = document()
    value["messages"][0]["message_digest"] = bad
    with pytest.raises(history.HistoryError, match="SHA-256"):
        history.parse_history(value)


def test_inaccessible_or_unstructured_history_is_not_an_empty_heartbeat():
    for value in (document([]), document([{"message_digest": digest("missing")}]), document([message("bad", None)])):
        with pytest.raises(history.HistoryError):
            plan(value)


def test_complete_flag_is_strict_boolean_and_input_bounded(monkeypatch):
    value = document()
    value["history_complete"] = "true"
    with pytest.raises(history.HistoryError):
        plan(value)
    monkeypatch.setattr(history, "MAX_TOTAL_ROWS", 1)
    with pytest.raises(history.HistoryError, match="row limit"):
        plan(document([message("one", [job(1), job(2)])]))


def test_same_digest_with_conflicting_data_is_rejected():
    with pytest.raises(history.HistoryError, match="conflicting rows"):
        plan(document([message("same", [job(1)]), message("same", [job(2)])]))
    output = plan(document([message("same", [job(1)]), message("same", [job(1)])]))
    assert len(output.messages) == 1


def test_duplicate_json_property_is_rejected_before_source_override():
    with pytest.raises(history.HistoryError):
        history._json_loads('{"source_id":"chatgpt-radar-01","source_id":"chatgpt-radar-06"}')
    with pytest.raises(history.HistoryError):
        history._json_loads('{"unknown":NaN}')


def test_newest_first_dedupe_preserves_latest_role_and_splits_at_ten():
    newest = message("new", [job(1, requirements="新的公开要求"), *[job(index) for index in range(2, 14)]])
    older = message("old", [job(1, requirements="旧要求"), job(2)])
    output = plan(document([newest, older]))
    assert [len(batch.payload["jobs"]) for batch in output.batches] == [10, 3]
    assert output.batches[0].payload["jobs"][0]["requirements"] == "新的公开要求"
    assert output.duplicate_rows == 2
    assert output.summary()["history_complete"] is False
    emitted = json.dumps([batch.payload for batch in output.batches], ensure_ascii=False)
    assert digest("new") not in emitted
    assert "message_digest" not in emitted
    assert all(batch.payload["source_id"] == "chatgpt-radar-01" for batch in output.batches)
    assert all(item["source_id"] == "chatgpt-radar-01" for batch in output.batches for item in batch.payload["jobs"])


def test_large_single_message_is_batched_without_silently_truncating_at_100():
    output = plan(document([message("long-public-table", [job(i) for i in range(103)])]))
    assert sum(len(batch.items) for batch in output.batches) == 103
    assert len(output.batches) == 11
    assert output.batches[-1].payload["jobs"][-1]["external_id"] == "public-ats-102"


def test_same_semantic_job_with_different_row_id_dedupes_without_city_cross_product():
    output = plan(document([
        message("new", [job(1, external_id="new-row", city="上海、北京")]),
        message("old", [job(1, external_id="old-row", city="上海、北京")]),
    ]))
    assert len(output.batches[0].items) == 1
    assert output.batches[0].payload["jobs"][0]["city"] == "上海、北京"
    assert output.batches[0].payload["jobs"][0]["external_id"] == "new-row"


def test_different_explicit_ats_requisitions_not_merged_when_urls_differ():
    output = plan(document([message("roles", [job(1), job(2, company=job(1)["company"], title=job(1)["title"])])]))
    assert len(output.batches[0].items) == 2


def test_missing_status_and_dates_do_not_claim_open_or_verified():
    row = job(1, tags=["官方已核验", "链接已验证"])
    row.pop("status")
    output = plan(document([message("one", [row])]))
    normalized = output.history.messages[0].rows[0]
    job_out = output.batches[0].payload["jobs"][0]
    assert normalized["verification_status"] == "pending"
    assert normalized["status"] == "unknown"
    assert "status" not in job_out
    assert job_out["opening_date"] is None and job_out["closing_date"] is None
    assert "官方已核验" not in job_out["tags"]
    assert "开放状态待核验" in job_out["tags"]


def test_closed_expired_and_deadline_today_are_held_and_never_close_existing_job():
    output = plan(document([message("old", [
        job(1, status="closed"), job(2, closing_date="2026-08-30"), job(3, closing_date="2026-08-31"),
        job(4, closing_date="2026-09-01"),
    ])], complete=True))
    assert output.summary()["held_rows"] == 3
    assert output.summary()["heartbeat_batches"] == 0
    assert [item["external_id"] for batch in output.batches for item in batch.payload["jobs"]] == ["public-ats-4"]


def test_newer_closed_observation_cannot_resurrect_old_open_row():
    output = plan(document([message("new", [job(1, status="closed")]), message("old", [job(1)])]))
    assert output.batches == []
    assert output.summary()["held_rows"] == 2
    assert output.held_reasons["superseded_by_newer_held_row"] == 1


@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--emit"]])
def test_offline_modes_use_real_ingest_dry_run_but_never_keychain_network_or_ledger(monkeypatch, tmp_path, mode):
    path = tmp_path / "not-created" / "ledger.json"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code, stdout, stderr = run_main(monkeypatch, document(), [*mode, "--ledger-file", str(path)])
    assert code == 0 and not stderr
    output = json.loads(stdout)
    if mode == ["--emit"]:
        assert set(output[0]) == {"source_id", "jobs"}
        assert output[0]["jobs"][0]["external_id"] == "public-ats-1"
    else:
        assert output["dry_run"] is True and output["eligible_rows"] == 1
        assert "careers.example.com" not in stdout
    assert not path.parent.exists()
    history.read_keychain_token.assert_not_called()
    history.submit_payload.assert_not_called()


def test_all_batches_pass_actual_cli_preflight_before_any_keychain_read(monkeypatch, tmp_path):
    events = []
    real_run = subprocess.run

    def preflight(*args, **kwargs):
        assert args[0][-1] == "--dry-run"
        result = real_run(*args, **kwargs)
        events.append("dry-run")
        return result

    def keychain():
        assert events == ["dry-run", "dry-run", "dry-run"]
        events.append("keychain")
        return "not-a-real-keychain-token"

    monkeypatch.setattr(history.subprocess, "run", preflight)
    monkeypatch.setattr(history, "read_keychain_token", keychain)
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    path = tmp_path / "ledger.json"
    value = document([message("many", [job(i) for i in range(23)])], complete=True)
    code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
    assert code == 0 and not stderr
    output = json.loads(stdout)
    assert output["history_complete"] is True
    assert output["successful_batches"] == 3
    assert submit.call_count == 3
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    saved = path.read_text()
    assert "careers.example.com" not in saved
    assert "public-ats" not in saved and "示例" not in saved
    assert "not-a-real-keychain-token" not in saved + stdout + stderr


def test_preflight_failure_never_reads_keychain_or_persists_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(history.subprocess, "run", mock.Mock(return_value=SimpleNamespace(returncode=2, stdout="", stderr="do-not-echo-private-body")))
    path = tmp_path / "ledger.json"
    code, stdout, stderr = run_main(monkeypatch, document(), ["--submit", "--ledger-file", str(path)])
    assert code == 2 and not stdout
    assert "dry-run validation failed" in stderr
    assert "do-not-echo" not in stderr
    assert not path.exists()
    history.read_keychain_token.assert_not_called()
    history.submit_payload.assert_not_called()


def test_partial_failure_advances_only_confirmed_messages_and_retry_only_unsent_rows(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    value = document([
        message("newest", [job(1), job(2)]),
        message("older", [job(i) for i in range(3, 14)]),
    ], complete=True)
    keychain = mock.Mock(return_value="synthetic-local-token")
    monkeypatch.setattr(history, "read_keychain_token", keychain)
    calls = []

    def partial_submit(payload, token, timeout):
        calls.append(payload)
        if len(calls) == 2:
            return 500, b'{"received":3,"http_status":200,"detail":"do-not-echo"}'
        return receipt(payload, token, timeout)

    monkeypatch.setattr(history, "submit_payload", partial_submit)
    code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
    assert code == 4 and not stderr
    output = json.loads(stdout)
    assert output["status"] == "partial_failure" and output["history_complete"] is False
    assert output["completed_messages"] == 1
    assert "do-not-echo" not in stdout
    ledger = history.load_ledger(path)
    receipts = ledger["sources"]["chatgpt-radar-01"]
    assert set(receipts["messages"]) == {digest("newest")}
    assert len(receipts["items"]) == 20
    assert receipts["history_complete"] is False

    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
    assert code == 0 and not stderr
    assert json.loads(stdout)["history_complete"] is True
    assert submit.call_count == 1
    assert [j["external_id"] for j in submit.call_args.args[0]["jobs"]] == ["public-ats-11", "public-ats-12", "public-ats-13"]
    assert set(history.load_ledger(path)["sources"]["chatgpt-radar-01"]["messages"]) == {digest("newest"), digest("older")}


@pytest.mark.parametrize("response", [
    (200, b'{"received":0}'),
    (200, b'{"received":true}'),
    (200, b'{"accepted":1}'),
    (200, b'{"received":1,"pending":100}'),
    (200, b'{"received":1,"received":0}'),
    (500, b'{"received":1,"http_status":200}'),
    (200, b'not-json-private-content'),
])
def test_only_real_success_and_exact_received_advance_hashes(monkeypatch, tmp_path, response):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic")
    monkeypatch.setattr(history, "submit_payload", lambda *_args: response)
    code, stdout, stderr = run_main(monkeypatch, document(), ["--submit", "--ledger-file", str(path)])
    assert code == 4 and not stderr
    assert not path.exists()
    assert json.loads(stdout)["completed_messages"] == 0
    assert "private-content" not in stdout


def test_http_exception_never_exposes_url_headers_or_response_body(monkeypatch, tmp_path):
    error = urllib.error.HTTPError("https://private.example.com/?secret=private", 403, "synthetic-local-token", {}, io.BytesIO(b"private"))
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic-local-token")
    monkeypatch.setattr(history, "submit_payload", mock.Mock(side_effect=error))
    code, stdout, stderr = run_main(monkeypatch, document(), ["--submit", "--ledger-file", str(tmp_path / "ledger.json")])
    assert code == 4 and not stderr
    assert json.loads(stdout)["error"] == {"code": "http_error", "http_status": 403}
    assert "private" not in stdout and "synthetic" not in stdout


def test_repeated_history_and_new_message_with_identical_job_do_not_post_again(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    keychain = mock.Mock(return_value="synthetic")
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "read_keychain_token", keychain)
    monkeypatch.setattr(history, "submit_payload", submit)
    initial = document()
    assert run_main(monkeypatch, initial, ["--submit", "--ledger-file", str(path)])[0] == 0
    keychain.reset_mock()
    submit.reset_mock()
    for value in (initial, document([message("different-message-same-role", [job()]), *initial["messages"]])):
        code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
        assert code == 0 and not stderr
        assert json.loads(stdout)["history_complete"] is False
    keychain.assert_not_called()
    submit.assert_not_called()
    assert len(history.load_ledger(path)["sources"]["chatgpt-radar-01"]["messages"]) == 2


def test_source_isolation_and_changed_new_message_are_not_accidentally_skipped(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic")
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    inputs = [document(), document(source="chatgpt-radar-06"), document([message("newer", [job(requirements="更新的公开资格条件")]), *document()["messages"]])]
    for value in inputs:
        assert run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])[0] == 0
    assert submit.call_count == 3
    assert set(history.load_ledger(path)["sources"]) == {"chatgpt-radar-01", "chatgpt-radar-06"}


def test_disjoint_older_window_cannot_overwrite_newer_delivered_job(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic")
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    value = document([message("new-public-version", [job(requirements="现行公开要求")])])
    assert run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])[0] == 0
    submit.reset_mock()
    old_window = document([message("older-page-only", [job(requirements="旧版不同要求"), job(2)])])
    code, stdout, stderr = run_main(monkeypatch, old_window, ["--submit", "--ledger-file", str(path)])
    assert code == 0 and not stderr
    result = json.loads(stdout)
    assert result["held_reasons"]["unanchored_history_update"] == 1
    assert submit.call_count == 1
    assert [j["external_id"] for j in submit.call_args.args[0]["jobs"]] == ["public-ats-2"]
    assert digest("older-page-only") not in history.load_ledger(path)["sources"]["chatgpt-radar-01"]["messages"]


def test_legitimate_empty_rows_heartbeat_advances_only_after_received_zero(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic")
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    value = document([message("empty-one", []), message("empty-two", [])], complete=False)
    code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
    assert code == 0 and not stderr
    assert submit.call_count == 1
    assert submit.call_args.args[0] == {"source_id": "chatgpt-radar-01", "jobs": []}
    result = json.loads(stdout)
    assert result["completed_messages"] == 2 and result["history_complete"] is False
    assert len(history.load_ledger(path)["sources"]["chatgpt-radar-01"]["messages"]) == 2


def test_all_held_rows_do_not_send_empty_heartbeat_or_advance_message(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    value = document([message("old", [job(status="closed")])], complete=True)
    code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
    assert code == 0 and not stderr
    result = json.loads(stdout)
    assert result["status"] == "held" and result["held_rows"] == 1
    assert result["history_complete"] is False
    assert not path.exists()
    history.read_keychain_token.assert_not_called()
    history.submit_payload.assert_not_called()


def test_mixed_held_message_never_claims_full_history_complete(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic")
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    value = document([message("mixed", [job(1), job(2, status="closed")])], complete=True)
    code, stdout, stderr = run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])
    assert code == 0 and not stderr
    assert json.loads(stdout)["history_complete"] is False
    assert history.load_ledger(path)["sources"]["chatgpt-radar-01"]["messages"] == {}
    submit.reset_mock()
    assert run_main(monkeypatch, value, ["--submit", "--ledger-file", str(path)])[0] == 0
    submit.assert_not_called()


def test_digest_cannot_be_reused_to_smuggle_modified_rows_after_success(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(history, "read_keychain_token", lambda: "synthetic")
    submit = mock.Mock(side_effect=receipt)
    monkeypatch.setattr(history, "submit_payload", submit)
    assert run_main(monkeypatch, document(), ["--submit", "--ledger-file", str(path)])[0] == 0
    submit.reset_mock()
    altered = document([message("one", [job(2)])])
    code, stdout, stderr = run_main(monkeypatch, altered, ["--submit", "--ledger-file", str(path)])
    assert code == 2 and not stdout
    assert "different rows" in stderr
    submit.assert_not_called()


def test_ledger_rejects_private_or_forged_state_and_symlinks(tmp_path):
    malformed = {"version": 1, "sources": {"chatgpt-radar-01": {"raw_text": "private"}}}
    with pytest.raises(history.HistoryError):
        history._validate_ledger(malformed)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(empty_ledger()))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(history.HistoryError, match="symbolic link"):
        history.load_ledger(link)
    with pytest.raises(history.HistoryError, match="outside"):
        history._checked_ledger_path(history.SCRIPT_ROOT / "unsafe-ledger.json")


def test_nonblocking_local_ledger_lock_prevents_two_history_submitters(tmp_path):
    path = tmp_path / "ledger.json"
    with history.ledger_lock(path):
        with pytest.raises(history.HistoryError, match="already using"):
            with history.ledger_lock(path):
                raise AssertionError("second submitter acquired the same lock")
    with history.ledger_lock(path):
        pass


def test_ledger_capacity_failure_happens_before_remote_write(monkeypatch):
    monkeypatch.setattr(history, "MAX_LEDGER_HASHES", 1)
    with pytest.raises(history.HistoryError, match="size limit"):
        plan(document())
