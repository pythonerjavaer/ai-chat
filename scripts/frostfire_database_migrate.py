#!/usr/bin/env python3
"""Take a consistent SQLite backup and optionally migrate it to PostgreSQL.

No application/config initialization, AI calls, credentials on argv, or row-value logs.
The default is an entirely local dry run. Source files are opened mode=ro;
PostgreSQL DDL/data are committed together only after full verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit


APPLICATION_TABLES = (
    "users", "sessions", "messages", "documents", "chunks", "spaces",
    "token_usage", "space_runs", "recruitment_profiles", "recruitment_jobs",
    "recruitment_ingest_candidates", "recruitment_ingest_sources",
    "recruitment_ingest_events", "recruitment_watches", "system_state",
    "api_usage_events", "radar_companies", "schema_migrations", "monitor_sources",
    "recruitment_programs", "radar_jobs", "source_articles", "job_sources",
    "program_sources", "radar_events", "radar_runs", "radar_sync_batches",
    "radar_locks", "radar_ai_cache", "radar_source_snapshots",
)
TABLE_SET = frozenset(APPLICATION_TABLES)
EXPECTED_FOREIGN_KEYS = 29
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,100}$")
INTEGER_LITERAL = re.compile(r"^[+-]?\d+$")
REAL_LITERAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
SQLITE_TYPES = {"INTEGER": "BIGINT", "TEXT": "TEXT", "REAL": "DOUBLE PRECISION"}
PG_TYPES = {"INTEGER": "bigint", "TEXT": "text", "REAL": "double precision"}
FK_ACTIONS = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
PG_FK_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
ROLE_CHECK = re.compile(
    r"\bCHECK\s*\(\s*role\s+IN\s*\(\s*'user'\s*,\s*'assistant'\s*\)\s*\)",
    re.IGNORECASE,
)
FORBIDDEN_SCHEMAS = {
    "public", "auth", "storage", "realtime", "extensions", "information_schema",
    "graphql", "graphql_public", "supabase_functions", "supabase_migrations",
}


class MigrationError(RuntimeError):
    """Only constant reason codes and allowlisted table names belong here."""


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    not_null: bool
    default: str | None
    pk_order: int


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    target: str
    target_columns: tuple[str, ...]
    on_delete: str
    on_update: str


@dataclass(frozen=True)
class Index:
    name: str
    unique: bool
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...]
    indexes: tuple[Index, ...]
    identity: str | None
    high_water: int
    role_check: bool


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise MigrationError("unsupported_schema_identifier")
    return '"' + value + '"'


def schema_name(value: str) -> str:
    identifier(value)
    if value in FORBIDDEN_SCHEMAS or value.startswith("pg_"):
        raise MigrationError("target_schema_must_be_private")
    return value


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def literal_default(value: str | None) -> str | None:
    """Rebuild literals, never pass expressions from sqlite_master to PostgreSQL."""
    if value is None:
        return None
    value = value.strip()
    if value.upper() == "NULL":
        return "NULL"
    if INTEGER_LITERAL.fullmatch(value):
        number = int(value)
        if -(2**63) <= number < 2**63:
            return str(number)
        raise MigrationError("unsupported_column_default")
    if REAL_LITERAL.fullmatch(value) and math.isfinite(float(value)):
        return value
    if re.fullmatch(r"'(?:[^']|'')*'", value, re.DOTALL) and "\x00" not in value:
        return value
    raise MigrationError("unsupported_column_default")


def check_sqlite(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchall()
    if len(result) != 1 or result[0][0] != "ok":
        raise MigrationError("sqlite_integrity_check_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("sqlite_foreign_key_check_failed")


def read_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def private_directory(path: Path | None) -> Path:
    if path is None:
        return Path(tempfile.mkdtemp(prefix="frostfire-database-backup-"))
    path = path.absolute()
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_mode & 0o077:
        raise MigrationError("backup_directory_must_be_private_0700")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise MigrationError("backup_directory_must_be_owned_by_current_user")
    return path


def exclusive_file(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)


def backup_sqlite(source: Path, directory: Path | None = None, timeout: float = 120) -> Path:
    """Hold a source read transaction, check it, then include committed WAL pages."""
    if not stat.S_ISREG(source.lstat().st_mode):
        raise MigrationError("source_must_be_a_regular_sqlite_file")
    destination_dir = private_directory(directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"sqlite-snapshot-{stamp}-{uuid.uuid4().hex[:10]}.sqlite3"
    deadline = time.monotonic() + timeout

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise MigrationError("sqlite_backup_timeout")

    original = read_sqlite(source)
    try:
        original.execute("BEGIN")
        check_sqlite(original)
        fd = exclusive_file(destination)
        os.close(fd)
        copied = sqlite3.connect(destination)
        try:
            original.backup(copied, pages=256, progress=progress, sleep=0.05)
            copied.execute("PRAGMA journal_mode = DELETE")
            check_sqlite(copied)
        finally:
            copied.close()
    finally:
        original.close()
    with destination.open("rb") as copied_file:
        os.fsync(copied_file.fileno())
    return destination


def _index_parts(connection: sqlite3.Connection, table: str, row: sqlite3.Row, columns: set[str]) -> tuple[str, ...]:
    parts: list[str] = []
    details = connection.execute(f"PRAGMA index_xinfo({identifier(row['name'])})").fetchall()
    for part in details:
        if not part["key"]:
            continue
        if part["cid"] == -2:
            # The only expression index in the audited application schema.
            if row["name"] != "idx_recruitment_ingest_sources_identity" or table != "recruitment_ingest_sources":
                raise MigrationError("unsupported_expression_index")
            sql_row = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (row["name"],)).fetchone()
            expected = re.compile(
                r"\s*CREATE\s+UNIQUE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?idx_recruitment_ingest_sources_identity"
                r"\s+ON\s+recruitment_ingest_sources\s*\(\s*source_id\s*,\s*COALESCE\s*\(\s*source_thread_id\s*,\s*''\s*\)\s*\)\s*;?\s*",
                re.IGNORECASE,
            )
            if not sql_row or not expected.fullmatch(sql_row["sql"] or ""):
                raise MigrationError("unsupported_expression_index")
            expression = 'COALESCE("source_thread_id", \'\')'
        elif part["cid"] >= 0 and part["name"] in columns:
            expression = identifier(part["name"])
            if part["coll"] == "NOCASE":
                if table != "users" or part["name"] != "username":
                    raise MigrationError("unsupported_index_collation")
                # SQLite NOCASE folds ASCII only, not all Unicode as lower()/citext do.
                expression = f"translate({expression}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') COLLATE \"C\""
            elif part["coll"] != "BINARY":
                raise MigrationError("unsupported_index_collation")
        else:
            raise MigrationError("unsupported_index_column")
        parts.append(expression + (" DESC" if part["desc"] else ""))
    if not parts:
        raise MigrationError("unsupported_empty_index")
    return tuple(parts)


def inspect_schema(connection: sqlite3.Connection) -> tuple[Table, ...]:
    objects = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    ordinary = {row["name"] for row in objects if row["type"] == "table" and not row["name"].startswith("sqlite_")}
    if ordinary != TABLE_SET:
        raise MigrationError("source_must_contain_exactly_the_30_audited_application_tables")
    for row in objects:
        if row["type"] == "view" or (
            row["type"] == "trigger" and not _revision_contract().is_known_sqlite_revision_trigger(
                row["name"], row["tbl_name"], row["sql"],
            )
        ):
            raise MigrationError("source_has_unsupported_views_or_triggers")
    if any(row["type"] == "index" and row["tbl_name"] not in TABLE_SET for row in objects):
        raise MigrationError("source_has_unsupported_index_owner")
    by_name = {row["name"]: row for row in objects if row["type"] == "table"}
    sequence_rows = connection.execute("SELECT name, seq FROM sqlite_sequence").fetchall() if "sqlite_sequence" in by_name else []
    sequences = {row["name"]: row["seq"] for row in sequence_rows if row["name"] in TABLE_SET}
    tables: list[Table] = []
    for name in APPLICATION_TABLES:
        raw_sql = by_name[name]["sql"] or ""
        if not re.match(r"\s*CREATE\s+TABLE\b", raw_sql, re.IGNORECASE):
            raise MigrationError("unsupported_table_definition")
        role_check = bool(ROLE_CHECK.search(raw_sql))
        if (role_check and name != "messages") or re.search(r"\bCHECK\s*\(", ROLE_CHECK.sub("", raw_sql), re.IGNORECASE):
            raise MigrationError("unsupported_check_constraint")
        if name == "messages" and not role_check:
            raise MigrationError("missing_messages_role_constraint")
        columns: list[Column] = []
        for row in connection.execute(f"PRAGMA table_xinfo({identifier(name)})"):
            identifier(row["name"])
            if row["type"].upper() not in SQLITE_TYPES or row["hidden"]:
                raise MigrationError("unsupported_column_type_or_generated_column")
            columns.append(Column(row["name"], row["type"].upper(), bool(row["notnull"]), literal_default(row["dflt_value"]), int(row["pk"])))
        names = {column.name for column in columns}
        primary = [column for column in columns if column.pk_order]
        if not primary:
            raise MigrationError("missing_application_primary_key")
        identity = None
        if re.search(r"\bAUTOINCREMENT\b", raw_sql, re.IGNORECASE):
            if len(primary) != 1 or primary[0].kind != "INTEGER":
                raise MigrationError("unsupported_identity_column")
            identity = primary[0].name
        high_water = int(sequences.get(name, 0)) if identity else 0
        if identity:
            high_water = max(high_water, connection.execute(f"SELECT COALESCE(MAX({identifier(identity)}), 0) FROM {identifier(name)}").fetchone()[0])
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({identifier(name)})"):
            grouped.setdefault(row["id"], []).append(row)
        foreign_keys: list[ForeignKey] = []
        for rows in grouped.values():
            rows.sort(key=lambda item: item["seq"])
            first = rows[0]
            if first["table"] not in TABLE_SET or first["on_delete"] not in FK_ACTIONS or first["on_update"] not in FK_ACTIONS or first["match"] != "NONE":
                raise MigrationError("unsupported_foreign_key")
            if any(row["from"] not in names for row in rows):
                raise MigrationError("unsupported_foreign_key_column")
            foreign_keys.append(ForeignKey(tuple(row["from"] for row in rows), first["table"], tuple(row["to"] for row in rows), first["on_delete"], first["on_update"]))
        indexes: list[Index] = []
        for row in connection.execute(f"PRAGMA index_list({identifier(name)})"):
            if row["origin"] == "pk":
                continue
            if row["partial"]:
                raise MigrationError("unsupported_partial_index")
            parts = _index_parts(connection, name, row, names)
            index_name = row["name"]
            if name == "users" and row["origin"] == "u" and len(parts) == 1 and 'translate("username",' in parts[0]:
                # Use the same name as backend.storage, so app init does not
                # create a redundant index after a successful migration.
                index_name = "users_username_nocase_key"
            elif index_name.startswith("sqlite_autoindex_"):
                index_name = f"ffm_{name}_{hashlib.sha256(index_name.encode()).hexdigest()[:10]}"
            identifier(index_name)
            indexes.append(Index(index_name, bool(row["unique"]), parts))
        tables.append(Table(name, tuple(columns), tuple(foreign_keys), tuple(sorted(indexes, key=lambda item: item.name)), identity, high_water, role_check))
    by_table = {table.name: {col.name for col in table.columns} for table in tables}
    for table in tables:
        for fk in table.foreign_keys:
            if any(column not in by_table[fk.target] for column in fk.target_columns):
                raise MigrationError("foreign_key_target_column_missing")
    if sum(len(table.foreign_keys) for table in tables) != EXPECTED_FOREIGN_KEYS:
        raise MigrationError("source_foreign_key_count_differs_from_audited_29")
    return tuple(tables)


def create_table_sql(table: Table) -> str:
    declarations: list[str] = []
    primary = sorted((col for col in table.columns if col.pk_order), key=lambda col: col.pk_order)
    for column in table.columns:
        declaration = f"{identifier(column.name)} {SQLITE_TYPES[column.kind]}"
        if column.name == table.identity:
            declaration += " GENERATED BY DEFAULT AS IDENTITY"
        if column.not_null or column.pk_order:
            declaration += " NOT NULL"
        if column.default is not None:
            declaration += " DEFAULT " + column.default
        declarations.append(declaration)
    declarations.append("PRIMARY KEY (" + ", ".join(identifier(col.name) for col in primary) + ")")
    if table.role_check:
        declarations.append("CONSTRAINT ffm_messages_role CHECK (role IN ('user', 'assistant'))")
    for fk in table.foreign_keys:
        declarations.append(
            "FOREIGN KEY (" + ", ".join(identifier(value) for value in fk.columns) + ") REFERENCES "
            + identifier(fk.target) + " (" + ", ".join(identifier(value) for value in fk.target_columns) + ")"
            + f" ON DELETE {fk.on_delete} ON UPDATE {fk.on_update} DEFERRABLE INITIALLY IMMEDIATE"
        )
    return f"CREATE TABLE {identifier(table.name)} (" + ",\n".join(declarations) + ")"


def create_index_sql(table: Table, index: Index) -> str:
    return f"CREATE {'UNIQUE ' if index.unique else ''}INDEX {identifier(index.name)} ON {identifier(table.name)} (" + ", ".join(index.parts) + ")"


def schema_digest(tables: Sequence[Table]) -> str:
    return hashlib.sha256(json_bytes([
        {"table": table.name, "create": create_table_sql(table), "indexes": [create_index_sql(table, index) for index in table.indexes]}
        for table in tables
    ])).hexdigest()


def typed_value(value: Any, column: Column) -> Any:
    if value is None:
        if column.not_null or column.pk_order:
            raise MigrationError("null_primary_key_or_required_value_cannot_be_preserved")
        return ["null"]
    if column.kind == "TEXT" and isinstance(value, str) and "\x00" not in value:
        return ["text", value]
    if column.kind == "INTEGER" and type(value) is int and -(2**63) <= value < 2**63:
        return ["integer", str(value)]
    if column.kind == "REAL" and type(value) in (int, float) and math.isfinite(float(value)):
        return ["real", float(value).hex()]
    raise MigrationError("unsupported_or_lossy_row_value")


def row_batches(cursor: Any, size: int = 500) -> Iterable[list[Any]]:
    if hasattr(cursor, "fetchmany"):
        while batch := cursor.fetchmany(size):
            yield batch
    else:
        rows = cursor.fetchall()
        for start in range(0, len(rows), size):
            yield rows[start:start + size]


def table_digest(connection: Any, table: Table) -> dict[str, Any]:
    columns = ", ".join(identifier(column.name) for column in table.columns)
    row_hashes: list[bytes] = []
    for rows in row_batches(connection.execute(f"SELECT {columns} FROM {identifier(table.name)}")):
        for row in rows:
            canonical = [typed_value(row[index], column) for index, column in enumerate(table.columns)]
            row_hashes.append(hashlib.sha256(json_bytes(canonical)).digest())
    # Sort binary row digests, not database collation-dependent text ordering.
    row_hashes.sort()
    digest = hashlib.sha256(json_bytes([(column.name, column.kind) for column in table.columns]))
    for row_hash in row_hashes:
        digest.update(row_hash)
    return {"rows": len(row_hashes), "sha256": digest.hexdigest()}


def database_digest(connection: Any, tables: Sequence[Table]) -> dict[str, dict[str, Any]]:
    return {table.name: table_digest(connection, table) for table in tables}


def synthetic_user_count(connection: sqlite3.Connection) -> int:
    # Fail closed for the reserved domains used by our local QA fixtures. This
    # guard is not a claim that arbitrary datasets' provenance can be inferred.
    count = 0
    for row in connection.execute("SELECT username FROM users"):
        username = str(row[0]).strip().lower()
        domain = username.rsplit("@", 1)[-1] if "@" in username else ""
        if domain.endswith((".invalid", ".test")) or username.startswith(("frostfire-offline-qa-", "offline-browser-qa-")):
            count += 1
    return count


def _revision_contract() -> Any:
    # This audited module is stdlib-only and never imports configuration,
    # application startup, a database driver, or an AI client. Source SQL is
    # compared to its fixed DDL, never replayed on the destination.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.future_radar import opportunity_cache

    return opportunity_cache


def _connect_postgres(dsn: str, schema: str) -> Any:
    # Lazy import is important: --dry-run works with only Python's stdlib and
    # cannot load backend.config, a developer .env, an AI client, or a DB pool.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.storage import connect_postgres

    return connect_postgres(dsn, schema=schema, timeout=30.0, max_size=1)


def validate_target_dsn(dsn: str, *, test_source: bool, target_env: str) -> None:
    try:
        parsed = urlsplit(dsn)
        parameters = parse_qs(parsed.query, keep_blank_values=True)
        is_loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError
        # libpq query parameters can override a URL's apparent destination.
        if parameters.keys() & {"host", "hostaddr", "port", "user", "password", "dbname", "service", "passfile", "options"}:
            raise ValueError
    except ValueError:
        raise MigrationError("invalid_or_ambiguous_target_database_url") from None
    if test_source and (not is_loopback or target_env != "FROSTFIRE_TEST_POSTGRES_URL"):
        raise MigrationError("test_sources_require_explicit_loopback_test_database")
    if not is_loopback:
        certificates = parameters.get("sslrootcert", [])
        if parameters.get("sslmode") != ["verify-full"] or len(certificates) != 1:
            raise MigrationError("remote_target_requires_verify_full_and_explicit_ca")
        certificate = Path(certificates[0])
        if not certificate.is_absolute() or not certificate.is_file():
            raise MigrationError("target_ca_file_must_exist_at_an_absolute_path")


def _target_objects(connection: Any, schema: str) -> list[Any]:
    return connection.execute(
        "SELECT c.relname, c.relkind FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=? AND c.relkind IN ('r','p','v','m','f','S')",
        (schema,),
    ).fetchall()


def _compact_expression(value: str) -> str:
    # All supported index expressions contain no meaningful whitespace in a
    # literal. The only casts added by PostgreSQL's deparser here are ::text.
    return re.sub(r"\s+", "", value.replace('"', "").replace("::text", "")).replace("coalesce(", "COALESCE(")


def _canonical_default(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"::(?:text|bigint|double precision|integer)$", "", value.strip())
    if value == "NULL":
        return None
    return literal_default(value)


def verify_target_schema(connection: Any, tables: Sequence[Table], schema: str) -> None:
    """Check columns, keys, checks, and indexes before accepting an idempotent run."""
    exposed = connection.execute(
        "SELECT COUNT(*) AS count FROM pg_catalog.pg_namespace n "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(n.nspacl, pg_catalog.acldefault('n', n.nspowner))) acl "
        "LEFT JOIN pg_catalog.pg_roles r ON r.oid=acl.grantee "
        "WHERE n.nspname=? AND (acl.grantee=0 OR r.rolname IN ('anon','authenticated'))",
        (schema,),
    ).fetchone()["count"]
    if exposed:
        raise MigrationError("target_schema_is_not_private")
    for table in tables:
        columns = connection.execute(
            "SELECT column_name, data_type, is_nullable, column_default, is_identity "
            "FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
            (schema, table.name),
        ).fetchall()
        if len(columns) != len(table.columns):
            raise MigrationError("target_schema_differs:" + table.name)
        for actual, expected in zip(columns, table.columns):
            if actual["column_name"] != expected.name or actual["data_type"] != PG_TYPES[expected.kind]:
                raise MigrationError("target_schema_differs:" + table.name)
            if (actual["is_nullable"] == "NO") != bool(expected.not_null or expected.pk_order):
                raise MigrationError("target_nullability_differs:" + table.name)
            if (actual["is_identity"] == "YES") != (expected.name == table.identity):
                raise MigrationError("target_identity_differs:" + table.name)
            if expected.name != table.identity and _canonical_default(actual["column_default"]) != _canonical_default(expected.default):
                raise MigrationError("target_default_differs:" + table.name)
        constraints = connection.execute(
            "SELECT con.contype, con.condeferrable, con.convalidated, con.confdeltype, con.confupdtype, "
            "ref.relname AS target_table, refns.nspname AS target_schema, "
            "ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY u(num, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=u.num ORDER BY u.ord) AS columns, "
            "ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY u(num, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=u.num ORDER BY u.ord) AS target_columns, "
            "pg_catalog.pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class c ON c.oid=con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "LEFT JOIN pg_catalog.pg_class ref ON ref.oid=con.confrelid "
            "LEFT JOIN pg_catalog.pg_namespace refns ON refns.oid=ref.relnamespace "
            "WHERE n.nspname=? AND c.relname=?",
            (schema, table.name),
        ).fetchall()
        primary = [tuple(row["columns"]) for row in constraints if row["contype"] == "p"]
        expected_primary = tuple(col.name for col in sorted(table.columns, key=lambda col: col.pk_order) if col.pk_order)
        if primary != [expected_primary]:
            raise MigrationError("target_primary_key_differs:" + table.name)
        actual_fks = set()
        for row in constraints:
            if row["contype"] != "f":
                continue
            if not row["condeferrable"] or not row["convalidated"] or row["target_schema"] != schema:
                raise MigrationError("target_foreign_key_not_validated_or_deferrable:" + table.name)
            actual_fks.add((tuple(row["columns"]), row["target_table"], tuple(row["target_columns"]), PG_FK_ACTIONS[row["confdeltype"]], PG_FK_ACTIONS[row["confupdtype"]]))
        expected_fks = {(fk.columns, fk.target, fk.target_columns, fk.on_delete, fk.on_update) for fk in table.foreign_keys}
        if actual_fks != expected_fks:
            raise MigrationError("target_foreign_keys_differ:" + table.name)
        checks = [row for row in constraints if row["contype"] == "c"]
        expected_check = "CHECK((role=ANY(ARRAY['user','assistant'])))"
        if table.role_check:
            if len(checks) != 1 or not checks[0]["convalidated"] or _compact_expression(checks[0]["definition"]) != expected_check:
                raise MigrationError("target_role_check_differs")
        elif checks:
            raise MigrationError("unexpected_target_check:" + table.name)
        if any(row["contype"] not in {"p", "f", "c"} for row in constraints):
            raise MigrationError("unexpected_target_constraint:" + table.name)
        indexes = connection.execute(
            "SELECT ci.relname AS name, i.indisunique, i.indisvalid, i.indoption::smallint[] AS options, "
            "ARRAY(SELECT coll.collname FROM unnest(i.indcollation) WITH ORDINALITY u(oid, ord) "
            "LEFT JOIN pg_catalog.pg_collation coll ON coll.oid=u.oid ORDER BY u.ord) AS collations, "
            "pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS predicate, "
            "ARRAY(SELECT pg_catalog.pg_get_indexdef(i.indexrelid, k, true) "
            "FROM generate_series(1, i.indnkeyatts) k) AS parts "
            "FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid "
            "WHERE n.nspname=? AND c.relname=? AND NOT i.indisprimary",
            (schema, table.name),
        ).fetchall()
        actual_indexes = {
            row["name"]: (bool(row["indisunique"]), tuple(
                _compact_expression(part + (' COLLATE "C"' if row["collations"][position] == "C" else "") + (" DESC" if row["options"][position] & 1 else ""))
                for position, part in enumerate(row["parts"])
            ))
            for row in indexes if row["indisvalid"] and row["predicate"] is None
            and all(collation in {None, "default", "C"} for collation in row["collations"])
        }
        expected_indexes = {index.name: (index.unique, tuple(_compact_expression(part) for part in index.parts)) for index in table.indexes}
        if len(actual_indexes) != len(indexes) or actual_indexes != expected_indexes:
            raise MigrationError("target_indexes_differ:" + table.name)
    verify_target_revision_triggers(connection, schema)


def _trigger_ddl_shape(sql: str, schema: str) -> str:
    # pg_get_triggerdef adds identifier quotes and parentheses around each
    # OR-only distinctness test. The fixed DDL contains no string literals or
    # mixed boolean operators; compare every remaining token, not a prefix.
    sql = sql.replace("CREATE OR REPLACE TRIGGER", "CREATE TRIGGER", 1)
    sql = re.sub(r'"([a-z_][a-z0-9_]*)"', r"\1", sql)
    # PostgreSQL omits current-schema qualifications for visible objects.
    # Only this exact, independently checked schema may be elided.
    sql = sql.replace(schema + ".", "")
    return re.sub(r"[\s();]", "", sql).lower()


def verify_target_revision_triggers(connection: Any, schema: str) -> None:
    triggers = connection.execute(
        "SELECT t.tgname, t.tgenabled, t.tgconstraint, t.tgdeferrable, t.tginitdeferred, "
        "pg_catalog.pg_get_triggerdef(t.oid, true) AS definition, "
        "p.proname, pn.nspname AS function_schema, p.prosrc, p.proconfig, "
        "p.prosecdef, p.pronargs, p.provolatile, p.proparallel, "
        "l.lanname, rt.typname AS return_type "
        "FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid "
        "JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace "
        "JOIN pg_catalog.pg_language l ON l.oid=p.prolang "
        "JOIN pg_catalog.pg_type rt ON rt.oid=p.prorettype "
        "WHERE n.nspname=? AND NOT t.tgisinternal", (schema,),
    ).fetchall()
    if not triggers:
        return
    contract = _revision_contract()
    function, expected_triggers = contract.postgres_revision_definitions(schema)
    expected_body = " ".join(function.split("$ff_radar_revision$")[1].split())
    for row in triggers:
        expected = expected_triggers.get(row["tgname"])
        valid = (
            expected is not None
            and _trigger_ddl_shape(row["definition"], schema) == _trigger_ddl_shape(expected, schema)
            and row["tgenabled"] == "O"
            and not row["tgconstraint"] and not row["tgdeferrable"] and not row["tginitdeferred"]
            and row["proname"] == contract.POSTGRES_REVISION_FUNCTION
            and row["function_schema"] == schema
            and " ".join(row["prosrc"].split()) == expected_body
            and row["proconfig"] == ["search_path=pg_catalog"]
            and not row["prosecdef"] and row["pronargs"] == 0
            and row["provolatile"] == "v" and row["proparallel"] == "u"
            and row["lanname"] == "plpgsql" and row["return_type"] == "trigger"
        )
        if not valid:
            raise MigrationError("unexpected_target_trigger")


def check_target_foreign_keys(connection: Any, tables: Sequence[Table]) -> None:
    # SET CONSTRAINTS validates this transaction's inserts; anti-joins also
    # check a pre-existing idempotent target, without printing the bad rows.
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    for table in tables:
        for fk in table.foreign_keys:
            non_null = " AND ".join(f"child.{identifier(col)} IS NOT NULL" for col in fk.columns)
            join = " AND ".join(f"parent.{identifier(right)}=child.{identifier(left)}" for left, right in zip(fk.columns, fk.target_columns))
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM {identifier(table.name)} child WHERE {non_null} "
                f"AND NOT EXISTS (SELECT 1 FROM {identifier(fk.target)} parent WHERE {join})"
            ).fetchone()
            if row["count"]:
                raise MigrationError("target_foreign_key_check_failed:" + table.name)


def calibrate_sequences(connection: Any, tables: Sequence[Table], schema: str, *, apply: bool) -> None:
    for table in tables:
        if not table.identity:
            continue
        row = connection.execute("SELECT pg_catalog.pg_get_serial_sequence(?, ?) AS name", (f"{schema}.{table.name}", table.identity)).fetchone()
        sequence = row["name"]
        if not isinstance(sequence, str) or sequence.count(".") != 1:
            raise MigrationError("target_identity_sequence_missing")
        sequence_schema, sequence_name = sequence.split(".")
        if sequence_schema != schema:
            raise MigrationError("target_identity_sequence_in_wrong_schema")
        qualified = identifier(sequence_schema) + "." + identifier(sequence_name)
        if apply:
            connection.execute("SELECT pg_catalog.setval(?::regclass, ?, ?)", (sequence, max(1, table.high_water), table.high_water > 0))
        actual = connection.execute(f"SELECT last_value, is_called FROM {qualified}").fetchone()
        next_value = actual["last_value"] + (1 if actual["is_called"] else 0)
        if next_value <= table.high_water:
            raise MigrationError("target_identity_high_water_mismatch:" + table.name)


def apply_snapshot(
    snapshot: Path,
    tables: Sequence[Table],
    expected: dict[str, dict[str, Any]],
    *,
    target_env: str = "DATABASE_URL",
    schema: str = "frostfire",
    source_kind: str | None = None,
) -> str:
    schema_name(schema)
    if not ENV_NAME.fullmatch(target_env):
        raise MigrationError("invalid_target_environment_name")
    if source_kind not in {"production", "test"}:
        raise MigrationError("apply_requires_explicit_source_kind")
    original = read_sqlite(snapshot)
    try:
        if source_kind == "production" and synthetic_user_count(original):
            raise MigrationError("synthetic_users_cannot_be_migrated_as_production")
        dsn = os.environ.get(target_env, "").strip()
        if not dsn:
            raise MigrationError("target_database_environment_is_missing")
        validate_target_dsn(dsn, test_source=source_kind == "test", target_env=target_env)
        with _connect_postgres(dsn, schema) as target:
            target.execute("SET LOCAL lock_timeout = '5s'")
            target.execute("SET LOCAL statement_timeout = '300s'")
            lock_key = int.from_bytes(hashlib.sha256(schema.encode()).digest()[:4], "big", signed=True)
            if not target.execute("SELECT pg_catalog.pg_try_advisory_xact_lock(?, ?) AS locked", (1179798866, lock_key)).fetchone()["locked"]:
                raise MigrationError("target_migration_is_already_running")
            objects = _target_objects(target, schema)
            existing_tables = {row["relname"] for row in objects if row["relkind"] == "r"}
            if any(row["relkind"] not in {"r", "S"} for row in objects):
                raise MigrationError("target_contains_unsupported_objects")
            if objects and existing_tables != TABLE_SET:
                raise MigrationError("target_is_not_empty_or_an_identical_complete_migration")
            if existing_tables:
                target.execute("LOCK TABLE " + ", ".join(identifier(name) for name in APPLICATION_TABLES) + " IN ACCESS EXCLUSIVE MODE")
                verify_target_schema(target, tables, schema)
                if database_digest(target, tables) != expected:
                    raise MigrationError("target_contains_different_data_no_changes_made")
                check_target_foreign_keys(target, tables)
                calibrate_sequences(target, tables, schema, apply=False)
                return "identical_target_skipped"
            target.ensure_schema()
            for table in tables:
                target.execute(create_table_sql(table))
                for index in table.indexes:
                    target.execute(create_index_sql(table, index))
            target.execute("SET CONSTRAINTS ALL DEFERRED")
            for table in tables:
                names = ", ".join(identifier(column.name) for column in table.columns)
                sql = f"INSERT INTO {identifier(table.name)} ({names}) VALUES (" + ", ".join("?" for _ in table.columns) + ")"
                for rows in row_batches(original.execute(f"SELECT {names} FROM {identifier(table.name)}")):
                    target.executemany(sql, [tuple(row) for row in rows])
            verify_target_schema(target, tables, schema)
            if database_digest(target, tables) != expected:
                raise MigrationError("target_content_hash_mismatch_rolled_back")
            check_target_foreign_keys(target, tables)
            calibrate_sequences(target, tables, schema, apply=True)
            # The context manager commits only after every check above passed.
        return "committed_and_verified"
    finally:
        original.close()


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's normal unknown-argument errors would echo an accidental DSN.
        self.exit(2, "invalid_arguments; use --help (credentials belong only in environment variables)\n")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="authorized current-instance SQLite file or full SQLite backup")
    parser.add_argument("--backup-dir", type=Path, help="new or existing owner-only 0700 directory; defaults to a new private temporary directory")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="default: take and validate a local backup; never connect to PostgreSQL")
    action.add_argument("--apply", action="store_true", help="import verified backup in one PostgreSQL transaction")
    parser.add_argument("--target-env", default="DATABASE_URL", help="name of environment variable holding the PostgreSQL connection string")
    parser.add_argument("--schema", default="frostfire", help="private PostgreSQL schema (never public/auth/storage)")
    parser.add_argument("--source-kind", choices=("production", "test"), help="required with --apply; test sources are restricted to the loopback test environment")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        schema_name(args.schema)
        snapshot = backup_sqlite(args.source, args.backup_dir)
        original = read_sqlite(snapshot)
        try:
            check_sqlite(original)
            tables = inspect_schema(original)
            digests = database_digest(original, tables)
            fixture_users = synthetic_user_count(original)
        finally:
            original.close()
        status = "dry_run_verified_no_remote_connection"
        if args.apply:
            status = apply_snapshot(snapshot, tables, digests, target_env=args.target_env, schema=args.schema, source_kind=args.source_kind)
        with snapshot.open("rb") as source_file:
            file_hash = hashlib.file_digest(source_file, "sha256").hexdigest()
        report = {
            "format": "FROSTFIRE_DATABASE_BACKUP_V1", "status": status,
            "snapshot": str(snapshot), "snapshot_sha256": file_hash,
            "schema_sha256": schema_digest(tables),
            "data_sha256": hashlib.sha256(json_bytes(digests)).hexdigest(),
            "application_table_count": len(tables),
            "foreign_key_count": sum(len(table.foreign_keys) for table in tables),
            "synthetic_user_count": fixture_users,
            "identity_high_water": {table.name: table.high_water for table in tables if table.identity},
            "tables": digests,
        }
        manifest = snapshot.with_suffix(".manifest.json")
        with os.fdopen(exclusive_file(manifest), "wb") as output:
            output.write(json_bytes(report) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except MigrationError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # SQLite/Postgres exceptions can include complete SQL rows and DSNs.
        print('{"status":"failed","reason":"database_operation_failed_details_redacted"}', file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
