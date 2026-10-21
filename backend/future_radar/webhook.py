"""Domain-separated signing for the existing controlled monitor writer.

An explicit webhook secret takes precedence. Otherwise the already-authorized
REST ingest token derives a signing-only child key; the root token is never
sent in a webhook request. This grants no additional writer permissions.
"""
import hashlib
import hmac
import time

DOMAIN = b"frostfire/future-radar/webhook/v1"


def signing_key(*, explicit: str = "", ingest_token: str = "") -> str:
    if explicit:
        return explicit if len(explicit.encode()) >= 32 else ""
    if len(ingest_token.encode()) < 32:
        return ""
    return hmac.new(ingest_token.encode(), DOMAIN, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, key: str, *, event_id: str, timestamp: int | None = None) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(key.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
            "X-Future-Radar-Timestamp": stamp, "X-Future-Radar-Signature": f"sha256={digest}",
            "Idempotency-Key": event_id}
