"""Bounded, user-scoped scoring cache with transactional database revisions.

Only sanitized opportunity results belong in this cache. Profile contents and
credentials are never retained: callers pass an opaque principal/profile/rules
digest. Each process owns its LRU; a small database revision read validates
every hit, including writes performed by another worker or a direct SQL client.

The fixed trigger definitions are exported for the offline SQLite migration
auditor. Unknown triggers must not be accepted just because their name matches.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Hashable


REVISION_KEY = "future_radar.opportunities.revision.v1"
NAMESPACE_KEY = "future_radar.opportunities.namespace.v1"
POSTGRES_REVISION_FUNCTION = "_ff_radar_opportunity_revision_v1"
CACHE_FORMAT_VERSION = "public-opportunity-score-cache-v3-source-ratings"

# These are precisely the inputs selected/filterable by _opportunity_rows.
# In particular, source scheduling/leases/errors, raw evidence, and model cache
# writes are not opportunity changes. NULL-to-value updates count as changes.
REVISION_COLUMNS: dict[str, tuple[str, ...]] = {
    "radar_jobs": (
        "id", "external_id", "program_id", "company", "title", "city", "region",
        "employer_type", "industry", "primary_category", "organization_category",
        "industry_tags", "role_tags", "official_url", "application_url",
        "opening_date", "closing_date", "status", "verification_status",
        "confidence_score", "description", "responsibilities", "requirements",
        "tags", "source_ratings", "first_seen_at", "last_seen_at", "last_changed_at",
    ),
    "recruitment_programs": (
        "id", "program_name", "recruitment_year", "recruitment_type", "status",
        "opening_date", "closing_date",
    ),
    "job_sources": (
        "job_id", "source_id", "source_url", "source_type", "verification_role",
        "active", "discovered_at", "last_seen_at",
    ),
    "monitor_sources": ("id", "name", "source_type", "trust_level"),
    "radar_events": ("id", "entity_type", "entity_id", "event_type", "detected_at"),
}


def _sqlite_trigger_definitions(columns_by_table=None) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for table, columns in (columns_by_table or REVISION_COLUMNS).items():
        for operation in ("insert", "update", "delete"):
            name = f"ff_radar_cache_v1_{table}_{operation}"
            event = operation.upper()
            condition = ""
            if operation == "update":
                event += " OF " + ", ".join(f'"{column}"' for column in columns)
                condition = " WHEN " + " OR ".join(
                    f'OLD."{column}" IS NOT NEW."{column}"' for column in columns
                )
            statement = (
                f'CREATE TRIGGER "{name}" AFTER {event} ON "{table}"{condition} '
                "BEGIN UPDATE system_state "
                "SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT), "
                "updated_at=CURRENT_TIMESTAMP "
                f"WHERE key='{REVISION_KEY}'; END"
            )
            result[name] = (table, statement)
    return result


SQLITE_REVISION_TRIGGERS = _sqlite_trigger_definitions()
PRE_RATING_SQLITE_REVISION_TRIGGERS = _sqlite_trigger_definitions({
    table: tuple(column for column in columns if column != "source_ratings")
    for table, columns in REVISION_COLUMNS.items()
})


def is_known_sqlite_revision_trigger(name: str, table: str, sql: str | None) -> bool:
    """Exact-definition allowlist, not a permissive prefix/name exception."""
    expected = SQLITE_REVISION_TRIGGERS.get(name)
    if expected is None or table != expected[0] or not isinstance(sql, str):
        return False
    # SQLite preserves the supplied SQL except optional IF NOT EXISTS and the
    # trailing semicolon. Do not lowercase quoted identifiers or string values.
    normalized = " ".join(sql.strip().removesuffix(";").split())
    normalized = normalized.replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)
    return normalized in {
        " ".join(expected[1].split()),
        " ".join(PRE_RATING_SQLITE_REVISION_TRIGGERS[name][1].split()),
    }


def postgres_revision_definitions(schema: str) -> tuple[str, dict[str, str]]:
    """Return fixed, schema-qualified DDL; no SQL is taken from source rows."""
    if not schema or len(schema) > 63 or not schema.replace("_", "a").isalnum():
        raise ValueError("Invalid private application schema")
    if not (schema[0].isalpha() or schema[0] == "_"):
        raise ValueError("Invalid private application schema")
    quoted_schema = '"' + schema + '"'
    function = f'{quoted_schema}."{POSTGRES_REVISION_FUNCTION}"'
    function_sql = (
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger "
        "LANGUAGE plpgsql SET search_path=pg_catalog AS $ff_radar_revision$ "
        "BEGIN "
        f"UPDATE {quoted_schema}.system_state "
        "SET value=(value::bigint+1)::text, updated_at=CURRENT_TIMESTAMP::text "
        f"WHERE key='{REVISION_KEY}'; "
        "RETURN NULL; END; $ff_radar_revision$"
    )
    triggers: dict[str, str] = {}
    for table, columns in REVISION_COLUMNS.items():
        for operation in ("insert", "update", "delete", "truncate"):
            name = f"ff_radar_cache_v1_{table}_{operation}"
            event = operation.upper()
            condition = ""
            row_kind = "STATEMENT" if operation == "truncate" else "ROW"
            if operation == "update":
                event += " OF " + ", ".join(f'"{column}"' for column in columns)
                condition = " WHEN (" + " OR ".join(
                    f'OLD."{column}" IS DISTINCT FROM NEW."{column}"' for column in columns
                ) + ")"
            triggers[name] = (
                f'CREATE OR REPLACE TRIGGER "{name}" AFTER {event} '
                f'ON {quoted_schema}."{table}" FOR EACH {row_kind}{condition} '
                f"EXECUTE FUNCTION {function}()"
            )
    return function_sql, triggers


def install_opportunity_revision(connection: Any) -> None:
    """Install cache metadata within the caller's existing init transaction."""
    sqlite = isinstance(connection, sqlite3.Connection)
    if not sqlite:
        # Coordinate two workers initializing the same private schema. This is
        # a startup-only DDL lock, never a request/scoring or source-run lock.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(current_schema()), 17028113)"
        )
    now = datetime.now(timezone.utc).isoformat()
    for key, value in ((REVISION_KEY, "0"), (NAMESPACE_KEY, uuid.uuid4().hex)):
        connection.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value, now),
        )
    if sqlite:
        installed = {
            row["name"]: row for row in connection.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        for name, (table, sql) in SQLITE_REVISION_TRIGGERS.items():
            existing = installed.get(name)
            if existing is not None:
                if not is_known_sqlite_revision_trigger(name, table, existing["sql"]):
                    raise sqlite3.OperationalError("Unexpected Radar cache trigger definition")
                normalized = " ".join(existing["sql"].strip().removesuffix(";").split())
                normalized = normalized.replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)
                if normalized == " ".join(sql.split()):
                    continue
                # Upgrade the exact recognized pre-rating trigger only.
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(sql)
        return
    schema = str(connection.schema)
    function, triggers = postgres_revision_definitions(schema)
    connection.execute(function)
    connection.execute(
        f'REVOKE ALL ON FUNCTION "{schema}"."{POSTGRES_REVISION_FUNCTION}"() FROM PUBLIC'
    )
    for sql in triggers.values():
        connection.execute(sql)


def _database_namespace(connection: Any) -> str | None:
    if isinstance(connection, sqlite3.Connection):
        rows = connection.execute("PRAGMA database_list").fetchall()
        filename = next((str(row[2]) for row in rows if row[1] == "main"), "")
        if not filename:
            # Distinct in-memory connections must never share a data snapshot.
            material: Any = ("sqlite-memory", id(connection))
        else:
            stat = os.stat(filename)
            material = ("sqlite", os.path.realpath(filename), stat.st_dev, stat.st_ino)
    else:
        info = getattr(getattr(connection, "_raw", None), "info", None)
        if info is None:
            # Unknown connection wrappers are safe to use without caching.
            return None
        material = ("postgres", info.host, info.port, info.dbname, connection.schema)
    return opaque_digest(material)


def read_opportunity_revision(connection: Any) -> tuple[str, str, int] | None:
    namespace = _database_namespace(connection)
    if namespace is None:
        return None
    rows = connection.execute(
        "SELECT key, value FROM system_state WHERE key IN (?, ?)",
        (NAMESPACE_KEY, REVISION_KEY),
    ).fetchall()
    values = {str(row["key"]): str(row["value"]) for row in rows}
    try:
        revision = int(values[REVISION_KEY])
        epoch = values[NAMESPACE_KEY]
        if revision < 0 or not epoch:
            return None
    except (KeyError, ValueError):
        return None
    return namespace, epoch, revision


def opaque_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def scoring_scope(user_id: int, profile: dict[str, Any], rules: Any) -> str:
    """Only the digest survives: no profile text or account credential cache."""
    return opaque_digest((CACHE_FORMAT_VERSION, str(user_id), profile, rules))


def date_boundary() -> tuple[str, str]:
    # score_job uses local date, SQL DATE('now') is UTC on SQLite. Both midnight
    # boundaries matter even if no table is modified on a deadline/opening day.
    return date.today().isoformat(), datetime.now(timezone.utc).date().isoformat()


# Observation clocks and event previews do not participate in the scorer.
# Keep source identity/trust/active state in the digest, and refresh the full
# sanitized provenance lists on every read so their clocks never become stale.
OPPORTUNITY_FRESHNESS_FIELDS = (
    "last_seen_at", "last_changed_at", "latest_event_type", "latest_event_at",
    "sources", "discovered_by", "verified_by",
)
_SOURCE_COLLECTION_FIELDS = frozenset({"sources", "discovered_by", "verified_by"})


def opportunity_scoring_input(public_item: dict[str, Any]) -> dict[str, Any]:
    """Hash-only scoring inputs; callers must supply their public sanitizer."""
    stable = {
        key: value for key, value in public_item.items()
        if key not in OPPORTUNITY_FRESHNESS_FIELDS
    }
    for field in _SOURCE_COLLECTION_FIELDS:
        if field in public_item:
            stable[field] = [
                {key: value for key, value in source.items() if key != "last_seen_at"}
                for source in public_item[field]
            ]
    return stable


def _retained_size(value: Any) -> int:
    """Count retained Python objects once, including nested texts and indexes."""
    seen: set[int] = set()

    def visit(item: Any) -> int:
        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)
        size = sys.getsizeof(item)
        if isinstance(item, dict):
            size += sum(visit(key) + visit(value) for key, value in item.items())
        elif isinstance(item, (list, tuple, set, frozenset)):
            size += sum(visit(value) for value in item)
        elif is_dataclass(item) and not isinstance(item, type):
            size += sum(visit(getattr(item, field.name)) for field in fields(item))
        return size

    return visit(value)


class RevisionChanged(RuntimeError):
    """A relevant writer committed while an uncached pool was being prepared."""


class BoundedScoringCache:
    """LRU + per-key single-flight, never a global user/request execution lock."""

    def __init__(self, *, max_entries: int = 16, max_bytes: int = 32 * 1024 * 1024,
                 max_inflight: int = 8, ttl_seconds: float = 300,
                 refresh_on_hit: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_inflight = max_inflight
        self.ttl_seconds = ttl_seconds
        self.refresh_on_hit = refresh_on_hit
        self._clock = clock
        self._guard = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, int, Any]] = OrderedDict()
        self._pending: dict[Hashable, Future[Any]] = {}
        self._bytes = 0

    def _touch(self, key: Hashable) -> Any:
        """Caller holds the guard; revision validation is still done upstream."""
        _, size, value = self._entries[key]
        if self.refresh_on_hit:
            self._entries[key] = (self._clock() + self.ttl_seconds, size, value)
        self._entries.move_to_end(key)
        return value

    def _expire(self) -> None:
        now = self._clock()
        if self.refresh_on_hit:
            # Refresh-on-hit keeps LRU order in expiration order. Removing
            # only the expired prefix avoids an O(n) sweep for each of the
            # thousands of individual records in a pool rebuild.
            while self._entries:
                key, (expires, size, _) = next(iter(self._entries.items()))
                if expires > now:
                    break
                del self._entries[key]
                self._bytes -= size
            return
        for key, (expires, size, _) in list(self._entries.items()):
            if expires <= now:
                del self._entries[key]
                self._bytes -= size

    def get_or_compute(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        with self._guard:
            self._expire()
            existing = self._entries.get(key)
            if existing is not None:
                return self._touch(key)
            pending = self._pending.get(key)
            owner = pending is None
            if owner and len(self._pending) < self.max_inflight:
                pending = Future()
                self._pending[key] = pending
        if not owner:
            return pending.result()
        if pending is None:
            # Keep the pending registry bounded too. Unrelated saturated keys
            # can still complete uncached, without blocking all other users.
            return factory()
        try:
            value = factory()
            size = _retained_size(value) + _retained_size(key)
            with self._guard:
                self._expire()
                if self.max_entries > 0 and size <= self.max_bytes:
                    while self._entries and (
                        len(self._entries) >= self.max_entries
                        or self._bytes + size > self.max_bytes
                    ):
                        _, (_, removed, _) = self._entries.popitem(last=False)
                        self._bytes -= removed
                    self._entries[key] = (self._clock() + self.ttl_seconds, size, value)
                    self._bytes += size
                self._pending.pop(key, None)
            pending.set_result(value)
            return value
        except BaseException as error:
            with self._guard:
                self._pending.pop(key, None)
            pending.set_exception(error)
            raise

    def find(self, predicate: Callable[[Hashable, Any], Any]) -> Any:
        """Inspect at most the bounded entry count for a cached detail alias."""
        with self._guard:
            self._expire()
            for key, (_, _, value) in reversed(self._entries.items()):
                result = predicate(key, value)
                if result is not None:
                    self._touch(key)
                    return result
        return None

    def info(self) -> dict[str, int]:
        with self._guard:
            self._expire()
            return {"entries": len(self._entries), "bytes": self._bytes,
                    "inflight": len(self._pending)}
