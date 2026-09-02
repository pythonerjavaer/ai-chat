"""Durable leases for retrying quarantined recruitment candidates."""

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend import database


@pytest.fixture
def claim_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database,
        "settings",
        SimpleNamespace(
            database_backend="sqlite",
            database_path=tmp_path / "verification-claims.db",
        ),
    )
    database.init_db()


def candidate(index: int, **changes) -> dict:
    item = {
        "id": f"candidate-{'a' * 28}{index:04d}",
        "dedupe_key": f"dedupe-{index}",
        "source_key": "chatgpt-radar-01::",
        "source_id": "chatgpt-radar-01",
        "source_thread_id": None,
        "source_item_id": f"item-{index}",
        "external_id": f"role-{index}",
        "source_updated_at": "2026-09-01T00:00:00+00:00",
        "company": "核验测试集团",
        "employer_type": "重点雇主",
        "title": f"2027校园招聘分析岗{index}",
        "city": "上海",
        "industry": "金融",
        "official_url": f"https://careers.example.com/jobs/{index}",
        "canonical_url": f"https://careers.example.com/jobs/{index}",
        "source": "ChatGPT 监控 1",
        "opening_date": None,
        "closing_date": None,
        "requirements": "面向应届毕业生",
        "tags": ["校园招聘"],
        "evidence": [],
        "incoming_status": "open",
        "payload_hash": f"hash-{index}",
    }
    item.update(changes)
    return item


def promoted_job(job_id: str = "monitor-atomic-job", **changes) -> dict:
    item = {
        "id": job_id,
        "company": "核验测试集团",
        "employer_type": "重点雇主",
        "title": "2027校园招聘分析岗",
        "city": "上海",
        "industry": "金融",
        "url": "https://careers.example.com/jobs/atomic",
        "source": "ChatGPT 监控 1",
        "opening_date": None,
        "closing_date": None,
        "requirements": "面向应届毕业生",
        "tags": ["校园招聘", "链接已验证"],
        "historical_applicants": None,
        "historical_offers": None,
        "last_verified_at": "2026-09-03T11:00:00+00:00",
        "status": "open",
    }
    item.update(changes)
    return item


def test_claim_selects_only_due_pending_candidates_and_is_bounded(claim_db):
    for index in range(4):
        database.upsert_recruitment_ingest_candidate(candidate(index))
    database.set_recruitment_ingest_candidate_verification(
        candidate(1)["id"], "rejected", "not_campus"
    )
    database.set_recruitment_ingest_candidate_verification(
        candidate(2)["id"], "verified", None
    )
    future = "2026-09-03T12:00:00+00:00"
    database.set_recruitment_ingest_candidate_verification(
        candidate(3)["id"], "pending", "official_page_fetch_failed",
        next_verification_at=future,
    )

    token, rows = database.claim_pending_recruitment_ingest_candidates(
        limit=1,
        now=datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc),
    )

    assert token
    assert [row["id"] for row in rows] == [candidate(0)["id"]]
    assert rows[0]["verification_attempt_count"] == 1
    assert rows[0]["last_verification_attempt_at"] == "2026-09-03T11:00:00+00:00"


def test_verification_host_counts_are_bounded_and_omit_candidate_details(claim_db):
    first = database.upsert_recruitment_ingest_candidate(candidate(0))
    second = database.upsert_recruitment_ingest_candidate(candidate(
        1, canonical_url="https://www.careers.example.com/private/path?token=secret",
    ))
    database.set_recruitment_ingest_candidate_verification(
        first["id"], "pending", "page_missing_official_domain_evidence",
    )
    database.set_recruitment_ingest_candidate_verification(
        second["id"], "rejected", "not_campus",
    )

    rows = database.recruitment_ingest_verification_host_counts(limit=10)

    assert rows == [
        {
            "status": "pending",
            "reason": "page_missing_official_domain_evidence",
            "hostname": "careers.example.com",
            "count": 1,
        },
        {
            "status": "rejected",
            "reason": "not_campus",
            "hostname": "careers.example.com",
            "count": 1,
        },
    ]
    serialized = str(rows)
    assert "private/path" not in serialized
    assert "核验测试集团" not in serialized


def test_active_claim_is_not_duplicated_and_expired_claim_is_recoverable(claim_db):
    database.upsert_recruitment_ingest_candidate(candidate(0))
    started = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)

    first_token, first = database.claim_pending_recruitment_ingest_candidates(
        now=started, claim_ttl_seconds=60
    )
    second_token, second = database.claim_pending_recruitment_ingest_candidates(
        now=started + timedelta(seconds=30), claim_ttl_seconds=60
    )
    recovered_token, recovered = database.claim_pending_recruitment_ingest_candidates(
        now=started + timedelta(seconds=61), claim_ttl_seconds=60
    )

    assert first and first_token != second_token
    assert second == []
    assert [row["id"] for row in recovered] == [candidate(0)["id"]]
    assert recovered_token != first_token
    assert recovered[0]["verification_attempt_count"] == 2


def test_claim_one_candidate_is_atomic_and_respects_a_live_lease(claim_db):
    stored = database.upsert_recruitment_ingest_candidate(candidate(0))
    started = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)

    first = database.claim_recruitment_ingest_candidate(
        stored["id"], now=started, claim_ttl_seconds=60,
    )
    duplicate = database.claim_recruitment_ingest_candidate(
        stored["id"], now=started + timedelta(seconds=30), claim_ttl_seconds=60,
    )
    recovered = database.claim_recruitment_ingest_candidate(
        stored["id"], now=started + timedelta(seconds=61), claim_ttl_seconds=60,
    )

    assert first is not None
    assert duplicate is None
    assert recovered is not None
    assert recovered[0] != first[0]
    assert recovered[1]["verification_attempt_count"] == 2


def test_ingest_upsert_can_create_the_verification_lease_atomically(claim_db):
    stored = database.upsert_recruitment_ingest_candidate(
        candidate(0), claim_for_verification=True,
    )

    assert stored["claimed_verification_token"]
    assert stored["verification_claim_token"] == stored["claimed_verification_token"]
    assert database.claim_pending_recruitment_ingest_candidates(limit=10)[1] == []


def test_explicit_source_replay_can_recheck_terminal_or_not_yet_due_candidate(claim_db):
    verified = database.upsert_recruitment_ingest_candidate(candidate(0))
    database.set_recruitment_ingest_candidate_verification(
        verified["id"], "verified", None,
    )
    assert database.claim_recruitment_ingest_candidate(verified["id"]) is None
    assert database.claim_recruitment_ingest_candidate(
        verified["id"], recheck_terminal=True, ignore_retry_time=True,
    ) is not None

    pending = database.upsert_recruitment_ingest_candidate(candidate(1))
    database.set_recruitment_ingest_candidate_verification(
        pending["id"], "pending", "official_page_fetch_failed",
        next_verification_at="2099-09-03T12:00:00+00:00",
    )
    assert database.claim_recruitment_ingest_candidate(pending["id"]) is None
    assert database.claim_recruitment_ingest_candidate(
        pending["id"], recheck_terminal=True, ignore_retry_time=True,
    ) is not None


def test_changed_payload_invalidates_old_worker_and_can_be_reclaimed(claim_db):
    original = database.upsert_recruitment_ingest_candidate(candidate(0))
    old_claim = database.claim_recruitment_ingest_candidate(original["id"])
    assert old_claim is not None

    changed = candidate(0, payload_hash="replacement", title="2027校园招聘战略岗")
    stored = database.upsert_recruitment_ingest_candidate(changed)
    replacement_claim = database.claim_recruitment_ingest_candidate(stored["id"])

    assert stored["disposition"] == "updated"
    assert replacement_claim is not None
    assert replacement_claim[0] != old_claim[0]
    assert database.finalize_recruitment_ingest_candidate_verification(
        stored["id"], "verified", None,
        claim_token=old_claim[0], promoted_job=promoted_job(),
    ) is None
    assert database.list_recruitment_jobs() == []


def test_release_requires_owner_and_honours_next_retry_time(claim_db):
    database.upsert_recruitment_ingest_candidate(candidate(0))
    started = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    token, rows = database.claim_pending_recruitment_ingest_candidates(now=started)
    assert rows

    assert not database.release_recruitment_ingest_candidate_verification_claim(
        candidate(0)["id"], "not-the-owner"
    )
    assert database.release_recruitment_ingest_candidate_verification_claim(
        candidate(0)["id"], token,
        next_verification_at="2026-09-03T12:00:00+00:00",
    )
    assert database.claim_pending_recruitment_ingest_candidates(
        now=started + timedelta(minutes=30)
    )[1] == []
    assert database.claim_pending_recruitment_ingest_candidates(
        now=started + timedelta(hours=1)
    )[1]


def test_changed_payload_resets_a_prior_review_and_retry_schedule(claim_db):
    original = candidate(0)
    database.upsert_recruitment_ingest_candidate(original)
    token, rows = database.claim_pending_recruitment_ingest_candidates(
        now=datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    )
    assert rows and token
    database.set_recruitment_ingest_candidate_verification(
        original["id"], "rejected", "not_campus"
    )

    changed = {**original, "requirements": "面向2027届应届毕业生", "payload_hash": "changed"}
    stored = database.upsert_recruitment_ingest_candidate(changed)

    assert stored["disposition"] == "updated"
    assert stored["verification_status"] == "pending"
    assert stored["verification_reason"] is None
    assert stored["verification_attempt_count"] == 0
    assert stored["last_verification_attempt_at"] is None
    assert stored["next_verification_at"] is None
    assert stored["verification_claim_token"] is None
    assert stored["verification_claimed_at"] is None


def test_expired_worker_cannot_overwrite_a_newer_claim(claim_db):
    database.upsert_recruitment_ingest_candidate(candidate(0))
    started = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    stale_token, rows = database.claim_pending_recruitment_ingest_candidates(
        now=started, claim_ttl_seconds=60
    )
    assert rows
    current_token, rows = database.claim_pending_recruitment_ingest_candidates(
        now=started + timedelta(seconds=61), claim_ttl_seconds=60
    )
    assert rows and current_token != stale_token

    stale_result = database.set_recruitment_ingest_candidate_verification(
        candidate(0)["id"], "rejected", "stale_worker",
        claim_token=stale_token,
    )
    assert stale_result is None
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT verification_status, verification_claim_token "
            "FROM recruitment_ingest_candidates WHERE id=?",
            (candidate(0)["id"],),
        ).fetchone()
    assert tuple(stored) == ("pending", current_token)

    current_result = database.set_recruitment_ingest_candidate_verification(
        candidate(0)["id"], "pending", "official_page_fetch_failed",
        next_verification_at="2026-09-03T12:00:00+00:00",
        claim_token=current_token,
    )
    assert current_result["verification_reason"] == "official_page_fetch_failed"
    assert current_result["verification_claim_token"] is None


def test_atomic_finalize_commits_candidate_and_promoted_job_together(claim_db):
    stored = database.upsert_recruitment_ingest_candidate(candidate(0))
    claimed = database.claim_recruitment_ingest_candidate(stored["id"])
    assert claimed is not None
    token, _ = claimed

    result = database.finalize_recruitment_ingest_candidate_verification(
        stored["id"],
        "verified",
        None,
        claim_token=token,
        promoted_job=promoted_job(),
        verified_opening_date="2026-08-01",
        verified_closing_date="2026-10-01",
    )

    assert result is not None
    assert result["verification_status"] == "verified"
    assert result["promoted_job_id"] == "monitor-atomic-job"
    assert result["verification_claim_token"] is None
    assert result["verified_opening_date"] == "2026-08-01"
    assert [job["id"] for job in database.list_recruitment_jobs()] == [
        "monitor-atomic-job"
    ]


def test_atomic_finalize_rolls_back_candidate_when_job_upsert_fails(claim_db):
    stored = database.upsert_recruitment_ingest_candidate(candidate(0))
    claimed = database.claim_recruitment_ingest_candidate(stored["id"])
    assert claimed is not None
    token, _ = claimed

    with pytest.raises(sqlite3.IntegrityError):
        database.finalize_recruitment_ingest_candidate_verification(
            stored["id"], "verified", None,
            claim_token=token,
            promoted_job=promoted_job(company=None),
        )

    with database.connect() as connection:
        row = connection.execute(
            "SELECT verification_status, verification_claim_token "
            "FROM recruitment_ingest_candidates WHERE id=?",
            (stored["id"],),
        ).fetchone()
        jobs = connection.execute("SELECT COUNT(*) AS total FROM recruitment_jobs").fetchone()
    assert tuple(row) == ("pending", token)
    assert jobs["total"] == 0


def test_stale_finalize_cannot_touch_an_existing_public_job(claim_db):
    stored = database.upsert_recruitment_ingest_candidate(candidate(0))
    started = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    stale = database.claim_recruitment_ingest_candidate(
        stored["id"], now=started, claim_ttl_seconds=60,
    )
    current = database.claim_recruitment_ingest_candidate(
        stored["id"], now=started + timedelta(seconds=61), claim_ttl_seconds=60,
    )
    assert stale is not None and current is not None
    database.upsert_recruitment_jobs([promoted_job(title="Original title")])

    result = database.finalize_recruitment_ingest_candidate_verification(
        stored["id"], "verified", None,
        claim_token=stale[0],
        promoted_job=promoted_job(title="Stale worker title"),
    )

    assert result is None
    with database.connect() as connection:
        job = connection.execute(
            "SELECT title, status FROM recruitment_jobs WHERE id=?",
            ("monitor-atomic-job",),
        ).fetchone()
    assert tuple(job) == ("Original title", "open")


def test_reject_only_closes_job_after_last_verified_backing_is_gone(claim_db):
    first = database.upsert_recruitment_ingest_candidate(candidate(0))
    second = database.upsert_recruitment_ingest_candidate(
        candidate(
            1,
            canonical_url=first["canonical_url"],
            official_url=first["official_url"],
            source_key="chatgpt-radar-02::",
            source_id="chatgpt-radar-02",
        )
    )
    job = promoted_job()
    for stored in (first, second):
        claimed = database.claim_recruitment_ingest_candidate(stored["id"])
        assert claimed is not None
        finalized = database.finalize_recruitment_ingest_candidate_verification(
            stored["id"], "verified", None,
            claim_token=claimed[0], promoted_job=job,
        )
        assert finalized is not None

    first = database.upsert_recruitment_ingest_candidate({
        **candidate(0), "payload_hash": "first-removed",
    })
    first_claim = database.claim_recruitment_ingest_candidate(first["id"])
    assert first_claim is not None
    first_result = database.finalize_recruitment_ingest_candidate_verification(
        first["id"], "rejected", "not_campus", claim_token=first_claim[0],
    )
    assert first_result is not None and not first_result["job_closed"]
    assert database.list_recruitment_jobs()[0]["status"] == "open"

    second = database.upsert_recruitment_ingest_candidate({
        **candidate(
            1,
            canonical_url=first["canonical_url"],
            official_url=first["official_url"],
            source_key="chatgpt-radar-02::",
            source_id="chatgpt-radar-02",
        ),
        "payload_hash": "second-removed",
    })
    second_claim = database.claim_recruitment_ingest_candidate(second["id"])
    assert second_claim is not None
    second_result = database.finalize_recruitment_ingest_candidate_verification(
        second["id"], "closed", "expired", claim_token=second_claim[0],
    )
    assert second_result is not None and second_result["job_closed"]
    with database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM recruitment_jobs WHERE id=?", (job["id"],),
        ).fetchone()["status"]
    assert status == "closed"


def test_transient_pending_last_known_good_still_backs_shared_job(claim_db):
    first = database.upsert_recruitment_ingest_candidate(candidate(0))
    second = database.upsert_recruitment_ingest_candidate(candidate(
        1,
        canonical_url=first["canonical_url"],
        official_url=first["official_url"],
        source_key="chatgpt-radar-02::",
        source_id="chatgpt-radar-02",
    ))
    job = promoted_job()
    for stored in (first, second):
        claimed = database.claim_recruitment_ingest_candidate(stored["id"])
        assert claimed is not None
        database.finalize_recruitment_ingest_candidate_verification(
            stored["id"], "verified", None,
            claim_token=claimed[0], promoted_job=job,
        )

    transient_claim = database.claim_recruitment_ingest_candidate(
        first["id"], recheck_terminal=True, ignore_retry_time=True,
    )
    assert transient_claim is not None
    database.finalize_recruitment_ingest_candidate_verification(
        first["id"], "pending", "official_page_fetch_failed",
        claim_token=transient_claim[0],
        promoted_job_id=job["id"],
        next_verification_at="2099-09-03T12:00:00+00:00",
    )

    second_changed = database.upsert_recruitment_ingest_candidate({
        **candidate(
            1,
            canonical_url=first["canonical_url"],
            official_url=first["official_url"],
            source_key="chatgpt-radar-02::",
            source_id="chatgpt-radar-02",
        ),
        "payload_hash": "second-rejected",
    })
    second_claim = database.claim_recruitment_ingest_candidate(second_changed["id"])
    assert second_claim is not None
    result = database.finalize_recruitment_ingest_candidate_verification(
        second_changed["id"], "rejected", "not_campus",
        claim_token=second_claim[0],
    )

    assert result is not None and not result["job_closed"]
    assert database.list_recruitment_jobs()[0]["status"] == "open"


def test_canonical_backing_protects_all_job_ids_until_last_candidate_closes(claim_db):
    canonical_url = "https://careers.example.com/jobs/shared-canonical"
    first = database.upsert_recruitment_ingest_candidate(candidate(
        0, canonical_url=canonical_url, official_url=canonical_url,
    ))
    second = database.upsert_recruitment_ingest_candidate(candidate(
        1,
        canonical_url=canonical_url,
        official_url=canonical_url,
        source_key="chatgpt-radar-02::",
        source_id="chatgpt-radar-02",
    ))
    for stored, job_id in ((first, "monitor-job-a"), (second, "monitor-job-b")):
        claimed = database.claim_recruitment_ingest_candidate(stored["id"])
        assert claimed is not None
        database.finalize_recruitment_ingest_candidate_verification(
            stored["id"], "verified", None,
            claim_token=claimed[0], promoted_job=promoted_job(job_id),
        )

    first = database.upsert_recruitment_ingest_candidate({
        **candidate(0, canonical_url=canonical_url, official_url=canonical_url),
        "payload_hash": "changed-a",
    })
    claim_a = database.claim_recruitment_ingest_candidate(first["id"])
    assert claim_a is not None
    database.finalize_recruitment_ingest_candidate_verification(
        first["id"], "rejected", "not_campus", claim_token=claim_a[0],
    )
    assert {job["id"] for job in database.list_recruitment_jobs()} == {
        "monitor-job-a", "monitor-job-b",
    }

    second = database.upsert_recruitment_ingest_candidate({
        **candidate(
            1,
            canonical_url=canonical_url,
            official_url=canonical_url,
            source_key="chatgpt-radar-02::",
            source_id="chatgpt-radar-02",
        ),
        "payload_hash": "changed-b",
    })
    claim_b = database.claim_recruitment_ingest_candidate(second["id"])
    assert claim_b is not None
    result = database.finalize_recruitment_ingest_candidate_verification(
        second["id"], "rejected", "not_campus", claim_token=claim_b[0],
    )
    assert result is not None and result["closed_job_count"] == 2
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, status FROM recruitment_jobs ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["status"]) for row in rows] == [
        ("monitor-job-a", "closed"), ("monitor-job-b", "closed"),
    ]


def test_reason_counts_are_source_scoped_and_unknown_values_are_redacted(claim_db):
    first = candidate(0)
    second = candidate(
        1,
        source_key="chatgpt-radar-02::",
        source_id="chatgpt-radar-02",
    )
    third = candidate(2)
    for item in (first, second, third):
        database.upsert_recruitment_ingest_candidate(item)
    database.set_recruitment_ingest_candidate_verification(
        first["id"], "pending", "official_page_fetch_failed"
    )
    database.set_recruitment_ingest_candidate_verification(
        second["id"], "rejected", "not_campus"
    )
    database.set_recruitment_ingest_candidate_verification(
        third["id"], "pending", "provider-secret-shaped-detail"
    )

    counts = database.recruitment_ingest_verification_reason_counts(
        source_ids=["chatgpt-radar-01"]
    )

    assert counts == {
        "pending": {"official_page_fetch_failed": 1, "other": 1},
        "rejected": {},
    }
    assert "provider-secret-shaped-detail" not in str(counts)
