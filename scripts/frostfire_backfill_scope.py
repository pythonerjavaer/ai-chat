#!/usr/bin/env python3
"""Resume a real, company-by-company search and stream public candidates to ingest.

Run from ai-chat with backend/.venv/bin/python. Credentials are loaded only by
the existing backend settings and macOS Keychain reader. Checkpoints contain
public, allowlisted candidate fields and aggregate usage, never raw responses,
private conversations, credentials, browser state, or personal profiles.
"""

from __future__ import annotations

import argparse
from collections import deque
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import frostfire_ingest as ingest


STATE_VERSION = 1
SOURCE_ID = "scope-web-2027"
SOURCE_NAME = "全范围企业网页检索"
SEARCH_SOURCE_ID = "openai-public-web-search"
MAX_WORKERS = 8
STATUS_URL = "https://frostfire-ai.onrender.com/api/recruitment/sync/status"
INGEST_SCRIPT = ROOT / "scripts" / "frostfire_ingest.py"
COUNTERS = (
    "received", "accepted", "new", "updated", "duplicates", "stale",
    "pending", "rejected", "closed",
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "tool_calls")
PUBLIC_SEARCH_TEXT_FIELDS = {
    "company": 120, "title": 240, "city": 120, "industry": 80,
    "requirements": 2000, "category": 80,
}
SECRET_TEXT = re.compile(
    r"(?i)(?:chatgpt\.com/c/|chat\.openai\.com/|\bsk-[a-z0-9_-]{8,}|"
    r"\bbearer\s+[a-z0-9._-]{12,}|(?:api[_ -]?key|password|cookie)\s*[:=])"
)
SECRET_QUERY = re.compile(r"(?i)^(?:token|access_token|api_key|apikey|password|cookie|authorization|jwt|signature)$")


class BackfillError(RuntimeError):
    """Only fixed error codes belong in this exception, never response bodies."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def safe_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if SECRET_TEXT.search(text):
        raise BackfillError("private_or_secret_like_candidate")
    text = ingest.EMAIL_PATTERN.sub("", text)
    for pattern in ingest.PHONE_PATTERNS:
        text = pattern.sub("", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def public_search_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Retain only public job fields, even when a normalization gate rejects it.

    This is a checkpoint projection, never an accepted/verified job. It lets a
    corrected parser replay real search results without another paid request.
    """
    from backend.recruitment_search import _date_or_none, _safe_official_url

    result = {field: safe_text(item.get(field), limit) for field, limit in PUBLIC_SEARCH_TEXT_FIELDS.items()}
    raw_url = str(item.get("official_url") or item.get("url") or "")
    if SECRET_TEXT.search(raw_url):
        raise BackfillError("private_or_secret_like_candidate")
    try:
        secret_query = any(SECRET_QUERY.fullmatch(key) for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(raw_url).query))
    except ValueError:
        secret_query = True
    if secret_query or ingest.EMAIL_PATTERN.search(raw_url):
        raise BackfillError("private_or_secret_like_candidate")
    # An unsafe URL is not retained; its fixed diagnostic remains in the record.
    result["official_url"] = _safe_official_url(raw_url) or ""
    result["opening_date"] = _date_or_none(item.get("opening_date"))
    result["closing_date"] = _date_or_none(item.get("closing_date"))
    return result


def public_candidate(
    job: dict[str, Any], *, observed_at: str, today: dt.date,
) -> dict[str, Any] | None:
    """Project an existing search result into the documented ingest allowlist."""
    from backend.recruitment_search import _candidate_cohort_is_unconfirmed, _safe_official_url

    if job.get("status", "open") != "open":
        return None
    for field, is_future in (("closing_date", False), ("opening_date", True)):
        value = job.get(field)
        if value:
            parsed = dt.date.fromisoformat(str(value))
            if (is_future and parsed > today) or (not is_future and parsed <= today):
                return None
    company = safe_text(job.get("company"), 120)
    title = safe_text(job.get("title"), 240)
    city = safe_text(job.get("city"), 120) or "地点待公告确认"
    requirements = safe_text(job.get("requirements"), 2000)
    target_year = today.year + (1 if today.month >= 6 else 0)
    cohort_text = title + " " + requirements
    if not re.search(rf"(?<!\d){target_year}(?!\d)|{str(target_year)[-2:]}届", cohort_text):
        return None
    if _candidate_cohort_is_unconfirmed(cohort_text):
        return None
    # Never revive a far-future Ubiquant program whose current-cohort eligibility
    # was not established; all employers also obey the opening/closing checks.
    if re.search(r"九坤|ubiquant", company, re.IGNORECASE) and re.search(
        rf"(?<!\d){target_year + 1}(?:届|\s*(?:graduate|campus))", cohort_text, re.IGNORECASE
    ):
        return None
    if not company or len(title) < 2:
        return None
    url = _safe_official_url(str(job.get("url") or job.get("official_url") or ""))
    if not url or SECRET_TEXT.search(url):
        return None
    parsed_url = urllib.parse.urlsplit(url)
    if any(SECRET_QUERY.fullmatch(key) for key, _ in urllib.parse.parse_qsl(parsed_url.query)):
        return None
    tags = []
    for value in job.get("tags", []):
        tag = safe_text(value, 120)
        # Only the receiving server can promote this transported candidate.
        if tag and "已验证" not in tag and tag not in tags:
            tags.append(tag)
    tags = list(dict.fromkeys([*tags[:17], "校园招聘", "全范围网页检索", "待官方核验"]))
    identity = "\0".join((company.casefold(), title.casefold(), city.casefold(), url))
    external_id = "scope-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    candidate = {
        "company": company,
        "title": title,
        "city": city,
        "employer_type": safe_text(job.get("employer_type"), 60) or "重点雇主",
        "industry": safe_text(job.get("industry"), 80),
        "official_url": url,
        "source": SOURCE_NAME,
        "source_id": SOURCE_ID,
        "source_item_id": external_id,
        "external_id": external_id,
        "source_updated_at": observed_at,
        "opening_date": job.get("opening_date"),
        "closing_date": job.get("closing_date"),
        "requirements": requirements,
        "tags": tags,
        "status": "open",
        "evidence": ["独立企业网页检索发现的候选；岗位、届别及开放状态仍须服务端依据官方原文复核。"],
    }
    return ingest.validate_job(candidate)


def scope_fingerprint(batches: Any, model: str, today: dt.date) -> str:
    value = {"model": model, "cohort": today.year + (1 if today.month >= 6 else 0), "targets": [b.targets[0].id for b in batches]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def new_state(fingerprint: str, model: str, target_count: int) -> dict[str, Any]:
    return {
        "version": STATE_VERSION, "scope": fingerprint, "model": model,
        "target_count": target_count, "created_at": now_iso(),
        "targets": {}, "deliveries": {},
    }


class Checkpoint:
    def __init__(self, directory: Path):
        roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
        directory = directory.expanduser()
        if directory.is_symlink():
            raise BackfillError("checkpoint_directory_symlink")
        resolved = directory.resolve()
        if not any(root in resolved.parents and resolved != root for root in roots):
            raise BackfillError("checkpoint_must_be_under_temp_directory")
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        if resolved.stat().st_uid != os.getuid():
            raise BackfillError("checkpoint_directory_not_owned")
        self.directory = resolved
        self.path = resolved / "checkpoint.json"
        self._lock_file: Any = None

    def __enter__(self) -> "Checkpoint":
        lock_path = self.directory / "process.lock"
        if lock_path.is_symlink():
            raise BackfillError("checkpoint_lock_symlink")
        self._lock_file = lock_path.open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise BackfillError("local_scope_process_running") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self._lock_file is not None:
            self._lock_file.close()

    def load(self, *, fingerprint: str, model: str, target_count: int) -> dict[str, Any]:
        if not self.path.exists():
            return new_state(fingerprint, model, target_count)
        if self.path.is_symlink():
            raise BackfillError("checkpoint_symlink")
        with self.path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        if state.get("version") != STATE_VERSION or state.get("scope") != fingerprint:
            raise BackfillError("checkpoint_scope_or_date_changed")
        if not isinstance(state.get("targets"), dict) or not isinstance(state.get("deliveries"), dict):
            raise BackfillError("invalid_checkpoint")
        for record in state["targets"].values():
            for job in record.get("jobs", []):
                ingest.validate_job(job)
            for raw in record.get("public_raw_candidates", []):
                if not isinstance(raw, dict) or not isinstance(raw.get("candidate"), dict):
                    raise BackfillError("invalid_public_candidate_checkpoint")
                if public_search_fields(raw["candidate"]) != raw["candidate"]:
                    raise BackfillError("unsafe_public_candidate_checkpoint")
        return state

    def save(self, state: dict[str, Any]) -> None:
        # Functional runtime output, not a repository artifact. Atomic rename
        # prevents a crash from destroying completed paid-search results.
        state["updated_at"] = now_iso()
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, self.path)


@contextlib.contextmanager
def search_lease():
    """Share the local app's actual deep-run and hosted-search source leases."""
    from backend import database
    from backend.future_radar.repository import RadarRepository, RUN_LOCK_TTL_SECONDS
    from backend.future_radar.service import _LeaseHeartbeat

    repository = RadarRepository(database.connect)
    owner = str(uuid.uuid4())
    leases = []
    names = ("future-radar-run:deep", f"future-radar-source:{SEARCH_SOURCE_ID}")
    try:
        for name in names:
            if not repository.acquire_lock(name, owner, RUN_LOCK_TTL_SECONDS):
                raise BackfillError("local_radar_search_already_running")
            lease = _LeaseHeartbeat(repository, lock_name=name, owner=owner, ttl_seconds=RUN_LOCK_TTL_SECONDS)
            leases.append((name, lease))
            lease.start()

        def ensure_owned() -> None:
            for _, lease in leases:
                lease.ensure_owned()

        yield ensure_owned
    finally:
        for name, lease in reversed(leases):
            lease.stop()
            repository.release_lock(name, owner)


def bridge_preflight() -> dict[str, Any]:
    token = ingest.read_keychain_token()
    if not token:
        raise BackfillError("keychain_token_unavailable")
    request = urllib.request.Request(STATUS_URL, headers={"X-Recruitment-Token": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read(ingest.MAX_RESPONSE_BYTES))
    except urllib.error.HTTPError as exc:
        raise BackfillError(f"bridge_http_{exc.code}") from None
    except Exception:
        raise BackfillError("bridge_status_unavailable") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise BackfillError("bridge_invalid_status")
    return {"source_count": int(payload.get("source_count", 0)), "source_registered": any(s.get("source_id") == SOURCE_ID for s in payload["sources"])}


def submit_jobs(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    payload = ingest.normalize_payload({"jobs": jobs, "source_id": SOURCE_ID})
    data = json.dumps(payload, ensure_ascii=False)
    child_env = dict(os.environ)
    # The legacy CLI permits env fallback. This workflow deliberately does not.
    child_env.pop(ingest.TOKEN_ENV, None)
    for flags in (("--dry-run",), ("--timeout", "120")):
        try:
            process = subprocess.run(
                [sys.executable, str(INGEST_SCRIPT), *flags], input=data,
                text=True, capture_output=True, cwd=ROOT, env=child_env, timeout=150,
            )
        except subprocess.TimeoutExpired:
            raise BackfillError("ingest_timeout_result_unknown") from None
        if process.returncode:
            status = re.search(r"HTTP error (\d{3})", process.stderr or "")
            suffix = "_http_" + status.group(1) if status else ""
            raise BackfillError(f"ingest_exit_{process.returncode}{suffix}")
        try:
            result = json.loads(process.stdout)
        except (ValueError, TypeError):
            raise BackfillError("ingest_invalid_response") from None
    if not isinstance(result, dict) or any(not isinstance(result.get(k), int) for k in COUNTERS):
        raise BackfillError("ingest_missing_counts")
    reason_counts: dict[str, int] = {}
    for skipped in result.get("skipped", []):
        reason = str(skipped.get("reason", "")) if isinstance(skipped, dict) else ""
        # Server reason codes only; never reflect a free-form title/body/error.
        reason = reason if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason) else "unspecified"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        **{key: result[key] for key in COUNTERS},
        "projection_status": result.get("search_updates_refresh", {}).get("status", "unknown"),
        "reason_counts": reason_counts,
    }


def pending_jobs(state: dict[str, Any]) -> list[dict[str, Any]]:
    delivered = {identity for delivery in state["deliveries"].values() if delivery.get("status") == "submitted" for identity in delivery.get("ids", [])}
    seen = set(delivered)
    jobs = []
    for record in state["targets"].values():
        for job in record.get("jobs", []):
            identity = job["external_id"]
            if identity not in seen:
                seen.add(identity)
                jobs.append(job)
    return jobs


def flush_pending(state: dict[str, Any], checkpoint: Checkpoint) -> None:
    jobs = pending_jobs(state)
    for offset in range(0, len(jobs), ingest.MAX_JOBS):
        batch = jobs[offset:offset + ingest.MAX_JOBS]
        ids = [job["external_id"] for job in batch]
        key = hashlib.sha256("\0".join(ids).encode()).hexdigest()[:24]
        try:
            counts = submit_jobs(batch)
        except BackfillError as exc:
            state["deliveries"][key] = {"status": "error", "ids": ids, "error": str(exc), "attempted_at": now_iso()}
            checkpoint.save(state)
            emit("ingest_failed", jobs=len(batch), error=str(exc))
            raise
        state["deliveries"][key] = {"status": "submitted", "ids": ids, "counts": counts, "submitted_at": now_iso()}
        checkpoint.save(state)
        emit("ingested", **counts)


def header_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
        return sum(float(number) * units[unit] for number, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value))


class HostedRequestLimiter:
    """Honor provider TPM/reset headers, independently of app scan frequency."""
    def __init__(self):
        self.condition = threading.Condition()
        self.next_start = 0.0
        self.pause_until = 0.0
        self.spacing = 0.0

    def acquire(self) -> None:
        with self.condition:
            while True:
                wait = max(self.next_start, self.pause_until) - time.monotonic()
                if wait <= 0:
                    self.next_start = time.monotonic() + self.spacing
                    return
                self.condition.wait(timeout=min(wait, 15.0))

    def observe(self, headers: Any, usage: dict[str, int] | None = None) -> None:
        from backend.recruitment_search import SEARCH_MAX_OUTPUT_TOKENS

        headers = headers or {}
        try:
            limit = int(headers.get("x-ratelimit-limit-tokens", 0))
        except (TypeError, ValueError):
            limit = 0
        if not limit:
            return
        reservation = max(SEARCH_MAX_OUTPUT_TOKENS + 2500, int((usage or {}).get("total_tokens", 0)))
        spacing = 60 * reservation / limit * 1.15
        with self.condition:
            self.spacing = max(self.spacing, spacing)
            try:
                remaining = int(headers.get("x-ratelimit-remaining-tokens", limit))
            except (TypeError, ValueError):
                remaining = limit
            if remaining < reservation:
                self.pause_until = max(self.pause_until, time.monotonic() + header_seconds(headers.get("x-ratelimit-reset-tokens")))
            self.next_start = max(self.next_start, time.monotonic() + self.spacing)
            self.condition.notify_all()

    def throttled(self, headers: Any) -> float:
        headers = headers or {}
        self.observe(headers)
        pause = max(header_seconds(headers.get("retry-after")), header_seconds(headers.get("x-ratelimit-reset-tokens")), 1.0)
        if pause == 1.0:
            pause = 60.0
        with self.condition:
            self.pause_until = max(self.pause_until, time.monotonic() + pause)
            self.spacing = max(self.spacing, 6.0)
            self.condition.notify_all()
        return pause


class ResponseMeter:
    def __init__(self, client: Any, ensure_owned: Any, limiter: HostedRequestLimiter | None = None, batch: Any = None):
        self.client = client
        self.ensure_owned = ensure_owned
        self.responses = self
        self.usage = {key: 0 for key in USAGE_FIELDS}
        self.returned_jobs = 0
        self.limiter = limiter
        self.batch = batch
        self.filter_counts: dict[str, int] = {}
        self.public_raw_candidates: list[dict[str, Any]] = []
        self.completed_source_urls: list[str] = []

    def create(self, **kwargs: Any) -> Any:
        from backend.recruitment_search import _response_value

        self.ensure_owned()
        if self.limiter is not None:
            self.limiter.acquire()
        self.ensure_owned()
        raw_api = getattr(self.client.responses, "with_raw_response", None)
        if raw_api is not None:
            raw = raw_api.create(**kwargs)
            response = raw.parse()
            headers = raw.headers
        else:
            response = self.client.responses.create(**kwargs)
            headers = {}
        usage = getattr(response, "usage", None)
        for key in USAGE_FIELDS[:-1]:
            self.usage[key] += max(0, int(getattr(usage, key, 0) or 0))
        self.usage["tool_calls"] += sum(_response_value(x, "type") == "web_search_call" and _response_value(x, "status") == "completed" for x in getattr(response, "output", []) or [])
        if self.limiter is not None:
            self.limiter.observe(headers, self.usage)
        try:
            parsed = json.loads(response.output_text)
            self.returned_jobs += len(parsed.get("jobs", [])) if isinstance(parsed, dict) else 0
            if self.batch is not None and isinstance(parsed, dict):
                from backend.recruitment_search import _candidate_was_cited, _completed_web_search_sources, _normalize_job_with_reason
                citations, _ = _completed_web_search_sources(response)
                for url in sorted(citations):
                    try:
                        safe_url = public_search_fields({"official_url": url})["official_url"]
                    except BackfillError:
                        continue
                    if safe_url and safe_url not in self.completed_source_urls:
                        self.completed_source_urls.append(safe_url)
                for item in parsed.get("jobs", []):
                    normalized, reason = _normalize_job_with_reason(item, self.batch.pool, self.batch.targets[0])
                    cited = bool(normalized and _candidate_was_cited(normalized["url"], citations))
                    if normalized:
                        reason = "passed_shape_and_citation" if cited else "citation_unconfirmed_pending"
                    if isinstance(item, dict):
                        try:
                            public = public_search_fields(item)
                            self.public_raw_candidates.append({"candidate": public, "decision": reason, "citation_confirmed": cited})
                        except BackfillError:
                            reason = "private_or_secret_like_candidate"
                    self.filter_counts[reason] = self.filter_counts.get(reason, 0) + 1
        except (AttributeError, ValueError, TypeError, RuntimeError):
            pass
        return response


def search_one(client: Any, batch: Any, ensure_owned: Any, today: dt.date, limiter: HostedRequestLimiter | None = None) -> dict[str, Any]:
    from backend.recruitment_search import _search_batch

    meter = ResponseMeter(client, ensure_owned, limiter, batch)
    record: dict[str, Any] = {"employer": batch.targets[0].canonical_name, "started_at": now_iso(), "jobs": [], "usage": meter.usage}
    try:
        result = _search_batch(meter, batch)
        observed_at = now_iso()
        excluded = 0
        for job in result.jobs:
            try:
                candidate = public_candidate(job, observed_at=observed_at, today=today)
            except (BackfillError, ValueError, ingest.InputError):
                candidate = None
            if candidate is None:
                excluded += 1
            else:
                record["jobs"].append(candidate)
        record.update(status="searched", excluded=excluded, completed_at=observed_at)
    except Exception as exc:
        # Avoid SDK exception strings: they can contain payloads and headers.
        record.update(status="error", error_type=type(exc).__name__, completed_at=now_iso())
        if isinstance(getattr(exc, "status_code", None), int):
            record["http_status"] = exc.status_code
        if record.get("http_status") == 429:
            code = str(getattr(exc, "code", ""))
            record["provider_code"] = code if code in {"rate_limit_exceeded", "insufficient_quota", "billing_hard_limit_reached"} else "rate_limit_unknown"
            if limiter is not None and record["provider_code"] not in {"insufficient_quota", "billing_hard_limit_reached"}:
                record["retry_after_seconds"] = round(limiter.throttled(getattr(getattr(exc, "response", None), "headers", {})), 2)
    record["raw_candidate_count"] = meter.returned_jobs
    record["filter_counts"] = meter.filter_counts
    record["public_raw_candidates"] = meter.public_raw_candidates
    record["completed_source_urls"] = meter.completed_source_urls
    return record


def reprocess_saved(state: dict[str, Any], checkpoint: Checkpoint, batches: Any) -> None:
    """Re-normalize saved public fields; no paid API call or invented tool result."""
    from backend.recruitment_search import _candidate_was_cited, _inspect_normalized_search_candidate, _normalize_job_with_reason

    batches_by_id = {batch.targets[0].id: batch for batch in batches}
    for identity, record in state["targets"].items():
        batch = batches_by_id.get(identity)
        if batch is None or record.get("status") != "searched":
            continue
        raw_records = record.get("public_raw_candidates", [])
        # Old checkpoints have no discarded raw fields. Existing transported
        # rows can still be checked for old-cohort or conditional assertions.
        if not raw_records:
            raw_records = [{"candidate": public_search_fields(job), "citation_confirmed": "搜索引用待确认" not in job.get("tags", [])} for job in record.get("jobs", [])]
        if not raw_records:
            continue
        record["public_raw_candidates"] = raw_records
        jobs = []
        reasons: dict[str, int] = {}
        for raw in raw_records:
            normalized, reason = _normalize_job_with_reason(raw["candidate"], batch.pool, batch.targets[0])
            if normalized:
                cited = bool(raw.get("citation_confirmed")) or _candidate_was_cited(normalized["url"], set(record.get("completed_source_urls", [])))
                normalized, reason = _inspect_normalized_search_candidate(normalized, batch.targets[0], cited=cited)
            if normalized:
                try:
                    candidate = public_candidate(normalized, observed_at=record.get("completed_at") or now_iso(), today=dt.date.today())
                except (BackfillError, ValueError, ingest.InputError):
                    candidate = None
                if candidate:
                    jobs.append(candidate)
                else:
                    reason = "transport_validation_excluded"
            reasons[reason] = reasons.get(reason, 0) + 1
        record["jobs"] = list({job["external_id"]: job for job in jobs}.values())
        record["reprocess_counts"] = reasons
        record["reprocessed_at"] = now_iso()
        checkpoint.save(state)
        emit("reprocessed", employer=record["employer"], candidates=len(record["jobs"]), reasons=reasons)


def revalidate_saved(state: dict[str, Any], checkpoint: Checkpoint) -> None:
    """Replay unchanged public payloads through the server's official verifier."""
    emit("bridge_preflight", **bridge_preflight())
    jobs = list({job["external_id"]: job for record in state["targets"].values() for job in record.get("jobs", [])}.values())
    for offset in range(0, len(jobs), ingest.MAX_JOBS):
        batch = jobs[offset:offset + ingest.MAX_JOBS]
        counts = submit_jobs(batch)
        state.setdefault("revalidations", []).append({"at": now_iso(), "ids": [job["external_id"] for job in batch], "counts": counts})
        checkpoint.save(state)
        emit("revalidated", **counts)


def missing_public_result_targets(state: dict[str, Any], batches: Any) -> list[Any]:
    """Target only previously dropped, unrecoverable results, never all successes."""
    return [batch for batch in batches if (
        (record := state["targets"].get(batch.targets[0].id))
        and record.get("status") == "searched"
        and record.get("raw_candidate_count", 0) > 0
        and not record.get("jobs")
        and not record.get("public_raw_candidates")
    )]


def summary(state: dict[str, Any]) -> dict[str, Any]:
    records = list(state["targets"].values())
    attempts = [attempt for record in records for attempt in [*record.get("previous_attempts", []), record]]
    totals = {key: sum(int(record.get("usage", {}).get(key, 0)) for record in attempts) for key in USAGE_FIELDS}
    received = {key: 0 for key in COUNTERS}
    filter_counts: dict[str, int] = {}
    for record in records:
        for reason, count in record.get("filter_counts", {}).items():
            filter_counts[reason] = filter_counts.get(reason, 0) + int(count)
    for delivery in state["deliveries"].values():
        if delivery.get("status") == "submitted":
            for key in COUNTERS:
                received[key] += int(delivery.get("counts", {}).get(key, 0))
    return {
        "target_count": state["target_count"],
        "searched": sum(r.get("status") == "searched" for r in records),
        "search_failed": sum(r.get("status") == "error" for r in records),
        "failed_attempts": sum(r.get("status") == "error" for r in attempts),
        "raw_candidates": sum(r.get("raw_candidate_count", 0) for r in records),
        "employers_with_candidates": sum(bool(r.get("jobs")) for r in records),
        "candidates": sum(len(r.get("jobs", [])) for r in records),
        "awaiting_ingest": len(pending_jobs(state)),
        "filter_counts": filter_counts,
        "usage": totals, "ingest": received,
    }


def execute(state: dict[str, Any], checkpoint: Checkpoint, batches: Any, *, retry_failed: bool = False, ingest_only: bool = False, recover_dropped: bool = False) -> int:
    from backend.config import settings
    from openai import OpenAI

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    emit("bridge_preflight", **bridge_preflight())
    flush_pending(state, checkpoint)
    if ingest_only:
        emit("summary", **summary(state))
        return 0
    unfinished = deque(missing_public_result_targets(state, batches)) if recover_dropped else deque(b for b in batches if b.targets[0].id not in state["targets"] or (retry_failed and state["targets"][b.targets[0].id].get("status") == "error"))
    if recover_dropped:
        emit("recover_dropped", employers=len(unfinished))
    ingestion_failed = False
    limiter = HostedRequestLimiter()
    rate_retries: dict[str, int] = {}
    with search_lease() as ensure_owned, OpenAI(api_key=settings.openai_api_key, timeout=240, max_retries=0) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures: dict[Any, Any] = {}

            def fill() -> None:
                while len(futures) < MAX_WORKERS and not stop.is_set():
                    batch = unfinished.popleft() if unfinished else None
                    if batch is None:
                        break
                    ensure_owned()
                    futures[pool.submit(search_one, client, batch, ensure_owned, dt.date.today(), limiter)] = batch

            fill()
            while futures:
                done, _ = concurrent.futures.wait(futures, timeout=15, return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    emit("running", in_flight=len(futures), **summary(state))
                    continue
                for future in done:
                    batch = futures.pop(future)
                    record = future.result()
                    previous = state["targets"].get(batch.targets[0].id)
                    if previous:
                        record["previous_attempts"] = [
                            *previous.get("previous_attempts", []),
                            {key: previous[key] for key in ("status", "usage", "error_type", "http_status", "completed_at", "raw_candidate_count", "filter_counts", "public_raw_candidates", "completed_source_urls") if key in previous},
                        ]
                    state["targets"][batch.targets[0].id] = record
                    checkpoint.save(state)
                    emit("employer_finished", employer=record["employer"], status=record["status"], candidates=len(record["jobs"]), **{k: record[k] for k in ("error_type", "http_status", "provider_code", "retry_after_seconds", "filter_counts") if k in record})
                    if record.get("http_status") in {401, 403} or record.get("provider_code") in {"insufficient_quota", "billing_hard_limit_reached"}:
                        stop.set()
                    elif record.get("http_status") == 429:
                        identity = batch.targets[0].id
                        rate_retries[identity] = rate_retries.get(identity, 0) + 1
                        if rate_retries[identity] <= 3:
                            unfinished.appendleft(batch)
                    if not ingestion_failed:
                        try:
                            flush_pending(state, checkpoint)
                        except BackfillError:
                            ingestion_failed = True
                            stop.set()
                    fill()
    emit("summary", **summary(state))
    return 2 if stop.is_set() or any(r.get("status") == "error" for r in state["targets"].values()) else 0


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="private runtime checkpoint directory under the system temporary directory")
    parser.add_argument("--plan", action="store_true", help="print scope/model without using API or Keychain")
    parser.add_argument("--status", action="store_true", help="print checkpoint counts without network calls")
    parser.add_argument("--ingest-only", action="store_true", help="deliver saved candidates without more paid searches")
    parser.add_argument("--retry-failed", action="store_true", help="explicitly retry failed employers; completed employers are never repeated")
    parser.add_argument("--recover-dropped", action="store_true", help="retry only old successful responses whose dropped public fields were not saved; does not search unfinished employers")
    parser.add_argument("--reprocess-saved", action="store_true", help="apply current normalization to checkpoint public fields without paid requests")
    parser.add_argument("--revalidate-saved", action="store_true", help="send unchanged saved candidates through official server verification without paid discovery")
    args = parser.parse_args(argv)
    from backend.config import settings
    from backend.recruitment_search import build_employer_search_batches

    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("openai").setLevel(logging.ERROR)
    batches = build_employer_search_batches()
    fingerprint = scope_fingerprint(batches, settings.recruitment_web_search_model, dt.date.today())
    if args.plan:
        emit("plan", model=settings.recruitment_web_search_model, targets=len(batches), concurrency=MAX_WORKERS, max_tool_calls_per_target=settings.recruitment_web_search_max_tool_calls)
        return 0
    try:
        checkpoint = Checkpoint(args.state_dir)
        if args.status:
            state = checkpoint.load(fingerprint=fingerprint, model=settings.recruitment_web_search_model, target_count=len(batches))
            emit("summary", **summary(state))
            return 0
        with checkpoint:
            state = checkpoint.load(fingerprint=fingerprint, model=settings.recruitment_web_search_model, target_count=len(batches))
            checkpoint.save(state)
            if args.reprocess_saved:
                reprocess_saved(state, checkpoint, batches)
            if args.revalidate_saved:
                revalidate_saved(state, checkpoint)
            if args.reprocess_saved or args.revalidate_saved:
                emit("summary", **summary(state))
                return 0
            return execute(state, checkpoint, batches, retry_failed=args.retry_failed, ingest_only=args.ingest_only, recover_dropped=args.recover_dropped)
    except BackfillError as exc:
        emit("error", code=str(exc))
        return 2
    except Exception as exc:
        emit("error", error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
