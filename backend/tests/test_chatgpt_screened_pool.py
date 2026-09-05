"""User-screened ChatGPT observations bypass employer-fetch gating, not safety."""

from datetime import date, timedelta

import pytest

from backend import database, main
from backend.chatgpt_screening import chatgpt_screening_eligible
from backend.tests.test_recruitment_ingest_bridge import (
    harness, payload as monitor_payload, INGEST_HEADERS,
)


def payload(key="pending-role"):
    return {**monitor_payload(key), "source_id": "chatgpt-radar-01"}


def pool(harness):
    response = harness.client.get("/api/future-radar/opportunities", headers=harness.bearer)
    assert response.status_code == 200
    return response.json()


def test_chatgpt_recommendation_enters_clickable_pool_without_official_fetch(harness, monkeypatch):
    def forbidden_fetch(_candidate):
        pytest.fail("ChatGPT-screened ingest must not synchronously fetch an employer page")
    monkeypatch.setattr(main, "_verify_ingest_candidate", forbidden_fetch)
    data = payload()
    data["jobs"][0]["source_rating"] = {"scope": "job", "tier_code": "T1", "score": 86}
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=data)
    assert response.status_code == 200, response.text
    assert response.json()["source_screened"] == 1
    assert response.json()["pending"] == response.json()["accepted"] == 0
    result = pool(harness)
    assert result["total"] == 1
    job = result["items"][0]
    assert job["verification_status"] == "source_screened"
    assert job["review_label"] == "ChatGPT 已筛选"
    assert job["officially_verified"] is False
    assert job["is_candidate"] is False
    assert job["published_as_active_job"] is True
    assert job["tier_code"] == "T1"
    assert job["job_score"] == 86
    assert job["official_url"] == data["jobs"][0]["official_url"]
    detail = harness.client.get(f"/api/future-radar/opportunities/{job['id']}", headers=harness.bearer)
    assert detail.status_code == 200
    summary = main.public_chatgpt_sync_status()
    assert summary["inventory_source_screened"] == 1
    assert summary["inventory_pending"] == summary["inventory_accepted"] == 0
    assert summary["inventory_total"] == 1
    with database.connect() as connection:
        stored = dict(connection.execute("SELECT * FROM recruitment_ingest_candidates").fetchone())
        assert stored["verified_at"] is None
        assert stored["next_verification_at"] is None
        assert connection.execute("SELECT COUNT(*) FROM recruitment_jobs").fetchone()[0] == 0
    _, retries = database.claim_pending_recruitment_ingest_candidates(ignore_retry_time=True)
    assert retries == []


def test_replay_is_idempotent_and_closed_update_stays_out(harness, monkeypatch):
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _candidate: pytest.fail("unexpected HTTP verification"))
    data = payload()
    for _ in range(2):
        response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=data)
        assert response.json()["source_screened"] == 1
    assert pool(harness)["total"] == 1
    data["jobs"][0]["status"] = "closed"
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=data)
    assert response.json()["closed"] == 1
    assert pool(harness)["total"] == 0


@pytest.mark.parametrize("url", ["http://careers.example.com/job", "https://127.0.0.1/job", "https://chatgpt.com/"])
def test_unsafe_or_private_links_do_not_gain_screened_status(harness, url):
    data = payload()
    data["jobs"][0]["official_url"] = url
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=data)
    assert response.status_code in {200, 422}
    if response.status_code == 200:
        assert response.json()["rejected"] == 1
        assert response.json()["source_screened"] == 0
    assert pool(harness)["total"] == 0


def test_other_sources_still_need_verification_and_label_does_not_confer_trust(harness):
    data = payload()
    data["source_id"] = "other-monitor"
    data["jobs"][0]["source"] = "ChatGPT 已筛选"
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=data)
    assert response.json()["pending"] == 1
    assert response.json()["source_screened"] == 0
    assert pool(harness)["items"][0]["verification_status"] == "pending"


def test_historical_pending_adoption_is_local_idempotent_and_preserves_exclusions(harness, monkeypatch):
    def forbidden_fetch(_candidate):
        pytest.fail("historical adoption must not perform network verification")
    monkeypatch.setattr(main, "_verify_ingest_candidate", forbidden_fetch)
    saved = []
    for key, status, url, deadline in (
        ("eligible", "pending", "https://careers.example.com/eligible", None),
        ("rejected", "rejected", "https://careers.example.com/rejected", None),
        ("closed", "closed", "https://careers.example.com/closed", None),
        ("unsafe", "pending", "https://127.0.0.1/private", None),
        ("expired", "pending", "https://careers.example.com/expired", date.today() - timedelta(days=1)),
    ):
        item = main.RecruitmentIngestJob(**{**payload(key)["jobs"][0], "source_id": "chatgpt-radar-01",
                                          "official_url": url, "closing_date": deadline})
        candidate, _ = main._candidate_from_ingest_item(item)
        stored = database.upsert_recruitment_ingest_candidate(candidate)
        if status != "pending":
            database.set_recruitment_ingest_candidate_verification(stored["id"], status, "fixture")
        saved.append(stored)
    # Create the legacy pending projection before adopting the user's rule.
    harness.service.run(trigger_type="fixture", scan_type="quick", source_ids=["legacy-search-discovery"])
    assert database.adopt_chatgpt_screened_candidates(batch_size=2) == 1
    assert database.adopt_chatgpt_screened_candidates(batch_size=2) == 0
    with database.connect() as connection:
        states = {row["external_id"]: row["verification_status"] for row in connection.execute(
            "SELECT external_id, verification_status FROM recruitment_ingest_candidates"
        )}
    assert states == {"eligible": "source_screened", "rejected": "rejected", "closed": "closed",
                      "unsafe": "pending", "expired": "pending"}
    # Existing IDs and their persisted projection become visible immediately,
    # without requiring another user-triggered Quick Scan.
    visible = pool(harness)["items"]
    screened = [job for job in visible if job["verification_status"] == "source_screened"]
    assert len(screened) == 1
    assert screened[0]["officially_verified"] is False


def test_missing_concrete_identity_cannot_pass_local_screening():
    candidate = {"source_id": "chatgpt-radar-01", "company": "", "title": "分析师",
                 "official_url": "https://careers.example.com/job"}
    assert chatgpt_screening_eligible(candidate) is False


def test_restore_projects_missing_durable_candidates_without_clicking_scan(harness, monkeypatch):
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _candidate: pytest.fail("unexpected external verification"))
    item = main.RecruitmentIngestJob(**{**payload("missing-projection")["jobs"][0], "source_id": "chatgpt-radar-01"})
    candidate, _ = main._candidate_from_ingest_item(item)
    database.upsert_recruitment_ingest_candidate(candidate)
    assert pool(harness)["total"] == 0
    assert main.restore_chatgpt_screened_opportunities() == {"adopted": 1, "projected": 1, "status": "success"}
    assert pool(harness)["total"] == 1
    assert main.restore_chatgpt_screened_opportunities() == {"adopted": 0, "projected": 0, "status": "success"}


def test_changed_officially_verified_content_is_screened_without_reusing_old_proof(harness, monkeypatch):
    data = payload("verified-then-changed")
    item = main.RecruitmentIngestJob(**{**data["jobs"][0], "source_id": data["source_id"]})
    candidate, _ = main._candidate_from_ingest_item(item)
    stored = database.upsert_recruitment_ingest_candidate(candidate, claim_for_verification=True)
    database.finalize_recruitment_ingest_candidate_verification(
        stored["id"], "verified", None, claim_token=stored["claimed_verification_token"],
        promoted_job=main._promoted_job(stored),
    )
    monkeypatch.setattr(main, "_verify_ingest_candidate", lambda _candidate: pytest.fail("unexpected external verification"))
    data["jobs"][0]["title"] = "新的校园招聘产品经理"
    response = harness.client.post("/api/recruitment/ingest", headers=INGEST_HEADERS, json=data)
    assert response.json()["source_screened"] == 1
    assert response.json()["accepted"] == 0
