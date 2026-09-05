#!/usr/bin/env python3
"""Review or submit already-sanitized, newest-first recruitment history.

This script never reads a browser, a conversation ID, cookies, or raw messages.
It accepts only logical sources, irreversible message digests, and the existing
bridge's recruitment row allowlist. Rendered assistant tables and individual
HTTPS-linked job entries are equally valid sources for sanitized rows; the
original message need not contain JSON. Default operation is an offline dry-run.
The separate local ledger contains only digests, booleans, and numeric counts.
The 10,000-row and byte bounds apply to one input page, not a monitoring run.
Continue newest-first pages until all readable updates are processed; leave
history_complete false while any source history remains unread or held.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from zoneinfo import ZoneInfo


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from backend.future_radar.normalization import canonicalize_url, normalized_key  # noqa: E402
from scripts import frostfire_chatgpt_bridge as bridge  # noqa: E402
from scripts.frostfire_batching import DEFAULT_BATCH_SIZE, MAX_INPUT_ROWS, validate_batch_size  # noqa: E402
from scripts.frostfire_chatgpt_sources import (  # noqa: E402
    ACTIVE_CHATGPT_SOURCE_IDS,
    HISTORICAL_CHATGPT_SOURCE_IDS,
)
from scripts.frostfire_ingest import (  # noqa: E402
    InputError as IngestInputError,
    MAX_RESPONSE_BYTES,
    normalize_payload as validate_ingest_payload,
    read_keychain_token,
    submit_payload,
)
from scripts.frostfire_source_import import (  # noqa: E402
    ImportError as SourceImportError,
    _validate_no_sensitive_material,
)


ALLOWED_SOURCES = frozenset(ACTIVE_CHATGPT_SOURCE_IDS)
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_CHAT_PATTERN = re.compile(r"(?i)(?:chatgpt\.com|chat\.openai\.com)(?:[/:?#]|$)")
MAX_INPUT_BYTES = 8_000_000
MAX_MESSAGES = 1_000
MAX_MESSAGE_ROWS = MAX_INPUT_ROWS
MAX_TOTAL_ROWS = MAX_INPUT_ROWS
MAX_LEDGER_BYTES = 8_000_000
MAX_LEDGER_HASHES = 50_000
LEDGER_VERSION = 1
TOP_FIELDS = frozenset({"source_id", "history_complete", "messages"})
MESSAGE_FIELDS = frozenset({"message_digest", "rows"})
SOURCE_LEDGER_FIELDS = frozenset({
    "messages", "items", "history_complete", "last_history_digest", "last_message_count",
})
RESULT_COUNTERS = ("received", "accepted", "source_screened", "pending", "rejected", "closed", "new", "updated", "duplicates", "stale")


class HistoryError(ValueError):
    """Only constant, safe error messages may be attached to this exception."""


class SubmissionError(HistoryError):
    def __init__(self, code: str, *, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(DIGEST_PATTERN.fullmatch(value))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HistoryError("duplicate JSON properties are not allowed")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise HistoryError("non-finite JSON values are not allowed")


def _json_loads(raw: str | bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HistoryError("invalid bounded JSON input") from exc


def _read_stdin() -> Any:
    stream = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw.strip() or len(raw) > MAX_INPUT_BYTES:
        raise HistoryError("history input is empty or exceeds the size limit")
    return _json_loads(raw)


def _reject_private_strings(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _reject_private_strings(item)
    elif isinstance(value, list):
        for item in value:
            _reject_private_strings(item)
    elif isinstance(value, str):
        decoded = urllib.parse.unquote(urllib.parse.unquote(value))
        if PRIVATE_CHAT_PATTERN.search(decoded) or bridge.SECRET_PATTERN.search(decoded):
            raise HistoryError("private account or credential material is not allowed")


@dataclass(frozen=True)
class Message:
    digest: str
    rows_digest: str
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class History:
    source_id: str
    history_complete: bool
    messages: list[Message]


def parse_history(value: Any) -> History:
    if not isinstance(value, dict) or set(value) != TOP_FIELDS:
        raise HistoryError("input must contain only source_id, history_complete, and messages")
    source_id = value.get("source_id")
    if not isinstance(source_id, str) or source_id not in ALLOWED_SOURCES:
        raise HistoryError("source_id must be one of the seven active ChatGPT monitoring labels")
    if type(value.get("history_complete")) is not bool:
        raise HistoryError("history_complete must be a boolean")
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list) or not 1 <= len(raw_messages) <= MAX_MESSAGES:
        raise HistoryError("messages must be a nonempty bounded array; inaccessible history is not a heartbeat")
    messages: list[Message] = []
    seen: dict[str, str] = {}
    total_rows = 0
    for raw in raw_messages:
        if not isinstance(raw, dict) or set(raw) != MESSAGE_FIELDS:
            raise HistoryError("each message must contain only message_digest and rows")
        digest = raw.get("message_digest")
        if not _is_digest(digest):
            raise HistoryError("message_digest must be a lowercase SHA-256 digest")
        rows = raw.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_MESSAGE_ROWS:
            raise HistoryError("message rows must be a bounded array of recruitment objects")
        total_rows += len(rows)
        if total_rows > MAX_TOTAL_ROWS:
            raise HistoryError("history exceeds the recruitment row limit")
        normalized = []
        for row in rows:
            try:
                _reject_private_strings(row)
                _validate_no_sensitive_material(row)
                item = bridge._normalize_row(row, source_id)
                # Unlike the single-message legacy bridge, absence is not a
                # positive claim that a historical listing is still open.
                if row.get("status") in (None, ""):
                    item["status"] = "unknown"
                normalized.append(item)
            except (bridge.BridgeError, SourceImportError, ValueError, TypeError) as exc:
                raise HistoryError("history recruitment row validation failed") from exc
        try:
            validated = bridge.build_batches(source_id, digest, normalized)
        except (bridge.BridgeError, SourceImportError, ValueError) as exc:
            raise HistoryError("history recruitment batch validation failed") from exc
        normalized = [job for batch in validated for job in batch["jobs"]]
        rows_digest = _digest(normalized)
        if digest in seen:
            if seen[digest] != rows_digest:
                raise HistoryError("one message digest was supplied with conflicting rows")
            continue
        seen[digest] = rows_digest
        messages.append(Message(digest, rows_digest, normalized))
    return History(source_id, value["history_complete"], messages)


def default_ledger_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Frostfire" / "chatgpt-history-ledger.json"


def _checked_ledger_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise HistoryError("ledger must not be a symbolic link")
    if path.resolve().is_relative_to(SCRIPT_ROOT):
        raise HistoryError("history ledger must be outside the Git project")
    return path


def _source_state() -> dict[str, Any]:
    return {"messages": {}, "items": {}, "history_complete": False, "last_history_digest": None, "last_message_count": 0}


def _validate_ledger(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"version", "sources"}
            or type(value.get("version")) is not int or value["version"] != LEDGER_VERSION
            or not isinstance(value.get("sources"), dict)):
        raise HistoryError("history ledger has an unsupported format")
    size = 0
    for source_id, item in value["sources"].items():
        # Retired monitors retain their receipts; changing the active roster
        # must neither invalidate an existing ledger nor reset its cursors.
        if source_id not in HISTORICAL_CHATGPT_SOURCE_IDS or not isinstance(item, dict) or set(item) != SOURCE_LEDGER_FIELDS:
            raise HistoryError("history ledger has unsupported source state")
        if (type(item["history_complete"]) is not bool
                or type(item["last_message_count"]) is not int or not 0 <= item["last_message_count"] <= MAX_MESSAGES
                or (item["last_history_digest"] is not None and not _is_digest(item["last_history_digest"]))
                or not isinstance(item["messages"], dict) or not isinstance(item["items"], dict)):
            raise HistoryError("history ledger metadata is invalid")
        for key, receipt in item["messages"].items():
            if (not _is_digest(key) or not isinstance(receipt, dict)
                    or set(receipt) != {"rows_digest", "row_count"}
                    or not _is_digest(receipt["rows_digest"])
                    or type(receipt["row_count"]) is not int or not 0 <= receipt["row_count"] <= MAX_MESSAGE_ROWS):
                raise HistoryError("history ledger message receipt is invalid")
        if any(not _is_digest(key) or not _is_digest(digest) for key, digest in item["items"].items()):
            raise HistoryError("history ledger item receipt is invalid")
        size += len(item["messages"]) + len(item["items"])
    if size > MAX_LEDGER_HASHES:
        raise HistoryError("history ledger reached its safe size limit")
    return value


def load_ledger(path: Path) -> dict[str, Any]:
    path = _checked_ledger_path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_LEDGER_BYTES + 1)
    except FileNotFoundError:
        return {"version": LEDGER_VERSION, "sources": {}}
    except OSError as exc:
        raise HistoryError("history ledger could not be read") from exc
    if len(raw) > MAX_LEDGER_BYTES:
        raise HistoryError("history ledger exceeds the size limit")
    return _validate_ledger(_json_loads(raw))


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never chmod a user's existing shared Documents/Application Support root.
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise HistoryError("ledger parent must be a private directory (mode 700)")


@contextlib.contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    path = _checked_ledger_path(path)
    try:
        _private_parent(path)
        lock = path.with_name(path.name + ".lock")
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HistoryError("another history submission is already using this ledger") from exc
            yield
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise HistoryError("history ledger lock is unavailable") from exc


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path = _checked_ledger_path(path)
    _validate_ledger(ledger)
    raw = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_LEDGER_BYTES:
        raise HistoryError("history ledger exceeds the size limit")
    descriptor = -1
    temporary_name: str | None = None
    try:
        _private_parent(path)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".chatgpt-history-", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise HistoryError("ledger must not be a symbolic link")
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise HistoryError("history ledger could not be updated") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _row_keys(source_id: str, job: dict[str, Any]) -> tuple[str, str]:
    return (
        _digest([source_id, "external", job["external_id"]]),
        _digest([source_id, "semantic", normalized_key(job["company"]), normalized_key(job["title"]),
                 normalized_key(job.get("city")), canonicalize_url(job["official_url"])]),
    )


def _held_reason(job: dict[str, Any], today: date) -> str | None:
    if job.get("status") == "closed":
        return "closed"
    closing = job.get("closing_date")
    if closing and date.fromisoformat(closing) <= today:
        # The legacy ingest endpoint closes on <= today. Hold today's date as
        # well instead of letting a historical replay close a last-known-good
        # listing before its actual deadline time has been independently read.
        return "deadline_on_or_before_today"
    return None


def _ingest_job(source_id: str, job: dict[str, Any]) -> dict[str, Any]:
    payload = bridge._legacy_ingest_batch({"source_id": source_id, "jobs": [job]})
    result = payload["jobs"][0]
    if job.get("status") == "unknown":
        result.pop("status", None)
        result["tags"] = list(dict.fromkeys([*result["tags"][:19], "开放状态待核验"]))
    # Keep unknown dates explicitly null even though the V1 intermediate
    # serialization excludes nulls. Do not derive dates from a message hash.
    result["opening_date"] = job.get("opening_date")
    result["closing_date"] = job.get("closing_date")
    return validate_ingest_payload({"source_id": source_id, "jobs": [result]})["jobs"][0]


def _content_digest(ingest_job: dict[str, Any]) -> str:
    return _digest({key: value for key, value in ingest_job.items() if key not in {"external_id", "source_item_id"}})


@dataclass
class Item:
    keys: tuple[str, str]
    content_digest: str
    job: dict[str, Any]
    delivered: bool = False
    held: bool = False


@dataclass
class MessageWork:
    message: Message
    previously_completed: bool = False
    dependencies: list[Item] = field(default_factory=list)
    held_rows: int = 0
    empty_delivered: bool = False

    def completed(self) -> bool:
        if self.previously_completed:
            return True
        if not self.message.rows:
            return self.empty_delivered
        return not self.held_rows and all(item.delivered and not item.held for item in self.dependencies)


@dataclass
class Batch:
    payload: dict[str, Any]
    items: list[Item] = field(default_factory=list)
    empty_messages: list[MessageWork] = field(default_factory=list)


@dataclass
class Plan:
    history: History
    messages: list[MessageWork]
    batches: list[Batch]
    duplicate_rows: int
    already_delivered_rows: int
    held_reasons: dict[str, int]

    def summary(self) -> dict[str, Any]:
        completed = sum(work.completed() for work in self.messages)
        return {
            "source_id": self.history.source_id,
            "input_history_complete": self.history.history_complete,
            "history_complete": self.history.history_complete and completed == len(self.messages),
            "messages": len(self.messages), "completed_messages": completed,
            "remaining_messages": len(self.messages) - completed,
            "rows": sum(len(work.message.rows) for work in self.messages),
            "batches": len(self.batches), "batch_sizes": [len(batch.items) for batch in self.batches],
            "eligible_rows": sum(len(batch.items) for batch in self.batches),
            "held_rows": sum(work.held_rows for work in self.messages),
            "held_reasons": self.held_reasons,
            "deduplicated_rows": self.duplicate_rows,
            "already_delivered_rows": self.already_delivered_rows,
            "heartbeat_batches": sum(not batch.items for batch in self.batches),
        }


def prepare_history(history: History, ledger: dict[str, Any], *, today: date | None = None,
                    batch_size: int = DEFAULT_BATCH_SIZE) -> Plan:
    validate_batch_size(batch_size)
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    state = ledger["sources"].get(history.source_id, _source_state())
    receipt_anchors: dict[str, int] = {}
    if state["items"]:
        # Digests alone cannot establish the chronology of disjoint history
        # windows. A changed previously delivered job may overwrite its old
        # contents only if that successful version occurs later (older) in
        # this newest-first input. Otherwise hold the ambiguous update.
        for message_index, message in enumerate(history.messages):
            for job in message.rows:
                keys = _row_keys(history.source_id, job)
                fingerprint = _content_digest(_ingest_job(history.source_id, job))
                if any(state["items"].get(key) == fingerprint for key in keys):
                    for key in keys:
                        receipt_anchors[key] = max(receipt_anchors.get(key, -1), message_index)
    representatives: dict[str, Item] = {}
    selected: list[Item] = []
    work_items: list[MessageWork] = []
    empty_messages: list[MessageWork] = []
    duplicate_rows = already_delivered_rows = 0
    held_reasons: dict[str, int] = {}
    for message_index, message in enumerate(history.messages):  # Newest first.
        receipt = state["messages"].get(message.digest)
        if receipt and receipt["rows_digest"] != message.rows_digest:
            raise HistoryError("an acknowledged message digest now has different rows")
        work = MessageWork(message, previously_completed=receipt is not None)
        work_items.append(work)
        if not message.rows:
            if not work.previously_completed:
                empty_messages.append(work)
            continue
        for job in message.rows:
            keys = _row_keys(history.source_id, job)
            reason = _held_reason(job, today)
            previous = next((representatives[key] for key in keys if key in representatives), None)
            if reason:
                work.held_rows += 1
                held_reasons[reason] = held_reasons.get(reason, 0) + 1
                if previous is None:
                    item = Item(keys, "", {}, held=True)
                    representatives.update(dict.fromkeys(keys, item))
                continue
            if previous is not None:
                duplicate_rows += 1
                work.dependencies.append(previous)
                representatives.update(dict.fromkeys(keys, previous))
                if previous.held:
                    work.held_rows += 1
                    held_reasons["superseded_by_newer_held_row"] = held_reasons.get("superseded_by_newer_held_row", 0) + 1
                continue
            ingest_job = _ingest_job(history.source_id, job)
            content_digest = _content_digest(ingest_job)
            delivered = work.previously_completed or any(state["items"].get(key) == content_digest for key in keys)
            if (not delivered and any(key in state["items"] for key in keys)
                    and not any(receipt_anchors.get(key, -1) > message_index for key in keys)):
                work.held_rows += 1
                held_reasons["unanchored_history_update"] = held_reasons.get("unanchored_history_update", 0) + 1
                representatives.update(dict.fromkeys(keys, Item(keys, "", {}, held=True)))
                continue
            item = Item(keys, content_digest, ingest_job, delivered=delivered)
            representatives.update(dict.fromkeys(keys, item))
            work.dependencies.append(item)
            if delivered:
                already_delivered_rows += 1
            else:
                selected.append(item)
    batches = []
    for index in range(0, len(selected), batch_size):
        chunk = selected[index:index + batch_size]
        batches.append(Batch(validate_ingest_payload({"source_id": history.source_id, "jobs": [item.job for item in chunk]}), chunk))
    if empty_messages:
        # Only explicitly empty, successfully parsed messages can authorize a
        # heartbeat. Filtering all rows or failing to read history cannot.
        empty_v1 = bridge.build_batches(history.source_id, _digest([work.message.digest for work in empty_messages]), [])
        batches.append(Batch(bridge._legacy_ingest_batch(empty_v1[0]), empty_messages=empty_messages))
    # Refuse a too-large ledger before a remote write, without evicting hashes
    # and silently weakening historical idempotency.
    projected = copy.deepcopy(ledger)
    projected_state = projected["sources"].setdefault(history.source_id, _source_state())
    for item in selected:
        projected_state["items"].update(dict.fromkeys(item.keys, item.content_digest))
    for work in work_items:
        projected_state["messages"][work.message.digest] = {"rows_digest": work.message.rows_digest, "row_count": len(work.message.rows)}
    _validate_ledger(projected)
    if len(json.dumps(projected).encode("utf-8")) > MAX_LEDGER_BYTES:
        raise HistoryError("history ledger would exceed the size limit")
    return Plan(history, work_items, batches, duplicate_rows, already_delivered_rows, held_reasons)


def validate_with_ingest_cli(batches: list[Batch]) -> None:
    """Every planned payload must pass the actual existing CLI before secrets."""
    for batch in batches:
        payload = validate_ingest_payload(batch.payload)
        try:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_ROOT / "scripts" / "frostfire_ingest.py"), "--dry-run"],
                input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                text=True, capture_output=True, check=False, timeout=30,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
            )
            result = _json_loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, HistoryError) as exc:
            raise HistoryError("ingest dry-run validation failed") from exc
        if (not isinstance(result, dict) or result.get("dry_run") is not True
                or result.get("source_id") != payload["source_id"]
                or type(result.get("jobs")) is not int or result["jobs"] != len(payload["jobs"])
                or result.get("heartbeat") is not (not payload["jobs"])):
            raise HistoryError("ingest dry-run validation failed")


def _submit_checked(batch: Batch, token: str, timeout: float) -> dict[str, int]:
    try:
        response = submit_payload(batch.payload, token, timeout)
    except urllib.error.HTTPError as exc:
        raise SubmissionError("http_error", http_status=exc.code if type(exc.code) is int else None) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SubmissionError("network_error") from None
    except IngestInputError:
        raise SubmissionError("invalid_response") from None
    if not isinstance(response, tuple) or len(response) != 2:
        raise SubmissionError("invalid_response")
    status_code, raw = response
    if type(status_code) is not int or not 200 <= status_code < 300:
        raise SubmissionError("http_error", http_status=status_code if type(status_code) is int else None)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
        raise SubmissionError("invalid_response")
    try:
        parsed = _json_loads(bytes(raw))
    except HistoryError:
        raise SubmissionError("invalid_response") from None
    expected = len(batch.payload["jobs"])
    if not isinstance(parsed, dict) or type(parsed.get("received")) is not int or parsed["received"] != expected:
        raise SubmissionError("received_mismatch")
    safe = {"http_status": status_code}
    for key in RESULT_COUNTERS:
        if key in parsed:
            if type(parsed[key]) is not int or not 0 <= parsed[key] <= expected:
                raise SubmissionError("invalid_response_counts")
            safe[key] = parsed[key]
    return safe


def _advance_receipts(ledger: dict[str, Any], plan: Plan, batch: Batch | None = None) -> None:
    state = ledger["sources"].setdefault(plan.history.source_id, _source_state())
    if batch is not None:
        for item in batch.items:
            item.delivered = True
            state["items"].update(dict.fromkeys(item.keys, item.content_digest))
        for work in batch.empty_messages:
            work.empty_delivered = True
    for work in plan.messages:
        if work.completed():
            state["messages"][work.message.digest] = {"rows_digest": work.message.rows_digest, "row_count": len(work.message.rows)}
    state["history_complete"] = plan.summary()["history_complete"]
    state["last_history_digest"] = _digest([work.message.digest for work in plan.messages])
    state["last_message_count"] = len(plan.messages)


def submit_plan(plan: Plan, ledger: dict[str, Any], path: Path, *, timeout: float) -> tuple[int, dict[str, Any]]:
    # Caller holds the local process lock and has preflighted *all* batches.
    results = []
    error: SubmissionError | None = None
    if plan.batches:
        token = read_keychain_token()
        if not token:
            raise HistoryError("the macOS ingest Keychain item is unavailable")
        for batch in plan.batches:
            try:
                result = _submit_checked(batch, token, timeout)
            except SubmissionError as exc:
                error = exc
                break
            _advance_receipts(ledger, plan, batch)
            save_ledger(path, ledger)
            results.append(result)
    else:
        # No fresh POST for already-acknowledged identical rows. A new message
        # can reuse previous successful item receipts, never an inferred one.
        before = _digest(ledger)
        if any(work.completed() for work in plan.messages):
            _advance_receipts(ledger, plan)
            if _digest(ledger) != before:
                save_ledger(path, ledger)
    summary = plan.summary()
    summary.update({
        "dry_run": False,
        "status": "partial_failure" if error else ("held" if summary["remaining_messages"] else ("submitted" if results else "unchanged")),
        "successful_batches": len(results), "failed_batches": int(error is not None),
        "unattempted_batches": len(plan.batches) - len(results) - int(error is not None),
        "results": results,
    })
    if error:
        summary["error"] = {"code": error.code}
        if error.http_status is not None:
            summary["error"]["http_status"] = error.http_status
        summary["history_complete"] = False
    return (4 if error else 0), summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="offline validation and counts (the default)")
    modes.add_argument("--emit", action="store_true", help="emit only reviewed public ingest batches; no state change")
    modes.add_argument("--submit", action="store_true", help="submit after all dry-runs; use only the macOS Keychain token")
    parser.add_argument("--ledger-file", type=Path, default=default_ledger_path())
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="jobs per HTTP request (1-100; default: 25); continue pages until all updates are processed")
    args = parser.parse_args(argv)
    try:
        validate_batch_size(args.batch_size)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        history = parse_history(_read_stdin())
        path = _checked_ledger_path(args.ledger_file)
        with ledger_lock(path) if args.submit else contextlib.nullcontext():
            ledger = load_ledger(path)
            plan = prepare_history(history, ledger, batch_size=args.batch_size)
            validate_with_ingest_cli(plan.batches)
            if args.emit:
                print(json.dumps([batch.payload for batch in plan.batches], ensure_ascii=False, indent=2))
                return 0
            if args.submit:
                code, summary = submit_plan(plan, ledger, path, timeout=args.timeout)
            else:
                code, summary = 0, {"dry_run": True, "status": "ready" if plan.batches else "unchanged_or_held", **plan.summary()}
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            return code
    except (HistoryError, bridge.BridgeError, SourceImportError, IngestInputError) as exc:
        # Bridge validation exceptions contain only safe field paths/messages.
        print(f"history error: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, RecursionError):
        print("history error: local validation or state operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
