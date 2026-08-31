#!/usr/bin/env python3
"""Create or submit a safe ``FROSTFIRE_SYNC_V1`` source batch.

Supported inputs are deliberately public or user-supplied: a ChatGPT
``/share/`` snapshot containing a complete sync JSON object, a local sync JSON
file, one public article URL, or a public RSS/Atom feed.  The script never logs
in to ChatGPT, reads browser cookies, calls a private conversation endpoint, or
turns a discovery article into a verified job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import ValidationError


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from backend.future_radar.normalization import clean_text, stable_digest  # noqa: E402
from backend.future_radar.schemas import FrostFireSyncV1  # noqa: E402
from backend.recruitment_watch import (  # noqa: E402
    WatchFetchError,
    fetch_watch_page,
    validate_public_https_url,
)


ENDPOINT = "https://frostfire-ai.onrender.com/api/future-radar/sync"
TOKEN_ENV = "FROSTFIRE_INGEST_TOKEN"
KEYCHAIN_SERVICE = "frostfire-recruitment-ingest"
MAX_INPUT_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 1_000_000
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
CHATGPT_SHARE_PATH = re.compile(r"^/share/[A-Za-z0-9-]+/?$")
SYNC_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
CAMPUS_MARKERS = ("校园招聘", "秋季招聘", "秋招", "校招", "应届", "graduate", "campus")
EMAIL_PATTERN = re.compile(r"(?i)\b[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)"),
    re.compile(r"(?<!\d)\+\d{8,15}(?!\d)"),
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:authorization|cookie|set-cookie|api[_ -]?key|access[_ -]?token|"
        r"refresh[_ -]?token)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{10,}\b"),
)
SECRET_KEY_MARKERS = (
    "authorization", "cookie", "password", "secret", "api_key", "apikey",
    "access_token", "refresh_token",
)
URL_FIELD_NAMES = {"official_url", "application_url", "article_url"}
SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie",
    "key", "password", "refresh_token", "secret", "sig", "signature", "token",
}


class ImportError(ValueError):
    """The source cannot be converted into a safe sync payload."""


def _safe_location(path: Sequence[Any]) -> str:
    parts: list[str] = []
    for raw in path:
        if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
            parts.append(str(raw))
            continue
        value = str(raw)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", value):
            parts.append(value)
        else:
            parts.append("field")
    return ".".join(parts) or "payload"


def _validate_logical_source_id(value: str) -> str:
    if not isinstance(value, str) or not SOURCE_ID_PATTERN.fullmatch(value):
        raise ImportError("source_id must be a 1-64 character logical label")
    if UUID_PATTERN.fullmatch(value):
        raise ImportError("source_id must be a logical label, not a conversation UUID")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Future Radar batch from a public or local controlled source."
    )
    parser.add_argument("--source-id", required=True, help="stable logical source ID")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--chatgpt-share",
        metavar="URL",
        help="public https://chatgpt.com/share/... snapshot containing FROSTFIRE_SYNC_V1",
    )
    inputs.add_argument(
        "--structured-json",
        metavar="PATH",
        help="local FROSTFIRE_SYNC_V1 JSON file; use - for stdin",
    )
    inputs.add_argument("--public-article", metavar="URL", help="one public HTTPS article")
    inputs.add_argument("--public-feed", metavar="URL", help="one public RSS/Atom URL")
    parser.add_argument("--title", help="required title for --public-article")
    parser.add_argument("--publisher", default="", help="public publisher/account display name")
    parser.add_argument("--published-at", help="optional ISO 8601 publication time")
    parser.add_argument(
        "--submit", action="store_true", help="submit with the ingest token instead of printing JSON"
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    try:
        _validate_logical_source_id(args.source_id)
    except ImportError as exc:
        parser.error(str(exc))
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    if args.public_article and not clean_text(args.title, limit=300):
        parser.error("--title is required with --public-article")
    return args


def _safe_chatgpt_share_url(value: str) -> str:
    try:
        safe = validate_public_https_url(value, resolve_dns=True)
    except WatchFetchError as exc:
        raise ImportError(str(exc)) from exc
    parsed = urllib.parse.urlsplit(safe)
    if parsed.hostname != "chatgpt.com" or not CHATGPT_SHARE_PATH.fullmatch(parsed.path):
        raise ImportError(
            "ChatGPT input must be a public https://chatgpt.com/share/... link; private /c/... links are not supported."
        )
    if parsed.query:
        raise ImportError("ChatGPT share links must not contain query parameters.")
    return safe


def _json_candidates(text: str) -> Iterable[dict[str, Any]]:
    fenced = list(SYNC_FENCE.finditer(text))
    for match in reversed(fenced):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            yield candidate

    # Some public renderers expose code text without Markdown fences.  Decode
    # only objects near the explicit protocol marker, with a hard candidate
    # cap so untrusted pages cannot trigger unbounded quadratic work.
    decoder = json.JSONDecoder()
    starts = [match.start() for match in re.finditer(r"\{", text)]
    for start in reversed(starts[-2_000:]):
        window = text[start : start + 250_000]
        if "FROSTFIRE_SYNC_V1" not in window:
            continue
        try:
            candidate, _ = decoder.raw_decode(window)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            yield candidate


def _redact_untrusted_text(value: Any, *, limit: int) -> str:
    # Redact the complete bounded page text before truncation so a credential
    # crossing the excerpt boundary cannot leave a recognizable prefix.
    text = clean_text(value, limit=1_500_000)
    text = EMAIL_PATTERN.sub("[redacted-email]", text)
    for pattern in PHONE_PATTERNS:
        text = pattern.sub("[redacted-phone]", text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted-secret]", text)
    # UUIDs in article prose have no recruitment value and can be private
    # conversation identifiers. Stable item IDs are handled separately.
    text = UUID_PATTERN.sub("[redacted-uuid]", text)
    return clean_text(text, limit=limit)


def _validate_no_sensitive_material(value: Any, *, path: tuple[str, ...] = ()) -> None:
    """Reject secrets/PII without including the rejected value in errors."""
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            folded = key.casefold()
            if folded == "token" or any(marker in folded for marker in SECRET_KEY_MARKERS):
                raise ImportError("sensitive properties are not allowed in source payloads")
            _validate_no_sensitive_material(item, path=(*path, key))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_no_sensitive_material(item, path=(*path, str(index)))
        return
    if not isinstance(value, str):
        return

    location = _safe_location(path)
    decoded = urllib.parse.unquote(value)
    if any(pattern.search(decoded) for pattern in SECRET_PATTERNS):
        raise ImportError(f"credential-like content is not allowed at {location}")
    field = path[-1] if path else ""
    if field in URL_FIELD_NAMES:
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError as exc:
            raise ImportError(f"invalid URL at {location}") from exc
        if parsed.hostname and parsed.hostname.rstrip(".").casefold() == "chatgpt.com":
            raise ImportError(f"ChatGPT conversation/share URLs cannot be persisted at {location}")
        if EMAIL_PATTERN.search(decoded):
            raise ImportError(f"contact information is not allowed in URL at {location}")
        query = urllib.parse.unquote_plus(parsed.query)
        if EMAIL_PATTERN.search(query) or any(pattern.search(query) for pattern in PHONE_PATTERNS):
            raise ImportError(f"contact information is not allowed in URL query at {location}")
        if any(
            key.casefold() in SENSITIVE_QUERY_KEYS
            for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        ):
            raise ImportError(f"credential-like query parameters are not allowed at {location}")
        return
    # Stable upstream IDs may legitimately be UUID-shaped, but prose may not
    # retain conversation IDs or personal contact details.
    if field not in {"external_id", "program_external_id", "article_external_id"}:
        if EMAIL_PATTERN.search(value) or any(pattern.search(value) for pattern in PHONE_PATTERNS):
            raise ImportError(f"contact information is not allowed at {location}")
        if UUID_PATTERN.search(value):
            raise ImportError(f"UUID-like conversation identifiers are not allowed at {location}")


def _pseudonymize_uuid_identifiers(payload: dict[str, Any], source_id: str) -> None:
    """Keep stable identity without retaining an upstream UUID verbatim."""
    groups = (
        (payload.get("programs"), ("external_id",)),
        (payload.get("jobs"), ("external_id", "program_external_id")),
        (payload.get("articles"), ("article_external_id",)),
    )
    for items, fields in groups:
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in fields:
                raw = item.get(field)
                if isinstance(raw, str) and UUID_PATTERN.search(raw):
                    item[field] = stable_digest(source_id, raw, prefix=field, length=32)

def _validated_payload(candidate: Any, source_id: str) -> dict[str, Any]:
    if not isinstance(candidate, dict) or candidate.get("version") != "FROSTFIRE_SYNC_V1":
        raise ImportError("input does not contain a FROSTFIRE_SYNC_V1 object")
    source_id = _validate_logical_source_id(source_id)
    # A JSON round trip provides a bounded deep copy so UUID pseudonymization
    # cannot mutate the caller's source object.
    try:
        controlled = json.loads(json.dumps(candidate, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ImportError("source payload must contain JSON-compatible values") from exc
    # The local mapping is authoritative.  A conversation or article cannot
    # redirect data into another logical source or smuggle its private title.
    controlled["source_id"] = source_id
    controlled.pop("source_name", None)
    controlled.pop("batch_id", None)
    _pseudonymize_uuid_identifiers(controlled, source_id)
    _validate_no_sensitive_material(controlled)
    try:
        parsed = FrostFireSyncV1.model_validate(controlled)
    except ValidationError as exc:
        # Pydantic's default string includes rejected input values.  Only emit
        # field paths and safe messages so conversation/article text is never
        # copied into automation logs.
        details = []
        for error in exc.errors(include_input=False, include_url=False)[:12]:
            location = _safe_location(error.get("loc", ()))
            details.append(f"{location}: {error.get('msg', 'invalid value')}")
        summary = "; ".join(details) or "invalid payload"
        raise ImportError(f"FROSTFIRE_SYNC_V1 validation failed: {summary}") from exc
    normalized = parsed.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    normalized["batch_id"] = "import-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return normalized


def payload_from_chatgpt_share(url: str, source_id: str, *, timeout: float) -> dict[str, Any]:
    source_id = _validate_logical_source_id(source_id)
    safe_url = _safe_chatgpt_share_url(url)
    page = fetch_watch_page(safe_url, (), timeout_seconds=timeout)
    for candidate in _json_candidates(page.raw_text + "\n" + page.text):
        if candidate.get("version") == "FROSTFIRE_SYNC_V1":
            return _validated_payload(candidate, source_id)
    raise ImportError(
        "the public share snapshot has no complete FROSTFIRE_SYNC_V1 JSON object; update the shared snapshot or import the JSON file directly"
    )


def _read_local_json(path: str) -> Any:
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        else:
            raw = Path(path).read_bytes()
    except OSError as exc:
        raise ImportError("structured input file could not be read") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ImportError(f"structured JSON exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ImportError("structured input is not valid UTF-8 JSON") from exc


def payload_from_article(
    url: str,
    source_id: str,
    *,
    title: str,
    publisher: str,
    published_at: str | None,
    timeout: float,
) -> dict[str, Any]:
    source_id = _validate_logical_source_id(source_id)
    page = fetch_watch_page(url, (), timeout_seconds=timeout)
    signal = f"{title} {page.text}".casefold()
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", signal)
    article = {
        "article_external_id": stable_digest(page.final_url, page.fingerprint, prefix="article"),
        "publisher": _redact_untrusted_text(publisher, limit=160),
        "article_title": _redact_untrusted_text(title, limit=300),
        "article_url": page.final_url,
        "raw_excerpt": _redact_untrusted_text(page.text, limit=1_500),
        "is_recruitment": any(marker.casefold() in signal for marker in CAMPUS_MARKERS),
        "recruitment_year": int(year_match.group(1)) if year_match else None,
        "classification": "recruitment_signal",
    }
    if published_at:
        article["publish_time"] = published_at
    return _validated_payload(
        {"version": "FROSTFIRE_SYNC_V1", "source_id": source_id, "articles": [article]},
        source_id,
    )


def payload_from_feed(url: str, source_id: str, *, publisher: str, timeout: float) -> dict[str, Any]:
    # Pure validation/structured imports are also used by the offline history
    # and browser bridges. Do not load the application's database/OpenAI
    # configuration unless this explicitly requested feed operation needs it.
    from backend.future_radar.adapters import PublicFeedAdapter

    source_id = _validate_logical_source_id(source_id)
    parsed = urllib.parse.urlsplit(validate_public_https_url(url, resolve_dns=True))
    result = PublicFeedAdapter().scan({
        "id": source_id,
        "name": _redact_untrusted_text(publisher or source_id, limit=160),
        "account_name": _redact_untrusted_text(publisher, limit=160),
        "url": url,
        "domain": parsed.hostname,
        "adapter_config": {"adapter": "public_feed", "timeout_seconds": timeout, "max_entries": 10},
    })
    return _validated_payload(
        {
            "version": "FROSTFIRE_SYNC_V1",
            "source_id": source_id,
            "snapshot_complete": False,
            "articles": result.articles[:10],
        },
        source_id,
    )


def read_keychain_token() -> str:
    if platform.system() != "Darwin":
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def load_token() -> str:
    return os.getenv(TOKEN_ENV, "").strip() or read_keychain_token()


def submit_payload(payload: dict[str, Any], token: str, timeout: float) -> Any:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Frostfire-Controlled-Source-Import/1.0",
            "X-Recruitment-Token": token,
            "Idempotency-Key": str(payload["batch_id"]),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ImportError("server response is unexpectedly large")
    except urllib.error.HTTPError as exc:
        raise ImportError(f"server returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ImportError("submission endpoint is temporarily unavailable") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ImportError("server did not return valid JSON") from exc


def _safe_response(value: Any) -> Any:
    """Defensively redact an unexpected server response before printing it."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            folded = key.casefold()
            if folded == "token" or any(marker in folded for marker in SECRET_KEY_MARKERS):
                continue
            result[key] = _safe_response(item)
        return result
    if isinstance(value, list):
        return [_safe_response(item) for item in value]
    if isinstance(value, str):
        return _redact_untrusted_text(value, limit=10_000)
    return value


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.chatgpt_share:
        return payload_from_chatgpt_share(args.chatgpt_share, args.source_id, timeout=args.timeout)
    if args.structured_json:
        return _validated_payload(_read_local_json(args.structured_json), args.source_id)
    if args.public_article:
        return payload_from_article(
            args.public_article,
            args.source_id,
            title=args.title,
            publisher=args.publisher,
            published_at=args.published_at,
            timeout=args.timeout,
        )
    return payload_from_feed(
        args.public_feed, args.source_id, publisher=args.publisher, timeout=args.timeout
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = build_payload(args)
        if args.submit:
            token = load_token()
            if not token:
                raise ImportError(
                    f"set {TOKEN_ENV} or add macOS Keychain service '{KEYCHAIN_SERVICE}'"
                )
            output = _safe_response(submit_payload(payload, token, args.timeout))
        else:
            output = payload
    except (ImportError, WatchFetchError, OSError, urllib.error.URLError) as exc:
        print(f"source import error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
