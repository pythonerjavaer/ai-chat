#!/usr/bin/env python3
"""Submit normalized Future Radar jobs to the Frostfire ingest endpoint.

The payload is read from stdin. It may be one job object, an array of up to ten
job objects, the API request shape ``{"jobs": [...]}``, or an empty heartbeat
``{"jobs": [], "source_id": "chatgpt-radar-01", ...}``. The ingest token is
read from ``FROSTFIRE_INGEST_TOKEN`` or, on macOS, from the Keychain service
``frostfire-recruitment-ingest``. The token is never written to stdout/stderr.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Sequence


ENDPOINT = "https://frostfire-ai.onrender.com/api/recruitment/ingest"
TOKEN_ENV = "FROSTFIRE_INGEST_TOKEN"
KEYCHAIN_SERVICE = "frostfire-recruitment-ingest"
MAX_STDIN_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_JOBS = 10
MAX_EVIDENCE_ITEMS = 12
MAX_EVIDENCE_LENGTH = 280

BATCH_FIELDS = {"jobs", "source_id", "source_updated_at"}
JOB_FIELDS = {
    "company", "title", "city", "employer_type", "industry", "official_url",
    "source", "opening_date", "closing_date", "requirements", "tags", "status",
    "source_id", "source_thread_id", "source_item_id", "source_updated_at",
    "external_id", "evidence",
}
REQUIRED_JOB_FIELDS = {"company", "title", "city", "official_url"}
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
EMAIL_PATTERN = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)"),
    re.compile(r"(?<!\d)\+\d{8,15}(?!\d)"),
)

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_SECRET = 3
EXIT_NETWORK = 4
EXIT_HTTP = 5
EXIT_RESPONSE = 6


class InputError(ValueError):
    """The stdin payload cannot be submitted to the ingest API."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read Future Radar jobs from stdin and submit them to Frostfire.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and summarize stdin without reading a token or using the network",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        metavar="SECONDS",
        help="network timeout in seconds (default: 45; allowed: 1-300)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    return args


def validate_source_id(value: Any, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not SOURCE_ID_PATTERN.fullmatch(value.strip()):
        raise InputError(
            "source_id must be 1-64 characters using letters, digits, '.', '_', ':', or '-'"
        )
    return value.strip()


def validate_evidence(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_ITEMS:
        raise InputError(f"evidence must be an array with at most {MAX_EVIDENCE_ITEMS} items")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise InputError("every evidence item must be a string")
        if not item or len(item) > MAX_EVIDENCE_LENGTH:
            raise InputError(
                f"every evidence item must contain 1-{MAX_EVIDENCE_LENGTH} characters"
            )
        text = item.strip()
        if not text:
            raise InputError(
                f"every evidence item must contain 1-{MAX_EVIDENCE_LENGTH} characters"
            )
        if "\n" in text or "\r" in text:
            raise InputError("evidence must be a short single-line statement")
        if EMAIL_PATTERN.search(text) or any(pattern.search(text) for pattern in PHONE_PATTERNS):
            raise InputError("evidence must not contain email addresses or phone numbers")
        result.append(text)
    return result


def validate_source_updated_at(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError("source_updated_at must be a non-empty ISO 8601 string")
    text = value.strip()
    try:
        dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError("source_updated_at must be a valid ISO 8601 date-time") from exc
    return text


def validate_job(job: Any) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise InputError("every job must be a JSON object")
    extra = sorted(set(job) - JOB_FIELDS)
    if extra:
        raise InputError(f"job contains unsupported properties: {', '.join(extra)}")
    missing = sorted(REQUIRED_JOB_FIELDS - set(job))
    if missing:
        raise InputError(f"job is missing required properties: {', '.join(missing)}")
    for field in REQUIRED_JOB_FIELDS:
        if not isinstance(job[field], str) or not job[field].strip():
            raise InputError(f"job property '{field}' must be a non-empty string")
    if not job["official_url"].strip().startswith("https://"):
        raise InputError("official_url must start with https://")
    normalized = dict(job)
    if "source_id" in normalized:
        normalized["source_id"] = validate_source_id(normalized["source_id"], required=True)
    if "source_updated_at" in normalized:
        normalized["source_updated_at"] = validate_source_updated_at(
            normalized["source_updated_at"]
        )
    if "evidence" in normalized:
        normalized["evidence"] = validate_evidence(normalized["evidence"])
    if "status" in normalized and normalized["status"] not in {"open", "closed"}:
        raise InputError("status must be 'open' or 'closed'")
    return normalized


def normalize_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        payload = {"jobs": value}
    elif isinstance(value, dict) and "jobs" in value:
        payload = dict(value)
    elif isinstance(value, dict) and "source_id" in value and set(value) <= BATCH_FIELDS:
        payload = {"jobs": [], **value}
    elif isinstance(value, dict):
        payload = {"jobs": [value]}
    else:
        raise InputError("stdin must contain one job object, a job array, or an ingest batch")

    extra = sorted(set(payload) - BATCH_FIELDS)
    if extra:
        raise InputError(f"batch contains unsupported properties: {', '.join(extra)}")
    jobs = payload.get("jobs", [])
    if not isinstance(jobs, list):
        raise InputError("'jobs' must be an array")
    if len(jobs) > MAX_JOBS:
        raise InputError(f"a batch may contain at most {MAX_JOBS} jobs")
    normalized: dict[str, Any] = {"jobs": [validate_job(job) for job in jobs]}
    if "source_id" in payload:
        normalized["source_id"] = validate_source_id(
            payload["source_id"], required=not jobs
        )
    if not jobs and not normalized.get("source_id"):
        raise InputError("source_id is required when jobs is empty")
    if "source_updated_at" in payload:
        normalized["source_updated_at"] = validate_source_updated_at(
            payload["source_updated_at"]
        )
    return normalized


def read_payload(stream: Any | None = None) -> dict[str, Any]:
    if stream is None:
        stream = sys.stdin
    raw = stream.read(MAX_STDIN_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
        raise InputError(f"stdin exceeds {MAX_STDIN_BYTES} bytes")
    if not raw.strip():
        raise InputError("stdin is empty")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"stdin is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from exc
    return normalize_payload(value)


def read_keychain_token() -> str:
    if platform.system() != "Darwin":
        return ""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def load_token() -> str:
    token = os.getenv(TOKEN_ENV, "").strip()
    if token:
        return token
    return read_keychain_token()


def submit_payload(
    payload: dict[str, Any],
    token: str,
    timeout: float,
) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Frostfire-Codex-Heartbeat/1.0",
            "X-Recruitment-Token": token,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise InputError("server response is unexpectedly large")
        return int(getattr(response, "status", 200)), raw


def print_json(value: Any, *, stream: Any | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "[redacted]") if secret else value


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = read_payload()
    except InputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if args.dry_run:
        print_json({
            "dry_run": True,
            "endpoint": ENDPOINT,
            "heartbeat": not payload["jobs"],
            "jobs": len(payload["jobs"]),
            "source_id": payload.get("source_id"),
        })
        return EXIT_OK

    token = load_token()
    if not token:
        print(
            f"secret error: set {TOKEN_ENV} or add macOS Keychain service "
            f"'{KEYCHAIN_SERVICE}'",
            file=sys.stderr,
        )
        return EXIT_SECRET

    try:
        status, raw = submit_payload(payload, token, args.timeout)
    except urllib.error.HTTPError as exc:
        # Never print request headers: they contain the ingest token.
        detail = ""
        try:
            response_body = exc.read(MAX_RESPONSE_BYTES)
            parsed = json.loads(response_body.decode("utf-8"))
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail", ""))[:500]
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        detail = redact_secret(detail, token)
        suffix = f": {detail}" if detail else ""
        print(f"HTTP error {exc.code}{suffix}", file=sys.stderr)
        return EXIT_HTTP
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", None)
        message = redact_secret(str(reason or exc), token)[:500]
        print(f"network error: {message}", file=sys.stderr)
        return EXIT_NETWORK
    except InputError as exc:
        print(f"response error: {exc}", file=sys.stderr)
        return EXIT_RESPONSE

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        print(f"response error: HTTP {status} did not return valid JSON", file=sys.stderr)
        return EXIT_RESPONSE
    print_json(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
