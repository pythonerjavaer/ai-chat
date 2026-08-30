import contextlib
import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts import frostfire_backfill_scope as backfill


TODAY = dt.date(2026, 8, 30)


def job(**changes):
    value = {
        "company": "腾讯", "title": "软件工程师", "city": "深圳",
        "url": "https://careers.tencent.com/job/1", "status": "open",
        "requirements": "2027届应届毕业生，计算机专业",
        "opening_date": None, "closing_date": None,
        "tags": ["校园招聘", "标题已验证", "链接已验证"],
    }
    value.update(changes)
    return value


def candidate(**changes):
    return backfill.public_candidate(job(**changes), observed_at="2026-08-30T00:00:00+00:00", today=TODAY)


def counts(size):
    return {**{key: 0 for key in backfill.COUNTERS}, "received": size, "pending": size, "new": size, "projection_status": "success"}


def test_transport_only_allowlisted_public_fields_and_no_client_verification():
    value = candidate(api_key="never-transport", source_thread_id="private", cookie="private", requirements="2027届应届毕业生，联系 person@example.com 13800138000")
    assert value is not None
    assert set(value) <= backfill.ingest.JOB_FIELDS
    assert not {"api_key", "source_thread_id", "cookie"} & set(value)
    assert "person@example.com" not in value["requirements"]
    assert "13800138000" not in value["requirements"]
    assert not any("已验证" in tag for tag in value["tags"])
    assert "待官方核验" in value["tags"]


@pytest.mark.parametrize("changes", [
    {"closing_date": "2026-08-29"},
    {"closing_date": "2026-08-30"},
    {"opening_date": "2026-08-31"},
    {"status": "closed"},
    {"requirements": "2026届毕业生"},
    {"url": "https://chatgpt.com/c/private-conversation"},
    {"url": "https://careers.tencent.com/job/1?access_token=private"},
])
def test_expired_future_old_cohort_or_private_targets_are_not_transported(changes):
    assert candidate(**changes) is None


def test_explicit_private_text_is_rejected_without_echoing_it():
    with pytest.raises(backfill.BackfillError) as error:
        candidate(requirements="2027毕业生 API_KEY=private-data")
    assert str(error.value) == "private_or_secret_like_candidate"


def test_identity_stays_stable_across_retries():
    first = candidate()
    second = backfill.public_candidate(job(), observed_at="2026-08-31T00:00:00Z", today=dt.date(2026, 8, 31))
    assert first["external_id"] == second["external_id"]
    assert first["source_item_id"] == first["external_id"]


def test_checkpoint_process_lock_and_resume(tmp_path):
    fingerprint = "scope"
    with backfill.Checkpoint(tmp_path) as checkpoint:
        state = checkpoint.load(fingerprint=fingerprint, model="test-model", target_count=1)
        state["targets"]["target"] = {"status": "searched", "jobs": [candidate()]}
        checkpoint.save(state)
        with pytest.raises(backfill.BackfillError, match="local_scope_process_running"):
            with backfill.Checkpoint(tmp_path):
                pass
    with backfill.Checkpoint(tmp_path) as checkpoint:
        loaded = checkpoint.load(fingerprint=fingerprint, model="test-model", target_count=1)
        assert loaded["targets"]["target"]["jobs"] == [candidate()]
        with pytest.raises(backfill.BackfillError, match="scope_or_date_changed"):
            checkpoint.load(fingerprint="different", model="test-model", target_count=1)


def test_all_43_jobs_are_delivered_in_batches_no_larger_than_ten(tmp_path):
    with backfill.Checkpoint(tmp_path) as checkpoint:
        state = backfill.new_state("x", "test-model", 1)
        state["targets"]["target"] = {"status": "searched", "jobs": [candidate(title=f"工程师-{i}") for i in range(43)]}
        batches = []

        def submit(batch):
            batches.append(batch)
            return counts(len(batch))

        with mock.patch.object(backfill, "submit_jobs", side_effect=submit):
            backfill.flush_pending(state, checkpoint)
            backfill.flush_pending(state, checkpoint)
        assert [len(batch) for batch in batches] == [10, 10, 10, 10, 3]
        assert len({j["external_id"] for batch in batches for j in batch}) == 43
        assert not backfill.pending_jobs(state)
        assert backfill.summary(state)["ingest"]["pending"] == 43


def test_failed_submission_keeps_saved_jobs_for_ingest_only_retry(tmp_path):
    with backfill.Checkpoint(tmp_path) as checkpoint:
        state = backfill.new_state("x", "test-model", 1)
        state["targets"]["target"] = {"status": "searched", "jobs": [candidate()]}
        with mock.patch.object(backfill, "submit_jobs", side_effect=backfill.BackfillError("ingest_timeout_result_unknown")):
            with pytest.raises(backfill.BackfillError):
                backfill.flush_pending(state, checkpoint)
        assert len(backfill.pending_jobs(state)) == 1
        with mock.patch.object(backfill, "submit_jobs", return_value=counts(1)):
            backfill.flush_pending(state, checkpoint)
        assert not backfill.pending_jobs(state)
        assert backfill.summary(state)["ingest"]["received"] == 1


def test_stdin_dry_run_precedes_submit_and_ignores_env_token():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert backfill.ingest.TOKEN_ENV not in kwargs["env"]
        assert len(json.loads(kwargs["input"])["jobs"]) == 1
        response = {"dry_run": True} if "--dry-run" in argv else counts(1)
        return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")

    with mock.patch.dict(os.environ, {backfill.ingest.TOKEN_ENV: "do-not-use-env-secret"}), mock.patch.object(backfill.subprocess, "run", side_effect=run):
        assert backfill.submit_jobs([candidate()])["pending"] == 1
    assert "--dry-run" in calls[0][0]
    assert "--timeout" in calls[1][0]
    assert len(calls) == 2


def test_failed_dry_run_never_submits_and_no_raw_error_is_repeated():
    process = SimpleNamespace(returncode=2, stdout="", stderr="bad private payload")
    with mock.patch.object(backfill.subprocess, "run", return_value=process) as run:
        with pytest.raises(backfill.BackfillError) as error:
            backfill.submit_jobs([candidate()])
    assert run.call_count == 1
    assert str(error.value) == "ingest_exit_2"


def test_failed_parsing_still_accounts_for_response_usage():
    import backend.recruitment_search as search

    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120), output=[{"type": "web_search_call", "status": "completed"}], output_text='{"jobs":[]}')
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: response))
    batch = SimpleNamespace(targets=[SimpleNamespace(canonical_name="腾讯")])

    def fail(meter, _):
        meter.responses.create()
        raise RuntimeError("must not retain raw server payload")

    with mock.patch.object(search, "_search_batch", side_effect=fail):
        record = backfill.search_one(client, batch, lambda: None, TODAY)
    assert record["status"] == "error"
    assert record["usage"] == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "tool_calls": 1}
    assert record["error_type"] == "RuntimeError"
    assert "must not retain" not in json.dumps(record)


def test_usage_includes_previous_failed_attempts():
    state = backfill.new_state("x", "test-model", 1)
    state["targets"]["target"] = {"status": "searched", "jobs": [], "usage": {"total_tokens": 20}, "previous_attempts": [{"status": "error", "usage": {"total_tokens": 10}}]}
    result = backfill.summary(state)
    assert result["usage"]["total_tokens"] == 30
    assert result["search_failed"] == 0
    assert result["failed_attempts"] == 1


def test_scope_resume_survives_midnight():
    batch = SimpleNamespace(targets=[SimpleNamespace(id="one")])
    assert backfill.scope_fingerprint([batch], "test-model", TODAY) == backfill.scope_fingerprint([batch], "test-model", dt.date(2026, 8, 31))


def test_execution_skips_completed_targets_and_delivers_incrementally(tmp_path):
    import openai

    batches = [SimpleNamespace(targets=[SimpleNamespace(id=str(i), canonical_name=f"company-{i}")]) for i in range(3)]
    state = backfill.new_state("x", "test-model", 3)
    state["targets"]["0"] = {"status": "searched", "jobs": []}
    requested = []
    delivered_at = []

    def search_one(_client, batch, _lease, _today, _limiter):
        requested.append(batch.targets[0].id)
        return {"status": "searched", "employer": batch.targets[0].canonical_name, "jobs": [candidate(title=f"工程师-{batch.targets[0].id}")], "usage": {}}

    def submit(batch):
        delivered_at.append(len(state["targets"]))
        return counts(len(batch))

    with backfill.Checkpoint(tmp_path) as checkpoint, mock.patch.object(backfill, "bridge_preflight", return_value={}), mock.patch.object(backfill, "search_lease", return_value=contextlib.nullcontext(lambda: None)), mock.patch.object(openai, "OpenAI", return_value=contextlib.nullcontext(object())), mock.patch.object(backfill, "search_one", side_effect=search_one), mock.patch.object(backfill, "submit_jobs", side_effect=submit), mock.patch.object(backfill.signal, "signal"):
        assert backfill.execute(state, checkpoint, batches) == 0
    assert sorted(requested) == ["1", "2"]
    assert delivered_at == [2, 3]


def test_provider_rate_limit_headers_control_spacing_and_retry():
    limiter = backfill.HostedRequestLimiter()
    limiter.observe({"x-ratelimit-limit-tokens": "200000", "x-ratelimit-remaining-tokens": "190000"}, {"total_tokens": 10000})
    assert 6 <= limiter.spacing <= 7
    pause = limiter.throttled({"retry-after": "2", "x-ratelimit-reset-tokens": "3500ms"})
    assert pause == 3.5
    assert limiter.pause_until > 0


def test_provider_retry_interval_parses_compound_durations():
    assert backfill.header_seconds("1m2.5s") == 62.5
    assert backfill.header_seconds("500ms") == 0.5
    assert backfill.header_seconds("30") == 30


def test_raw_public_projection_preserves_safe_rejected_fields_but_no_extra_fields():
    original = {
        "company": "腾讯", "title": "软件工程师", "city": "深圳",
        "requirements": "2027届，person@example.com 13800138000",
        "official_url": "https://careers.tencent.com/job/1",
        "closing_date": "未公告", "source_thread_id": "private-thread",
        "api_key": "private-secret", "cookie": "private-cookie",
    }
    public = backfill.public_search_fields(original)
    assert set(public) == {*backfill.PUBLIC_SEARCH_TEXT_FIELDS, "official_url", "opening_date", "closing_date"}
    assert public["requirements"] == "2027届，"
    assert public["closing_date"] is None
    assert "private" not in json.dumps(public)


def test_meter_records_specific_gates_and_public_candidates_before_normalization():
    import backend.recruitment_search as search

    batch = next(b for b in search.build_employer_search_batches() if b.targets[0].canonical_name == "腾讯")
    source_url = "https://careers.tencent.com/job/1"
    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=10, total_tokens=20),
        output=[{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": source_url}]}}],
        output_text=json.dumps({"jobs": [
            {**job(), "official_url": source_url, "requirements": "2027届", "source_thread_id": "private"},
            {**job(), "official_url": source_url, "requirements": "2026届"},
        ]}),
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: response))
    meter = backfill.ResponseMeter(client, lambda: None, batch=batch)
    assert meter.create() is response
    assert meter.filter_counts == {"passed_shape_and_citation": 1, "wrong_or_missing_cohort": 1}
    assert len(meter.public_raw_candidates) == 2
    assert meter.public_raw_candidates[1]["candidate"]["requirements"] == "2026届"
    assert "source_thread_id" not in json.dumps(meter.public_raw_candidates)


def test_recovery_targets_only_old_dropped_results_without_saved_public_fields():
    batches = [SimpleNamespace(targets=[SimpleNamespace(id=str(i))]) for i in range(5)]
    state = backfill.new_state("x", "test-model", 5)
    state["targets"].update({
        "0": {"status": "searched", "raw_candidate_count": 3, "jobs": []},
        "1": {"status": "searched", "raw_candidate_count": 0, "jobs": []},
        "2": {"status": "searched", "raw_candidate_count": 1, "jobs": [candidate()]},
        "3": {"status": "searched", "raw_candidate_count": 1, "jobs": [], "public_raw_candidates": [{"candidate": {}}]},
    })
    assert [b.targets[0].id for b in backfill.missing_public_result_targets(state, batches)] == ["0"]


def test_reprocessing_uses_saved_fields_not_openai_and_rejects_conditional_cohort(tmp_path):
    import backend.recruitment_search as search

    batch = next(b for b in search.build_employer_search_batches() if b.targets[0].canonical_name == "腾讯")
    state = backfill.new_state("x", "test-model", 1)
    public = backfill.public_search_fields({**job(), "requirements": "2027届"})
    conditional = {**public, "requirements": "原公告2026届，2027届是否接收待确认"}
    state["targets"][batch.targets[0].id] = {
        "status": "searched", "employer": "腾讯", "completed_at": "2026-08-30T00:00:00+00:00", "jobs": [],
        "public_raw_candidates": [{"candidate": public, "citation_confirmed": True}, {"candidate": conditional, "citation_confirmed": True}],
    }
    with backfill.Checkpoint(tmp_path) as checkpoint, mock.patch.object(search, "_inspect_official_candidate_page", return_value=search.CandidatePageEvidence(readable=False, title_confirmed=False)), mock.patch.object(search, "_search_batch", side_effect=AssertionError("must not pay again")):
        backfill.reprocess_saved(state, checkpoint, [batch])
    record = state["targets"][batch.targets[0].id]
    assert len(record["jobs"]) == 1
    assert record["reprocess_counts"] == {"official_pending": 1, "cohort_unconfirmed": 1}
    assert record["jobs"][0]["source_updated_at"] == "2026-08-30T00:00:00+00:00"


def test_ingest_retains_only_safe_reason_codes():
    def run(argv, **kwargs):
        response = {"dry_run": True} if "--dry-run" in argv else {
            **counts(2), "skipped": [
                {"title": "private ignored title", "reason": "not_campus"},
                {"title": "private ignored title", "reason": "Raw request secret"},
            ],
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")

    with mock.patch.object(backfill.subprocess, "run", side_effect=run):
        result = backfill.submit_jobs([candidate()])
    assert result["reason_counts"] == {"not_campus": 1, "unspecified": 1}
    assert "private" not in json.dumps(result)
