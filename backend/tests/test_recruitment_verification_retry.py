"""Pending ChatGPT candidates are retried and promoted without duplicate fetches."""

import asyncio
from types import SimpleNamespace

import pytest

from backend import database, main
from backend.future_radar.service import RadarRunBusy
from backend.recruitment_watch import WatchFetchError


@pytest.fixture
def retry_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database,
        "settings",
        SimpleNamespace(
            database_backend="sqlite",
            database_path=tmp_path / "verification-retry.db",
        ),
    )
    database.init_db()


def stored_candidate(source_id: str, *, external_id: str = "shared-role", **changes):
    item = main.RecruitmentIngestJob(**{
        "source_id": source_id,
        "external_id": external_id,
        "company": "复核科技",
        "title": "2027校园招聘数据分析岗",
        "city": "上海",
        "employer_type": "互联网企业",
        "industry": "科技",
        "official_url": "https://example.zhiye.com/jobs/shared-role",
        "requirements": "面向2027届应届毕业生",
        "tags": ["校园招聘"],
        **changes,
    })
    candidate, url_error = main._candidate_from_ingest_item(item)
    assert url_error is None
    return database.upsert_recruitment_ingest_candidate(candidate)


def test_retry_fetches_shared_page_once_and_promotes_cross_source_candidates(
    retry_db, monkeypatch,
):
    first = stored_candidate("chatgpt-radar-01")
    second = stored_candidate("chatgpt-radar-02")
    fetches = []

    def fetch(url, *_args, **_kwargs):
        fetches.append(url)
        return SimpleNamespace(
            text=(
                "复核科技 2027校园招聘数据分析岗 工作地点上海 "
                "面向2027届应届毕业生 立即申请"
            ),
            final_url=url,
        )

    monkeypatch.setattr(main, "fetch_watch_page", fetch)
    result = main.reverify_pending_recruitment_candidates(limit=10)

    assert result["claimed"] == result["checked"] == result["verified"] == 2
    assert result["fetches"] == len(fetches) == 1
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, verification_status, promoted_job_id "
            "FROM recruitment_ingest_candidates ORDER BY id"
        ).fetchall()
    assert {row["id"] for row in rows} == {first["id"], second["id"]}
    assert {row["verification_status"] for row in rows} == {"verified"}
    assert len({row["promoted_job_id"] for row in rows}) == 1
    assert len(database.list_recruitment_jobs()) == 1


def test_transient_failure_is_released_with_source_level_retry_time(
    retry_db, monkeypatch,
):
    stored_candidate("chatgpt-radar-01")

    def unavailable(*_args, **_kwargs):
        raise WatchFetchError("temporary")

    monkeypatch.setattr(main, "fetch_watch_page", unavailable)
    first = main.reverify_pending_recruitment_candidates(limit=10)
    second = main.reverify_pending_recruitment_candidates(limit=10)

    assert first["pending"] == first["checked"] == 1
    assert first["reason_counts"] == {"official_page_fetch_failed": 1}
    assert second["claimed"] == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT verification_status, verification_reason, next_verification_at, "
            "verification_claim_token FROM recruitment_ingest_candidates"
        ).fetchone()
    assert row["verification_status"] == "pending"
    assert row["verification_reason"] == "official_page_fetch_failed"
    assert row["next_verification_at"]
    assert row["verification_claim_token"] is None


def test_hard_noncampus_rejection_never_fetches_official_page(retry_db, monkeypatch):
    stored_candidate(
        "chatgpt-radar-01",
        external_id="social-role",
        title="社会招聘高级经理",
        requirements="仅限社会招聘",
        tags=["社会招聘"],
    )
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: pytest.fail("hard rejection must not fetch"),
    )

    result = main.reverify_pending_recruitment_candidates(limit=10)

    assert result["rejected"] == result["checked"] == 1
    assert result["reason_counts"] == {"not_campus": 1}
    assert database.recruitment_ingest_verification_reason_counts()["rejected"] == {
        "not_campus": 1
    }


def test_process_retry_lock_prevents_a_second_worker_from_claiming_more_rows(
    retry_db, monkeypatch,
):
    stored_candidate("chatgpt-radar-01")
    monkeypatch.setattr(
        main,
        "fetch_watch_page",
        lambda *_args, **_kwargs: pytest.fail("a busy worker must not fetch"),
    )

    assert main._recruitment_verification_retry_lock.acquire(blocking=False)
    try:
        result = main.reverify_pending_recruitment_candidates(limit=10)
    finally:
        main._recruitment_verification_retry_lock.release()

    assert result["busy"] is True
    assert result["claimed"] == result["checked"] == 0


def test_manual_radar_acquires_run_lock_before_best_effort_retry(monkeypatch):
    order = []
    monkeypatch.setattr(main, "_future_radar_run_sources", lambda _payload: ["source"])
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(
        run=lambda **_kwargs: order.append("radar") or {"status": "success"},
    ))

    def broken_retry(*, limit):
        assert limit == 40
        order.append("retry")
        raise RuntimeError("retry unavailable")

    monkeypatch.setattr(main, "reverify_pending_recruitment_candidates", broken_retry)
    result = asyncio.run(
        main.run_future_radar(None, main.RadarRunRequest(scan_type="quick"), None)
    )

    assert order == ["radar", "retry"]
    assert result["status"] == "success"
    assert result["verification_retry"]["status"] == "error"


def test_busy_manual_radar_does_not_start_candidate_fetches(monkeypatch):
    monkeypatch.setattr(main, "_future_radar_run_sources", lambda _payload: ["source"])

    def busy(**_kwargs):
        raise RadarRunBusy("busy", scan_type="quick")

    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(run=busy))
    monkeypatch.setattr(
        main,
        "reverify_pending_recruitment_candidates",
        lambda **_kwargs: pytest.fail("busy Radar must not start verification retry"),
    )

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.run_future_radar(None, main.RadarRunRequest(), None))

    assert exc.value.status_code == 409


def test_scheduled_retry_failure_does_not_block_radar_or_scheduler_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(
        run=lambda **_kwargs: calls.append("radar") or {
            "id": "run", "status": "success", "sources_succeeded": 1,
            "sources_checked": 1,
        },
    ))

    def broken_retry(*, limit):
        assert limit == 100
        calls.append("retry")
        raise RuntimeError("retry unavailable")

    async def stop_after_iteration(_seconds):
        calls.append("sleep")
        raise StopAsyncIteration

    monkeypatch.setattr(main, "reverify_pending_recruitment_candidates", broken_retry)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_iteration)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(main.future_radar_refresh_loop())

    assert calls == ["radar", "retry", "sleep"]
