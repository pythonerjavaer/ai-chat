"""Fresh CLI processes must not inherit pytest's synthetic app credentials."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def clean_environment():
    # Do not copy os.environ: conftest intentionally supplies app secrets for
    # unrelated API tests. An empty PATH also excludes the Keychain executable.
    return {
        "PATH": "",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
    }


def public_row():
    return {
        "external_id": "offline-cli-public-job",
        "company": "示例雇主",
        "title": "2027届校园招聘分析师",
        "city": "上海",
        "official_url": "https://careers.example.com/jobs/offline-cli",
        "opening_date": None,
        "closing_date": None,
        "evidence": ["公开招聘测试资料，仅用于离线校验。"],
    }


def history_input():
    return {
        "source_id": "chatgpt-radar-01",
        "history_complete": False,
        "messages": [{
            "message_digest": hashlib.sha256(b"offline-cli-message").hexdigest(),
            "rows": [public_row()],
        }],
    }


def run_cli(script, args, value):
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / script), *args],
        input=json.dumps(value, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        cwd=PROJECT_ROOT,
        env=clean_environment(),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


@pytest.mark.parametrize("mode", ["--dry-run", "--emit"])
def test_history_real_cli_without_openai_jwt_or_database_settings(tmp_path, mode):
    ledger = tmp_path / "not-created" / "history.json"
    result = run_cli(
        "frostfire_chatgpt_history.py", [mode, "--ledger-file", str(ledger)], history_input(),
    )
    assert not ledger.parent.exists()
    if mode == "--dry-run":
        assert result["dry_run"] is True
        assert result["eligible_rows"] == 1
        assert result["history_complete"] is False
    else:
        assert len(result) == 1
        assert result[0]["source_id"] == "chatgpt-radar-01"
        assert result[0]["jobs"][0]["external_id"] == public_row()["external_id"]
        assert "status" not in result[0]["jobs"][0]


@pytest.mark.parametrize("mode", ["--dry-run", None])
def test_single_message_bridge_real_cli_dry_run_and_emission_are_offline(tmp_path, mode):
    cursor = tmp_path / "not-created" / "cursor.json"
    result = run_cli(
        "frostfire_chatgpt_bridge.py",
        ([mode] if mode else []) + ["--cursor-file", str(cursor)],
        {"source_id": "chatgpt-radar-01", "message_id": "offline-message", "rows": [public_row()]},
    )
    assert not cursor.parent.exists()
    if mode:
        assert result["dry_run"] is True
        assert result["rows"] == 1
    else:
        assert result["version"] == "FROSTFIRE_SYNC_V1"
        assert result["source_id"] == "chatgpt-radar-01"
        assert result["jobs"][0]["verification_status"] == "pending"


def test_structured_source_import_real_cli_does_not_require_application_config():
    result = run_cli(
        "frostfire_source_import.py", ["--source-id", "public-test-source", "--structured-json", "-"],
        {"version": "FROSTFIRE_SYNC_V1", "source_id": "public-test-source", "jobs": [public_row()]},
    )
    assert result["source_id"] == "public-test-source"
    assert result["jobs"][0]["external_id"] == public_row()["external_id"]


def test_importing_offline_tools_does_not_load_application_or_ai_modules():
    code = """
import sys
from scripts import frostfire_chatgpt_history, frostfire_chatgpt_bridge, frostfire_source_import
for name in ('backend.config', 'backend.database', 'backend.future_radar.adapters', 'openai'):
    assert name not in sys.modules, name
print('offline-imports-ok')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, env=clean_environment(),
        text=True, capture_output=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "offline-imports-ok"


def test_history_mocked_submit_requires_no_application_credentials(tmp_path):
    # Exercise submit in a genuinely fresh, credential-free process. Only its
    # Keychain/HTTP boundary is replaced; the CLI, every ingest dry-run, receipt
    # checks and hash-only ledger remain real. Never access a real account.
    code = """
import json, sys
from scripts import frostfire_chatgpt_history as history
history.read_keychain_token = lambda: 'synthetic-ingest-token'
history.submit_payload = lambda payload, token, timeout: (
    200, json.dumps({'received': len(payload['jobs']), 'pending': len(payload['jobs'])}).encode()
)
raise SystemExit(history.main(['--submit', '--ledger-file', sys.argv[1]]))
"""
    ledger = tmp_path / "private" / "history.json"
    completed = subprocess.run(
        [sys.executable, "-c", code, str(ledger)], cwd=PROJECT_ROOT, env=clean_environment(),
        input=json.dumps(history_input(), ensure_ascii=False), text=True,
        capture_output=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["successful_batches"] == 1
    assert result["results"][0]["received"] == 1
    assert result["history_complete"] is False
    assert ledger.exists()
    assert "synthetic-ingest-token" not in completed.stdout
