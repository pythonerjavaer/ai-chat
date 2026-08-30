"""PostgreSQL bridge for the application's existing parameterised SQL.

No settings import or environment reads: only the caller selects a database.
An unavailable PostgreSQL database is never permission to open a local SQLite
file. SQL is tokenised before conversion; bound values are never rewritten.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import queue
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_IDENTITY_TABLES = frozenset({
    "users", "messages", "chunks", "token_usage", "api_usage_events",
    "radar_events", "radar_source_snapshots",
})
_MANAGED_SCHEMAS = frozenset({
    "public", "auth", "storage", "realtime", "extensions", "information_schema",
    "graphql", "graphql_public", "supabase_functions", "supabase_migrations",
})


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or len(value) > 63:
        raise sqlite3.ProgrammingError("Invalid database identifier.")
    return '"' + value + '"'


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str

    @property
    def word(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


def _tokens(sql: str) -> list[_Token]:
    if not isinstance(sql, str) or "\x00" in sql:
        raise sqlite3.ProgrammingError("Invalid SQL statement.")
    result: list[_Token] = []
    i = 0
    while i < len(sql):
        char = sql[i]
        if char.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < len(sql) and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise sqlite3.ProgrammingError("Unclosed SQL comment.")
            continue
        if char in {"'", '"', chr(96)}:
            start, quote = i, char
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise sqlite3.ProgrammingError("Unclosed SQL quoted value.")
            text = sql[start:i]
            if quote == chr(96):
                text = '"' + text[1:-1].replace(chr(96) * 2, chr(96)).replace('"', '""') + '"'
            result.append(_Token("string" if quote == "'" else "identifier", text))
            continue
        if char == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if match:
                delimiter = match.group(0)
                end = sql.find(delimiter, i + len(delimiter))
                if end < 0:
                    raise sqlite3.ProgrammingError("Unclosed SQL function body.")
                end += len(delimiter)
                result.append(_Token("string", sql[i:end]))
                i = end
                continue
        if char == "?":
            if i + 1 < len(sql) and sql[i + 1].isdigit():
                raise sqlite3.ProgrammingError("Numbered SQL parameters are not supported.")
            result.append(_Token("parameter", "?"))
            i += 1
            continue
        if char.isalpha() or char == "_":
            start = i
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            result.append(_Token("word", sql[start:i]))
            continue
        if char.isdigit():
            match = re.match(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", sql[i:])
            assert match is not None
            result.append(_Token("number", match.group(0)))
            i += len(match.group(0))
            continue
        operator = next(
            (op for op in ("->>", "#>>", "<=", ">=", "<>", "!=", "||", "::", "->", "#>")
             if sql.startswith(op, i)),
            char,
        )
        result.append(_Token("punctuation", operator))
        i += len(operator)
    return result


def _render(tokens: Sequence[_Token], *, bind: bool = True) -> str:
    return " ".join(
        "%s" if token.kind == "parameter"
        else token.text.replace("%", "%%") if bind else token.text
        for token in tokens
    )


def _closing(tokens: Sequence[_Token], start: int) -> int:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].text == "(":
            depth += 1
        elif tokens[index].text == ")":
            depth -= 1
            if depth == 0:
                return index
    raise sqlite3.ProgrammingError("Unbalanced SQL parentheses.")


def _split(tokens: Sequence[_Token], separator: str = ",") -> list[list[_Token]]:
    parts: list[list[_Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.text == separator and depth == 0:
            parts.append([])
        else:
            parts[-1].append(token)
            depth += int(token.text == "(") - int(token.text == ")")
    return parts


def _value(token: _Token) -> str:
    if token.kind == "word":
        return token.text
    if token.kind in {"identifier", "string"} and token.text[:1] in {"'", '"'}:
        quote = token.text[0]
        return token.text[1:-1].replace(quote + quote, quote)
    raise sqlite3.ProgrammingError("Invalid SQL identifier or literal.")


def _operand_start(tokens: Sequence[_Token], end: int) -> int:
    if end <= 0:
        raise sqlite3.ProgrammingError("Unsupported SQL collation expression.")
    start = end - 1
    if tokens[start].text == ")":
        depth = 1
        start -= 1
        while start >= 0 and depth:
            depth += int(tokens[start].text == ")") - int(tokens[start].text == "(")
            start -= 1
        if depth:
            raise sqlite3.ProgrammingError("Unbalanced SQL expression.")
        start += 1
        if start and tokens[start - 1].kind in {"word", "identifier"}:
            start -= 1
    while start >= 2 and tokens[start - 1].text == ".":
        start -= 2
    return start


def _operand_end(tokens: Sequence[_Token], start: int) -> int:
    if start >= len(tokens):
        raise sqlite3.ProgrammingError("Missing SQL expression.")
    end = start + 1
    while end + 1 < len(tokens) and tokens[end].text == ".":
        end += 2
    if end < len(tokens) and tokens[end].text == "(":
        end = _closing(tokens, end) + 1
    elif tokens[start].text == "(":
        end = _closing(tokens, start) + 1
    return end


def _fold(tokens: Sequence[_Token]) -> list[_Token]:
    return _tokens("translate(") + list(tokens) + _tokens(
        f", '{_UPPER}', '{_LOWER}') COLLATE \"C\""
    )


def _rewrite_collation(tokens: list[_Token]) -> list[_Token]:
    comparison = {"=", "!=", "<>", "<", ">", "<=", ">="}
    while True:
        index = next(
            (i for i in range(len(tokens) - 1)
             if tokens[i].word == "COLLATE" and tokens[i + 1].word == "NOCASE"),
            None,
        )
        if index is None:
            break
        start = _operand_start(tokens, index)
        operand = tokens[start:index]
        if start and tokens[start - 1].text in comparison:
            left = _operand_start(tokens, start - 1)
            replacement = _fold(tokens[left:start - 1]) + [tokens[start - 1]] + _fold(operand)
            tokens[left:index + 2] = replacement
        elif index + 2 < len(tokens) and tokens[index + 2].text in comparison:
            end = _operand_end(tokens, index + 3)
            replacement = _fold(operand) + [tokens[index + 2]] + _fold(tokens[index + 3:end])
            tokens[start:end] = replacement
        else:
            tokens[start:index + 2] = _fold(operand)
    # SQLite LIKE folds ASCII, unlike Unicode-sensitive PostgreSQL ILIKE.
    index = 0
    while index < len(tokens):
        if tokens[index].word != "LIKE":
            index += 1
            continue
        left_end = index - int(index > 0 and tokens[index - 1].word == "NOT")
        start = _operand_start(tokens, left_end)
        end = _operand_end(tokens, index + 1)
        middle = tokens[left_end:index + 1]
        replacement = _fold(tokens[start:left_end]) + middle + _fold(tokens[index + 1:end])
        # SQLite has no implicit LIKE escape character; PostgreSQL otherwise
        # treats a backslash inside a user's search pattern as an escape.
        if end >= len(tokens) or tokens[end].word != "ESCAPE":
            replacement.extend(_tokens("ESCAPE ''"))
        tokens[start:end] = replacement
        index = start + len(replacement)
    return tokens


def _rewrite_functions(tokens: list[_Token], schema: str) -> list[_Token]:
    index = 0
    while index + 1 < len(tokens):
        name = tokens[index].word
        if name not in {"DATE", "DATETIME", "JULIANDAY", "JSON_EXTRACT"} or tokens[index + 1].text != "(":
            index += 1
            continue
        end = _closing(tokens, index + 1)
        args = _split(tokens[index + 2:end])
        replacement: list[_Token] | None = None
        if name in {"DATE", "DATETIME"}:
            if len(args) == 1 and len(args[0]) == 1 and args[0][0].kind == "string" and _value(args[0][0]).lower() == "now":
                fmt = "YYYY-MM-DD" if name == "DATE" else "YYYY-MM-DD HH24:MI:SS"
                replacement = _tokens(f"to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', '{fmt}')")
        elif name == "JULIANDAY" and len(args) == 1:
            replacement = (
                _tokens(f"{_identifier(schema)}._ff_julianday(CAST(")
                + _rewrite_functions(args[0], schema)
                + _tokens("AS TEXT))")
            )
        elif name == "JSON_EXTRACT":
            if len(args) != 2 or len(args[1]) != 1 or args[1][0].kind != "string":
                raise sqlite3.ProgrammingError("Unsupported JSON path expression.")
            path = _value(args[1][0])
            if not re.fullmatch(r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+", path):
                raise sqlite3.ProgrammingError("Unsupported JSON path expression.")
            keys = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", path)
            key_sql = ", ".join("'" + (key or item) + "'" for key, item in keys)
            replacement = (
                _tokens("(CAST(NULLIF(") + args[0]
                + _tokens(f", '') AS JSONB) #>> ARRAY[{key_sql}])")
            )
        if replacement is not None:
            tokens[index:end + 1] = replacement
            index += len(replacement)
        else:
            index += 1
    return tokens


@dataclass(frozen=True)
class CompiledSQL:
    sql: str
    parameters: tuple[Any, ...] = field(repr=False)
    action: str = "query"
    followup_sql: tuple[str, ...] = ()
    identity_table: str | None = None
    implicit_returning: bool = False
    insert: bool = False
    guarded_insert: bool = False


def _create_table(
    tokens: list[_Token], schema: str,
) -> tuple[list[_Token], tuple[str, ...], str | None]:
    position = 2
    if [token.word for token in tokens[position:position + 3]] == ["IF", "NOT", "EXISTS"]:
        position += 3
    table = _value(tokens[position])
    _identifier(table)
    if position + 1 < len(tokens) and tokens[position + 1].text == ".":
        table = _value(tokens[position + 2])
        _identifier(table)
        position += 2
    opening = position + 1
    if opening >= len(tokens) or tokens[opening].text != "(":
        raise sqlite3.ProgrammingError("Unsupported CREATE TABLE statement.")
    closing = _closing(tokens, opening)
    declarations = _split(tokens[opening + 1:closing])
    followup: list[str] = []
    identity = None
    for declaration in declarations:
        original = [token.word for token in declaration]
        has_identity = "AUTOINCREMENT" in original or "IDENTITY" in original
        if has_identity and declaration and _value(declaration[0]).lower() == "id":
            identity = table.lower()
        if "AUTOINCREMENT" in original:
            index = original.index("AUTOINCREMENT")
            del declaration[index]
            type_index = next((i for i, token in enumerate(declaration) if token.word == "INTEGER"), None)
            if type_index is None:
                raise sqlite3.ProgrammingError("Unsupported identity column.")
            declaration[type_index:type_index + 1] = _tokens("BIGINT GENERATED BY DEFAULT AS IDENTITY")
        declaration[:] = [
            replacement
            for token in declaration
            for replacement in (
                _tokens("BIGINT") if token.word == "INTEGER"
                else _tokens("DOUBLE PRECISION") if token.word == "REAL"
                else [token]
            )
        ]
        words = [token.word for token in declaration]
        if "COLLATE" in words:
            index = words.index("COLLATE")
            if index + 1 < len(words) and words[index + 1] == "NOCASE":
                column = _value(declaration[0])
                _identifier(column)
                del declaration[index:index + 2]
                if any(token.word == "UNIQUE" for token in declaration):
                    declaration[:] = [token for token in declaration if token.word != "UNIQUE"]
                    index_name = f"{table}_{column}_nocase_key"
                    if len(index_name) > 63:
                        index_name = index_name[:46] + "_" + hashlib.sha256(index_name.encode()).hexdigest()[:16]
                    expression = _render(_fold(_tokens(_identifier(column))), bind=False)
                    followup.append(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {_identifier(index_name)} "
                        f"ON {_identifier(schema)}.{_identifier(table)} ({expression})"
                    )
        words = [token.word for token in declaration]
        if "REFERENCES" in words and "DEFERRABLE" not in words:
            declaration.extend(_tokens("DEFERRABLE INITIALLY IMMEDIATE"))
    middle: list[_Token] = []
    for index, declaration in enumerate(declarations):
        if index:
            middle.append(_Token("punctuation", ","))
        middle.extend(declaration)
    return tokens[:opening + 1] + middle + tokens[closing:], tuple(followup), identity


def compile_sql(
    sql: str,
    parameters: Sequence[Any] | None = None,
    *,
    schema: str = "frostfire",
    identity_tables: Iterable[str] = _IDENTITY_TABLES,
) -> CompiledSQL:
    """Compile the project's SQLite dialect without touching parameter values."""
    _identifier(schema)
    if isinstance(parameters, (str, bytes, bytearray, dict)):
        raise sqlite3.ProgrammingError("Positional SQL parameters are required.")
    params = tuple(parameters) if parameters is not None else ()
    tokens = _tokens(sql)
    while tokens and tokens[-1].text == ";":
        tokens.pop()
    if not tokens:
        if params:
            raise sqlite3.ProgrammingError("Incorrect SQL parameter count.")
        return CompiledSQL("", (), action="noop")
    if any(token.text == ";" for token in tokens):
        raise sqlite3.ProgrammingError("Use executescript for multiple statements.")
    if sum(token.kind == "parameter" for token in tokens) != len(params):
        raise sqlite3.ProgrammingError("Incorrect SQL parameter count.")
    words = [token.word for token in tokens]
    if words[:2] == ["BEGIN", "IMMEDIATE"]:
        return CompiledSQL("", (), action="begin_immediate")
    if words[0] in {"BEGIN", "COMMIT", "END", "ROLLBACK"} and len(tokens) == 1:
        return CompiledSQL("", (), action="commit" if words[0] == "END" else words[0].lower())
    if words[0] == "PRAGMA":
        if len(tokens) == 5 and words[1] == "TABLE_INFO" and tokens[2].text == "(" and tokens[-1].text == ")":
            table = _value(tokens[3])
            _identifier(table)
            return CompiledSQL(
                """SELECT c.ordinal_position - 1 AS cid, c.column_name AS name,
                          c.data_type AS type,
                          CASE WHEN c.is_nullable='NO' THEN 1 ELSE 0 END AS "notnull",
                          c.column_default AS dflt_value,
                          COALESCE(array_position(p.conkey, a.attnum), 0) AS pk
                   FROM information_schema.columns c
                   JOIN pg_catalog.pg_namespace n ON n.nspname=c.table_schema
                   JOIN pg_catalog.pg_class t ON t.relnamespace=n.oid AND t.relname=c.table_name
                   JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attname=c.column_name
                   LEFT JOIN pg_catalog.pg_constraint p ON p.conrelid=t.oid AND p.contype='p'
                   WHERE c.table_schema=%s AND c.table_name=%s ORDER BY c.ordinal_position""",
                (schema, table),
            )
        if words[1:2] == ["FOREIGN_KEYS"]:
            if len(tokens) == 2:
                return CompiledSQL("SELECT 1 AS foreign_keys", ())
            if len(tokens) == 4 and tokens[2].text == "=" and (words[3] == "ON" or tokens[3].text == "1"):
                return CompiledSQL("", (), action="noop")
        if words[1:2] == ["JOURNAL_MODE"] and len(tokens) == 4 and words[3] == "WAL":
            return CompiledSQL("", (), action="noop")
        raise sqlite3.ProgrammingError("Unsupported SQLite PRAGMA for PostgreSQL.")
    followup: tuple[str, ...] = ()
    identity = None
    create = words[:2] == ["CREATE", "TABLE"]
    if create:
        tokens, followup, identity = _create_table(tokens, schema)
    elif words[:2] == ["ALTER", "TABLE"]:
        tokens = [
            replacement
            for token in tokens
            for replacement in (
                _tokens("BIGINT") if token.word == "INTEGER"
                else _tokens("DOUBLE PRECISION") if token.word == "REAL"
                else [token]
            )
        ]
    ignored = words[:3] == ["INSERT", "OR", "IGNORE"]
    if ignored:
        tokens[1:3] = []
    tokens = _rewrite_functions(tokens, schema)
    if not create:
        tokens = _rewrite_collation(tokens)
    words = [token.word for token in tokens]
    insert = bool(words and words[0] == "INSERT")
    if ignored:
        if "RETURNING" in words:
            position = words.index("RETURNING")
            tokens[position:position] = _tokens("ON CONFLICT DO NOTHING")
        else:
            tokens.extend(_tokens("ON CONFLICT DO NOTHING"))
    words = [token.word for token in tokens]
    implicit_returning = False
    if insert and "INTO" in words:
        position = words.index("INTO") + 1
        table = _value(tokens[position])
        if position + 1 < len(tokens) and tokens[position + 1].text == ".":
            table = _value(tokens[position + 2])
        if table.lower() in set(identity_tables) and "RETURNING" not in words:
            tokens.extend(_tokens("RETURNING id"))
            implicit_returning = True
    return CompiledSQL(
        _render(tokens), params, action="create_table" if create else "query",
        followup_sql=followup, identity_table=identity,
        implicit_returning=implicit_returning, insert=insert,
        guarded_insert=insert and "CONFLICT" not in words,
    )


def _script_statements(sql: str) -> Iterator[str]:
    tokens = _tokens(sql)
    statement: list[_Token] = []
    for token in tokens:
        if token.text == ";":
            if statement:
                yield _render(statement, bind=False)
                statement = []
        else:
            statement.append(token)
    if statement:
        yield _render(statement, bind=False)


class Row(Sequence[Any]):
    """SQLite-compatible named/positional row; iteration yields values."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self._names = tuple(names)
        self._values = tuple(values)
        self._lookup: dict[str, int] = {}
        for index, name in enumerate(self._names):
            self._lookup.setdefault(name, index)
            self._lookup.setdefault(name.lower(), index)

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            position = self._lookup.get(key, self._lookup.get(key.lower()))
            if position is None:
                raise IndexError("No item with that key.")
            return self._values[position]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)


class Cursor:
    def __init__(
        self, rows: Iterable[Row] = (), *, names: Sequence[str] = (),
        rowcount: int = -1, lastrowid: int | None = None,
    ):
        self._rows = list(rows)
        self._position = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.arraysize = 1
        self.description = tuple((name, None, None, None, None, None, None) for name in names) or None

    def fetchone(self) -> Row | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[Row]:
        amount = self.arraysize if size is None else size
        if amount < 0:
            return self.fetchall()
        result = self._rows[self._position:self._position + amount]
        self._position += len(result)
        return result

    def fetchall(self) -> list[Row]:
        result = self._rows[self._position:]
        self._position = len(self._rows)
        return result

    def close(self) -> None:
        self._position = len(self._rows)

    def __iter__(self) -> Iterator[Row]:
        while (row := self.fetchone()) is not None:
            yield row


def _safe_error(error: Exception) -> sqlite3.DatabaseError:
    state = str(getattr(error, "sqlstate", "") or "")
    if not re.fullmatch(r"[A-Z0-9]{5}", state):
        state = ""
    suffix = f" (SQLSTATE {state})." if state else "."
    if state.startswith("23"):
        return sqlite3.IntegrityError("PostgreSQL constraint violation" + suffix)
    if state.startswith("42") or state.startswith("22"):
        return sqlite3.ProgrammingError("PostgreSQL statement could not be executed" + suffix)
    return sqlite3.OperationalError("PostgreSQL operation is unavailable" + suffix)


def _advisory_key(schema: str, purpose: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"frostfire:{schema}:{purpose}".encode()).digest()[:8],
        "big", signed=True,
    )


class _Pool:
    """Bounded, lazy pool without a background worker logging connection errors."""

    def __init__(self, dsn: str, schema: str, max_size: int):
        self._dsn = dsn
        self.schema = schema
        self.max_size = max_size
        self._slots = threading.BoundedSemaphore(max_size)
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(max_size)
        self._state_lock = threading.Lock()
        self.closed = False
        self.schema_ready = False
        self.identity_tables = set(_IDENTITY_TABLES)

    def checkout(self, timeout: float) -> Any:
        if self.closed or not self._slots.acquire(timeout=timeout):
            raise sqlite3.OperationalError("PostgreSQL connection pool is busy.")
        try:
            if self.closed:
                raise sqlite3.OperationalError("PostgreSQL connection pool is closed.")
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                connection = None
            if connection is not None and connection.closed:
                connection = None
            if connection is None:
                # Offline migration validation need not install or contact PG.
                import psycopg
                from psycopg.conninfo import conninfo_to_dict

                options = conninfo_to_dict(self._dsn)
                host = str(options.get("host") or "")
                if (host.lower().rstrip(".").endswith(".pooler.supabase.com")
                        and str(options.get("port") or "") == "6543"):
                    # search_path/time zone belong to the connection session.
                    # Transaction pooling cannot preserve these between uses.
                    raise sqlite3.OperationalError("Use the PostgreSQL session pooler.")
                loopback = host.lower() == "localhost"
                try:
                    loopback = loopback or ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
                secure_options = {}
                if not loopback:
                    # Do not silently trade hostname/CA verification for mere
                    # encryption. Supabase's public CA is supplied in the DSN.
                    secure_options["sslmode"] = "verify-full"
                connection = psycopg.connect(
                    self._dsn, connect_timeout=max(1, min(30, math.ceil(timeout))),
                    autocommit=False, prepare_threshold=None, **secure_options,
                )
                try:
                    connection.execute(f"SET search_path TO {_identifier(self.schema)}, pg_catalog")
                    connection.execute("SET TIME ZONE 'UTC'")
                    connection.commit()
                except Exception:
                    connection.close()
                    raise
            return connection
        except Exception as error:
            self._slots.release()
            raise _safe_error(error) from None

    def checkin(self, connection: Any) -> None:
        try:
            try:
                if not connection.closed:
                    connection.rollback()
            except Exception:
                connection.close()
            if self.closed or connection.closed:
                connection.close()
            else:
                self._idle.put_nowait(connection)
        finally:
            self._slots.release()

    def close(self) -> None:
        with self._state_lock:
            self.closed = True
            while True:
                try:
                    self._idle.get_nowait().close()
                except queue.Empty:
                    break


_pools_lock = threading.Lock()
_pools: dict[tuple[str, str, int], _Pool] = {}


class Connection:
    def __init__(self, pool: _Pool, timeout: float):
        self._pool = pool
        self._raw = pool.checkout(timeout)
        self.schema = pool.schema
        self.timeout = timeout
        self._closed = False
        self._schema_created = False
        self._timeouts_set = False
        self._savepoint_counter = 0

    def _check_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("Database connection is closed.")

    def _timeouts(self) -> None:
        if not self._timeouts_set:
            milliseconds = str(max(1, math.ceil(self.timeout * 1000)))
            self._raw.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)", (milliseconds, milliseconds),
            )
            self._timeouts_set = True

    def ensure_schema(self) -> None:
        """Create/protect the private schema inside the caller's transaction."""
        self._check_open()
        if self._schema_created or self._pool.schema_ready:
            return
        schema = _identifier(self.schema)
        try:
            self._timeouts()
            self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (_advisory_key(self.schema, "schema"),))
            if self._pool.schema_ready:
                return
            self._raw.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            roles = ["PUBLIC"]
            roles.extend(
                _identifier(row[0])
                for row in self._raw.execute(
                    "SELECT rolname FROM pg_catalog.pg_roles "
                    "WHERE rolname IN ('anon', 'authenticated')"
                ).fetchall()
            )
            recipients = ", ".join(roles)
            self._raw.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {recipients}")
            for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                self._raw.execute(f"REVOKE ALL ON ALL {object_type} IN SCHEMA {schema} FROM {recipients}")
                self._raw.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                    f"REVOKE ALL ON {object_type} FROM {recipients}"
                )
            self._raw.execute(
                f"""CREATE OR REPLACE FUNCTION {schema}._ff_julianday(value TEXT)
                    RETURNS DOUBLE PRECISION LANGUAGE plpgsql STABLE
                    SET search_path = pg_catalog AS $ff$
                    BEGIN
                        IF value IS NULL OR btrim(value) = '' OR
                           lower(btrim(value)) IN ('infinity', '-infinity') THEN
                            RETURN NULL;
                        END IF;
                        RETURN extract(epoch FROM value::timestamptz) / 86400.0 + 2440587.5;
                    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
                        RETURN NULL;
                    END;
                    $ff$"""
            )
            self._raw.execute(f"REVOKE ALL ON FUNCTION {schema}._ff_julianday(TEXT) FROM {recipients}")
            self._schema_created = True
        except Exception as error:
            raise _safe_error(error) from None

    @staticmethod
    def _result(cursor: Any, plan: CompiledSQL) -> Cursor:
        names = tuple(column.name for column in cursor.description) if cursor.description else ()
        rows = [Row(names, values) for values in cursor.fetchall()] if names else []
        lastrowid = None
        if rows and "id" in names:
            value = rows[0]["id"]
            if isinstance(value, int) and not isinstance(value, bool):
                lastrowid = value
        if plan.implicit_returning:
            return Cursor(rowcount=cursor.rowcount, lastrowid=lastrowid)
        return Cursor(rows, names=names, rowcount=cursor.rowcount, lastrowid=lastrowid)

    def execute(self, sql: str, parameters: Sequence[Any] | None = None) -> Cursor:
        self._check_open()
        plan = compile_sql(
            sql, parameters, schema=self.schema,
            identity_tables=self._pool.identity_tables,
        )
        if plan.action == "noop":
            return Cursor()
        if plan.action in {"commit", "rollback"}:
            getattr(self, plan.action)()
            return Cursor()
        savepoint = None
        try:
            self._timeouts()
            if plan.action == "begin":
                return Cursor()
            if plan.action == "begin_immediate":
                self._raw.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_advisory_key(self.schema, "short-write"),),
                )
                return Cursor()
            if plan.action == "create_table":
                self.ensure_schema()
            # PG errors otherwise poison the transaction; SQLite callers may
            # catch a duplicate INSERT and continue using the same connection.
            if plan.guarded_insert:
                self._savepoint_counter += 1
                savepoint = f"ff_insert_{self._savepoint_counter}"
                self._raw.execute(f"SAVEPOINT {savepoint}")
            with self._raw.cursor() as cursor:
                cursor.execute(plan.sql, plan.parameters)
                result = self._result(cursor, plan)
            for statement in plan.followup_sql:
                self._raw.execute(statement)
            if plan.identity_table:
                self._pool.identity_tables.add(plan.identity_table)
            if savepoint:
                self._raw.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except Exception as error:
            if savepoint:
                try:
                    self._raw.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._raw.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    pass
            raise _safe_error(error) from None

    def executemany(self, sql: str, parameter_sets: Iterable[Sequence[Any]]) -> Cursor:
        self._check_open()
        params = list(parameter_sets)
        if not params:
            return Cursor(rowcount=0)
        plans = [compile_sql(sql, item, schema=self.schema, identity_tables=()) for item in params]
        plan = plans[0]
        if plan.action != "query" or plan.followup_sql:
            raise sqlite3.ProgrammingError("executemany requires a single data statement.")
        savepoint = None
        try:
            self._timeouts()
            self._savepoint_counter += 1
            savepoint = f"ff_many_{self._savepoint_counter}"
            self._raw.execute(f"SAVEPOINT {savepoint}")
            with self._raw.cursor() as cursor:
                cursor.executemany(plan.sql, [item.parameters for item in plans])
                rowcount = cursor.rowcount
            self._raw.execute(f"RELEASE SAVEPOINT {savepoint}")
            return Cursor(rowcount=rowcount)
        except Exception as error:
            if savepoint:
                try:
                    self._raw.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._raw.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    pass
            raise _safe_error(error) from None

    def executescript(self, sql: str) -> Cursor:
        result = Cursor()
        for statement in _script_statements(sql):
            result = self.execute(statement)
        return result

    def commit(self) -> None:
        self._check_open()
        try:
            self._raw.commit()
            if self._schema_created:
                self._pool.schema_ready = True
            self._schema_created = False
            self._timeouts_set = False
        except Exception as error:
            raise _safe_error(error) from None

    def rollback(self) -> None:
        self._check_open()
        try:
            self._raw.rollback()
            self._schema_created = False
            self._timeouts_set = False
        except Exception as error:
            raise _safe_error(error) from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pool.checkin(self._raw)

    def __enter__(self) -> Connection:
        self._check_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            if exc_type is None:
                self.commit()
            else:
                try:
                    self.rollback()
                except sqlite3.Error:
                    pass
        finally:
            self.close()
        return False


def connect_postgres(
    dsn: str, *, schema: str = "frostfire", timeout: float = 30.0, max_size: int = 4,
) -> Connection:
    """Connect only to the explicitly supplied PostgreSQL database.

    Private schema creation is deferred to ensure_schema()/CREATE TABLE and is
    transactional. Connection failures contain neither DSNs nor server details.
    """
    _identifier(schema)
    if schema.lower() in _MANAGED_SCHEMAS or schema.lower().startswith("pg_"):
        raise sqlite3.ProgrammingError("PostgreSQL requires a private application schema.")
    if not isinstance(dsn, str) or not dsn.strip():
        raise sqlite3.OperationalError("PostgreSQL connection configuration is required.")
    if not isinstance(max_size, int) or isinstance(max_size, bool) or not 1 <= max_size <= 16:
        raise sqlite3.ProgrammingError("PostgreSQL pool size must be between 1 and 16.")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise sqlite3.ProgrammingError("Invalid PostgreSQL timeout.")
    key = (hashlib.sha256(dsn.encode()).hexdigest(), schema, max_size)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None or pool.closed:
            pool = _Pool(dsn, schema, max_size)
            _pools[key] = pool
    return Connection(pool, float(timeout))


def close_postgres_pools() -> None:
    """Close idle database connections; checked-out connections close on return."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()
