#!/usr/bin/env python3
"""Convert a pre-sanitized browser message into controlled recruitment batches.

This is a narrow trust-boundary tool for a local browser automation.  It does
not open ChatGPT, accept conversation URLs, read cookies, or retain message
content.  stdin must contain exactly one logical source, one message ID, and a
list of already-structured recruitment table rows::

    {"source_id": "chatgpt-radar-01", "message_id": "message-42", "rows": [...]}

The message ID is hashed immediately.  Neither it nor any official URL is
written to the cursor file.  Successful submissions advance an atomic local
cursor; validation, emission, and dry-runs never advance it.  A retry after a
partial submission is safe because every batch and job identifier is stable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any, Sequence


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from backend.future_radar.normalization import clean_text, stable_digest  # noqa: E402
from backend.recruitment_watch import WatchFetchError, validate_public_https_url  # noqa: E402
from scripts.frostfire_ingest import (  # noqa: E402
    InputError as IngestInputError,
    KEYCHAIN_SERVICE,
    normalize_payload as normalize_ingest_payload,
    read_keychain_token,
    submit_payload,
)
from scripts.frostfire_source_import import (  # noqa: E402
    ImportError as SourceImportError,
    UUID_PATTERN,
    _validate_logical_source_id,
    _validated_payload,
)


MAX_INPUT_BYTES = 2_000_000
MAX_ROWS = 100
MAX_BATCH_JOBS = 10
MAX_CURSOR_BYTES = 256_000
CURSOR_VERSION = 1

TOP_LEVEL_FIELDS = frozenset({"source_id", "message_id", "rows"})
ROW_FIELDS = frozenset({
    "row_id", "source_item_id", "external_id", "company", "title", "city", "region",
    "employer_type", "industry", "primary_category", "organization_category",
    "industry_tags", "role_tags", "official_url", "application_url",
    "opening_date", "closing_date", "status", "description",
    "responsibilities", "requirements", "tags", "evidence",
})
REQUIRED_ROW_FIELDS = frozenset({"company", "title", "official_url"})
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SECRET_PATTERN = re.compile(
    r"(?i)(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b|"
    r"\b(?:authorization|cookie|set-cookie|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|password|secret)\s*[:=])"
)
EMAIL_PATTERN = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)"),
    re.compile(r"(?<!\d)\+\d{8,15}(?!\d)"),
)


class BridgeError(ValueError):
    """A safe bridge validation or cursor error."""


def default_cursor_path() -> Path:
    """Return a local, non-repository state path containing only hashes."""
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Frostfire"
        / "chatgpt-bridge-cursors.json"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read one sanitized ChatGPT recruitment table message from stdin and "
            "emit or submit FROSTFIRE_SYNC_V1 batches."
        )
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report counts without reading Keychain, submitting, or advancing cursor",
    )
    modes.add_argument(
        "--submit",
        action="store_true",
        help="submit all batches and advance the cursor only after every batch succeeds",
    )
    parser.add_argument(
        "--cursor-file",
        type=Path,
        default=default_cursor_path(),
        help="local hash-only cursor file (default: macOS Application Support)",
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    return args


def _read_stdin(stream: Any | None = None) -> Any:
    stream = stream or sys.stdin
    raw = stream.buffer.read(MAX_INPUT_BYTES + 1) if hasattr(stream, "buffer") else stream.read()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_INPUT_BYTES:
        raise BridgeError(f"stdin exceeds {MAX_INPUT_BYTES} bytes")
    if not raw.strip():
        raise BridgeError("stdin is empty")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("stdin must be valid UTF-8 JSON") from exc


def _message_digest(source_id: str, message_id: str) -> str:
    return hashlib.sha256(f"{source_id}\0{message_id}".encode("utf-8")).hexdigest()


def _validate_message_id(value: Any) -> str:
    if not isinstance(value, str) or not MESSAGE_ID_PATTERN.fullmatch(value):
        raise BridgeError("message_id must be a 1-256 character logical identifier")
    if UUID_PATTERN.fullmatch(value):
        raise BridgeError("message_id must not be a conversation UUID")
    if SECRET_PATTERN.search(value) or EMAIL_PATTERN.search(value) or any(
        pattern.search(value) for pattern in PHONE_PATTERNS
    ):
        raise BridgeError("message_id contains forbidden sensitive material")
    return value


def _require_string(row: dict[str, Any], field: str, *, limit: int) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise BridgeError(f"row.{field} must be a string")
    normalized = clean_text(value, limit=limit + 1)
    if not normalized:
        raise BridgeError(f"row.{field} must not be empty")
    if len(normalized) > limit:
        raise BridgeError(f"row.{field} exceeds its maximum length")
    return normalized


def _optional_string(row: dict[str, Any], field: str, *, limit: int) -> str:
    value = row.get(field)
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise BridgeError(f"row.{field} must be a string")
    normalized = clean_text(value, limit=limit + 1)
    if len(normalized) > limit:
        raise BridgeError(f"row.{field} exceeds its maximum length")
    return normalized


def _string_list(row: dict[str, Any], field: str, *, max_items: int, limit: int) -> list[str]:
    value = row.get(field)
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise BridgeError(f"row.{field} must be an array with at most {max_items} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise BridgeError(f"row.{field} items must be strings")
        if field == "evidence" and ("\n" in item or "\r" in item):
            raise BridgeError("row.evidence items must be single-line strings")
        normalized = clean_text(item, limit=limit + 1)
        if not normalized:
            raise BridgeError(f"row.{field} items must not be empty")
        if len(normalized) > limit:
            raise BridgeError(f"row.{field} items exceed their maximum length")
        result.append(normalized)
    return result


def _public_https_url(row: dict[str, Any], field: str, *, required: bool) -> str:
    value = (
        _require_string(row, field, limit=2_000)
        if required
        else _optional_string(row, field, limit=2_000)
    )
    if not value:
        return ""
    try:
        return validate_public_https_url(value, resolve_dns=False)
    except WatchFetchError as exc:
        raise BridgeError(f"row.{field} must be a public HTTPS URL") from exc


def _date_value(row: dict[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise BridgeError(f"row.{field} must be an ISO date string")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise BridgeError(f"row.{field} must be an ISO date string") from exc


def _normalize_row(row: Any, source_id: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise BridgeError("every row must be an object")
    if set(row) - ROW_FIELDS:
        # Never echo an unexpected key because its name may itself contain a
        # secret copied from browser state.
        raise BridgeError("row contains unsupported properties")
    if REQUIRED_ROW_FIELDS - set(row):
        raise BridgeError("row is missing company, title, or official_url")

    company = _require_string(row, "company", limit=160)
    title = _require_string(row, "title", limit=280)
    official_url = _public_https_url(row, "official_url", required=True)
    application_url = _public_https_url(row, "application_url", required=False)
    external = _optional_string(row, "external_id", limit=180)
    row_id = _optional_string(row, "row_id", limit=180)
    source_item_id = _optional_string(row, "source_item_id", limit=180)
    if not external:
        if source_item_id or row_id:
            external = stable_digest(
                source_id,
                source_item_id or row_id,
                prefix="bridge-job",
                length=32,
            )
        else:
            # Match the service's semantic fallback so the same job can be
            # reconciled across discovery sources even when its URL changes.
            external = stable_digest(
                company,
                title,
                _optional_string(row, "city", limit=160),
                prefix="job",
            )

    status = _optional_string(row, "status", limit=24).casefold() or "open"
    if status not in {"open", "closed", "unknown"}:
        raise BridgeError("row.status must be open, closed, or unknown")

    tags = [
        tag
        for tag in _string_list(row, "tags", max_items=28, limit=80)
        if tag not in {"链接已验证", "标题已验证", "官方已核验"}
    ]
    tags = list(dict.fromkeys([*tags, "受控结构化导入", "待官方核验"]))
    normalized: dict[str, Any] = {
        "external_id": external,
        "company": company,
        "title": title,
        "city": _optional_string(row, "city", limit=160),
        "region": _optional_string(row, "region", limit=160),
        "employer_type": _optional_string(row, "employer_type", limit=80),
        "industry": _optional_string(row, "industry", limit=120),
        "organization_category": _optional_string(
            row, "organization_category", limit=80
        ),
        "industry_tags": _string_list(
            row, "industry_tags", max_items=30, limit=80
        ),
        "role_tags": _string_list(row, "role_tags", max_items=30, limit=80),
        "official_url": official_url,
        "application_url": application_url or official_url,
        "opening_date": _date_value(row, "opening_date"),
        "closing_date": _date_value(row, "closing_date"),
        "status": status,
        # Browser/ChatGPT extraction is discovery evidence.  Only the server's
        # official-page verifier may promote this value later.
        "verification_status": "pending",
        "confidence_score": 0.55,
        "description": _optional_string(row, "description", limit=2_000),
        "responsibilities": _optional_string(row, "responsibilities", limit=2_000),
        "requirements": _optional_string(row, "requirements", limit=2_000),
        "tags": tags,
        "evidence": _string_list(row, "evidence", max_items=12, limit=280),
    }
    primary_category = _optional_string(row, "primary_category", limit=80)
    if primary_category:
        normalized["primary_category"] = primary_category
    return normalized


def parse_browser_message(value: Any) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise BridgeError("stdin must contain one browser message object")
    if set(value) != TOP_LEVEL_FIELDS:
        raise BridgeError("input must contain only source_id, message_id, and rows")
    try:
        # Reuse the source importer's UUID rejection and logical-ID grammar.
        source_id = _validate_logical_source_id(value["source_id"])
    except (KeyError, SourceImportError, TypeError) as exc:
        raise BridgeError("source_id must be a logical source label") from exc
    message_id = _validate_message_id(value.get("message_id"))
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise BridgeError(f"rows must be an array with at most {MAX_ROWS} items")
    normalized = [_normalize_row(row, source_id) for row in rows]
    return source_id, _message_digest(source_id, message_id), normalized


def build_batches(
    source_id: str,
    message_digest: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunks = [rows[index : index + MAX_BATCH_JOBS] for index in range(0, len(rows), MAX_BATCH_JOBS)]
    if not chunks:
        chunks = [[]]
    batches: list[dict[str, Any]] = []
    for index, jobs in enumerate(chunks):
        try:
            payload = _validated_payload(
                {
                    "version": "FROSTFIRE_SYNC_V1",
                    "source_id": source_id,
                    "snapshot_complete": False,
                    "jobs": jobs,
                },
                source_id,
            )
        except SourceImportError as exc:
            raise BridgeError(str(exc)) from exc
        canonical = json.dumps(
            payload["jobs"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload["batch_id"] = "bridge-" + hashlib.sha256(
            f"{source_id}\0{message_digest}\0{index}\0{canonical}".encode("utf-8")
        ).hexdigest()[:32]
        batches.append(payload)
    return batches


def _legacy_ingest_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Route browser candidates through the server's quarantine verifier.

    ``/api/future-radar/sync`` stores discovery rows but intentionally cannot
    verify them.  The existing recruitment ingest endpoint opens the official
    page, checks company/title/campus/closed state, and only then promotes the
    row to the public pool.  Keep V1 as the local interchange shape while using
    that stronger production path for submission.
    """
    jobs = []
    for item in batch.get("jobs", []):
        jobs.append({
            "company": item["company"],
            "title": item["title"],
            "city": item.get("city") or "地点待公告确认",
            "employer_type": item.get("employer_type") or "重点雇主",
            "industry": item.get("industry") or "",
            "official_url": item["official_url"],
            "source": "ChatGPT 受控同步",
            "opening_date": item.get("opening_date"),
            "closing_date": item.get("closing_date"),
            "requirements": item.get("requirements") or item.get("description") or "",
            "tags": list(item.get("tags") or [])[:20],
            "status": "closed" if item.get("status") == "closed" else "open",
            "source_id": batch["source_id"],
            "external_id": item.get("external_id"),
            "evidence": list(item.get("evidence") or []),
        })
    try:
        return normalize_ingest_payload({
            "source_id": batch["source_id"],
            "jobs": jobs,
        })
    except IngestInputError as exc:
        raise BridgeError(str(exc)) from exc


def _safe_response(value: Any) -> dict[str, Any]:
    if isinstance(value, tuple) and len(value) == 2:
        status, raw = value
        try:
            parsed = json.loads(bytes(raw).decode("utf-8"))
        except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise BridgeError("server response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise BridgeError("server response was not an object")
        value = {"http_status": int(status), **parsed}
    if not isinstance(value, dict):
        raise BridgeError("server response was not an object")
    # Return only the public status contract; never echo a provider/server
    # detail or a test fixture field that could contain a token.
    allowed = {
        "http_status", "received", "accepted", "new", "updated",
        "duplicates", "stale", "pending", "rejected", "closed", "event_id",
    }
    return {key: value[key] for key in allowed if key in value}


def _submit_batch(payload: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    """Submit without exposing request metadata, response bodies, or secrets."""
    try:
        return _safe_response(submit_payload(payload, token, timeout))
    except urllib.error.HTTPError as exc:
        # ``HTTPError`` carries the request URL, headers, reason and response
        # body.  The numeric status is the only attribute safe to report.
        code = exc.code if isinstance(exc.code, int) else "error"
        raise BridgeError(f"submission failed (HTTP {code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise BridgeError("submission endpoint is temporarily unavailable") from None


def load_cursor(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise BridgeError("cursor file must not be a symbolic link")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"version": CURSOR_VERSION, "sources": {}}
    except OSError as exc:
        raise BridgeError("cursor file could not be read") from exc
    if len(raw) > MAX_CURSOR_BYTES:
        raise BridgeError("cursor file is unexpectedly large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("cursor file is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "sources"}
        or value.get("version") != CURSOR_VERSION
        or not isinstance(value.get("sources"), dict)
    ):
        raise BridgeError("cursor file has an unsupported format")
    for raw_source_id, item in value["sources"].items():
        try:
            _validate_logical_source_id(raw_source_id)
        except SourceImportError as exc:
            raise BridgeError("cursor file has an unsupported format") from exc
        if (
            not isinstance(item, dict)
            or set(item) != {"message_digest"}
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(item.get("message_digest", ""))
            )
        ):
            raise BridgeError("cursor file has an unsupported format")
    return value


def cursor_has_message(cursor: dict[str, Any], source_id: str, digest: str) -> bool:
    item = cursor.get("sources", {}).get(source_id)
    return isinstance(item, dict) and item.get("message_digest") == digest


def save_cursor(path: Path, cursor: dict[str, Any], source_id: str, digest: str) -> None:
    controlled = {
        "version": CURSOR_VERSION,
        "sources": {
            key: {"message_digest": value["message_digest"]}
            for key, value in cursor.get("sources", {}).items()
            if isinstance(key, str)
            and isinstance(value, dict)
            and re.fullmatch(r"[0-9a-f]{64}", str(value.get("message_digest", "")))
        },
    }
    controlled["sources"][source_id] = {"message_digest": digest}
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise BridgeError("cursor file must not be a symbolic link")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".chatgpt-bridge-cursor-", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(controlled, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    except BridgeError:
        raise
    except OSError as exc:
        raise BridgeError("cursor file could not be updated") from exc


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        source_id, message_digest, rows = parse_browser_message(_read_stdin())
        cursor = load_cursor(args.cursor_file)
        unchanged = cursor_has_message(cursor, source_id, message_digest)
        batches = [] if unchanged else build_batches(source_id, message_digest, rows)

        if args.dry_run:
            _print_json({
                "dry_run": True,
                "status": "unchanged" if unchanged else "ready",
                "source_id": source_id,
                "rows": len(rows),
                "batches": len(batches),
                "batch_sizes": [len(batch["jobs"]) for batch in batches],
                "heartbeat": bool(batches and not rows),
            })
            return 0

        if not args.submit:
            _print_json(batches[0] if len(batches) == 1 else batches)
            return 0

        if unchanged:
            _print_json({
                "status": "unchanged", "source_id": source_id,
                "rows": 0, "batches": 0, "results": [],
            })
            return 0

        token = read_keychain_token()
        if not token:
            raise BridgeError(
                f"macOS Keychain service '{KEYCHAIN_SERVICE}' does not contain an ingest token"
            )
        results = []
        for batch in batches:
            ingest_batch = _legacy_ingest_batch(batch)
            results.append(_submit_batch(ingest_batch, token, args.timeout))
        save_cursor(args.cursor_file, cursor, source_id, message_digest)
        _print_json({
            "status": "submitted",
            "source_id": source_id,
            "rows": len(rows),
            "batches": len(batches),
            "results": results,
        })
        return 0
    except (BridgeError, SourceImportError) as exc:
        print(f"bridge error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
