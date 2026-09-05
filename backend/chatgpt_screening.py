"""User-approved source screening, separate from official employer attestation."""

from datetime import date
from urllib.parse import urlsplit

from .chatgpt_sources import KNOWN_CHATGPT_SOURCE_IDS
from .recruitment_watch import WatchFetchError, normalize_public_https_urls


SOURCE_SCREENED = "source_screened"


def chatgpt_screening_eligible(candidate: dict, *, today: date | None = None) -> bool:
    """Local structural checks only: never fetch a page or invent verification.

    The source ID comes from the authenticated bridge envelope. Merely putting
    "ChatGPT" in a source label, tags or evidence does not confer this policy.
    Established rejections/closures remain authoritative until a genuine newer
    source observation changes the candidate through the existing CAS path.
    """
    if candidate.get("source_id") not in KNOWN_CHATGPT_SOURCE_IDS:
        return False
    if candidate.get("verification_status") in {"closed", "rejected", "conflicted"}:
        return False
    if candidate.get("incoming_status", candidate.get("status")) == "closed":
        return False
    if not str(candidate.get("company") or "").strip() or not str(candidate.get("title") or "").strip():
        return False
    for field in ("closing_date", "verified_closing_date"):
        value = candidate.get(field)
        if value:
            try:
                if date.fromisoformat(str(value)) <= (today or date.today()):
                    return False
            except ValueError:
                return False
    try:
        url, _ = normalize_public_https_urls(
            str(candidate.get("canonical_url") or candidate.get("official_url") or ""),
            resolve_dns=False,
        )
    except WatchFetchError:
        return False
    host = (urlsplit(url).hostname or "").lower()
    # A private chat is not a public recruitment destination.
    if host in {"chatgpt.com", "chat.openai.com"} or host.endswith(".chatgpt.com"):
        return False
    # Reuse the public-pool redaction policy (including secret/contact query
    # parameters). Import lazily because the adapter also reads the database.
    from .future_radar.adapters import _public_reference_url
    return _public_reference_url(url) is not None
