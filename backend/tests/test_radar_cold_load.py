"""Avoid unbounded legacy work without weakening public-data safeguards."""

import pytest

from backend.future_radar import adapters
from backend.future_radar.normalization import clean_text


def legacy_redaction(value, limit):
    text = clean_text(value, limit=1_500_000)
    text = adapters._PUBLIC_EMAIL.sub("[redacted-email]", text)
    for pattern in adapters._PUBLIC_PHONES:
        text = pattern.sub("[redacted-phone]", text)
    for pattern in adapters._PUBLIC_SECRETS:
        text = pattern.sub("[redacted-secret]", text)
    text = adapters._PUBLIC_UUID.sub("[redacted-uuid]", text)
    return clean_text(text, limit=limit)


@pytest.mark.parametrize("value", [
    "岗位职责与任职条件" * 2000,
    "Long contact-free job requirements " * 1000,
    "联系测试 person+jobs@example.invalid 或 13812345678",
    "＠candidate＠example.invalid email test@test.invalid",
    "a" * 310 + " token=sample-no-production-key " + "b" * 300,
    "岗位" * 158 + " sk-proj-0123456789abcdefghijklmnopqrstuv",
    "标识 12345678-abcd-1234-abcd-123456789abc",
    "source https://example.invalid/jobs?email=test%40example.invalid",
    None, "", "prefix@missing-domain", "\u200b岗位\ufeff",
])
@pytest.mark.parametrize("limit", [0, 80, 320, 2000])
def test_contact_prefilter_matches_full_legacy_redaction(value, limit):
    assert adapters._redact_public_text(value, limit=limit) == legacy_redaction(value, limit)


def test_contact_free_text_never_invokes_email_regex(monkeypatch):
    class Forbidden:
        def sub(self, *_args):
            pytest.fail("email regex scanned text which cannot contain an email")

    monkeypatch.setattr(adapters, "_PUBLIC_EMAIL", Forbidden())
    assert adapters._redact_public_text("公开岗位职责" * 1000, limit=320)


def test_summary_status_does_not_read_or_score_legacy_jobs(monkeypatch):
    from backend import main

    def forbidden(*_args, **_kwargs):
        pytest.fail("summary-only status loaded/scored the legacy pool")

    for field in ("get_recruitment_profile", "list_recruitment_jobs", "recruitment_job_summary"):
        monkeypatch.setattr(main.database, field, forbidden)
    monkeypatch.setattr(main, "score_job", forbidden)
    sync = {"transport_state": "synced", "last_synced_at": "2026-10-02T01:00:00+00:00"}
    watches = {"total": 2, "enabled": 2}
    monkeypatch.setattr(main, "public_chatgpt_sync_status", lambda: sync)
    monkeypatch.setattr(main.database, "recruitment_watch_summary", lambda user_id: watches if user_id == 42 else forbidden())
    assert main.recruitment_jobs({"id": 42}, summary_only=True) == {
        "jobs": [], "summary_only": True,
        "data_status": {"mode": "radar_status_summary", "chatgpt_sync": sync, "watches": watches},
    }
