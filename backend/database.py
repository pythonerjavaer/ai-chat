import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import settings
from .workspaces import DEFAULT_WORKSPACE, validate_workspace
from .future_radar.schema import migrate as migrate_future_radar
from .future_radar.opportunity_cache import install_opportunity_revision


SPACE_RUN_HISTORY_LIMIT = 100
API_USAGE_RETENTION_DAYS = 30
API_USAGE_SQLITE_TIMEOUT_SECONDS = 0.05


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(*, timeout: float = 30.0) -> Any:
    if getattr(settings, "database_backend", "sqlite") == "postgres":
        from .storage import connect_postgres

        return connect_postgres(
            settings.database_url,
            schema=settings.database_schema,
            timeout=timeout,
            max_size=settings.database_pool_size,
        )
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def close_database_pools() -> None:
    if getattr(settings, "database_backend", "sqlite") == "postgres":
        from .storage import close_postgres_pools

        close_postgres_pools()


def init_db(*, connection_factory: Callable[[], Any] | None = None) -> None:
    with (connection_factory or connect)() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                privacy_accepted_at TEXT,
                privacy_version TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT 'general',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT 'general',
                file_type TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding TEXT NOT NULL,
                page INTEGER,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS spaces (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                icon TEXT NOT NULL,
                theme TEXT NOT NULL,
                template_id TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                monthly_token_budget INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                space_id TEXT,
                period TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS space_runs (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                requested_mode TEXT NOT NULL,
                execution_path TEXT NOT NULL,
                status TEXT NOT NULL,
                input_text TEXT NOT NULL,
                artifact TEXT NOT NULL DEFAULT '{}',
                reply TEXT NOT NULL DEFAULT '',
                estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
                max_output_tokens INTEGER NOT NULL DEFAULT 0,
                actual_input_tokens INTEGER NOT NULL DEFAULT 0,
                actual_output_tokens INTEGER NOT NULL DEFAULT 0,
                actual_total_tokens INTEGER NOT NULL DEFAULT 0,
                saved_tokens INTEGER NOT NULL DEFAULT 0,
                cached_from_run_id TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (cached_from_run_id) REFERENCES space_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS recruitment_profiles (
                user_id INTEGER PRIMARY KEY,
                desired_roles TEXT NOT NULL DEFAULT '[]',
                industries TEXT NOT NULL DEFAULT '[]',
                locations TEXT NOT NULL DEFAULT '[]',
                employer_types TEXT NOT NULL DEFAULT '[]',
                background TEXT NOT NULL DEFAULT '',
                education_level TEXT NOT NULL DEFAULT '',
                major_category TEXT NOT NULL DEFAULT '',
                school_tier TEXT NOT NULL DEFAULT '',
                experience_level TEXT NOT NULL DEFAULT '',
                skill_tags TEXT NOT NULL DEFAULT '[]',
                language_level TEXT NOT NULL DEFAULT '',
                undergraduate_major TEXT NOT NULL DEFAULT '',
                undergraduate_school_tier TEXT NOT NULL DEFAULT '',
                master_major TEXT NOT NULL DEFAULT '',
                master_school_tier TEXT NOT NULL DEFAULT '',
                undergraduate_school TEXT NOT NULL DEFAULT '',
                master_school TEXT NOT NULL DEFAULT '',
                composite_interest INTEGER NOT NULL DEFAULT 0,
                graduation_year INTEGER,
                availability_start TEXT,
                availability_end TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recruitment_jobs (
                id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                employer_type TEXT NOT NULL,
                title TEXT NOT NULL,
                city TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                opening_date TEXT,
                closing_date TEXT,
                requirements TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                historical_applicants INTEGER,
                historical_offers INTEGER,
                last_verified_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS recruitment_ingest_candidates (
                id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                source_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_thread_id TEXT,
                source_item_id TEXT,
                external_id TEXT,
                source_updated_at TEXT,
                company TEXT NOT NULL,
                employer_type TEXT NOT NULL,
                title TEXT NOT NULL,
                city TEXT NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                official_url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                source TEXT NOT NULL,
                opening_date TEXT,
                closing_date TEXT,
                requirements TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '[]',
                incoming_status TEXT NOT NULL DEFAULT 'open',
                payload_hash TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'pending',
                verification_reason TEXT,
                promoted_job_id TEXT,
                verified_opening_date TEXT,
                verified_closing_date TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                verified_at TEXT,
                rejected_at TEXT,
                FOREIGN KEY (promoted_job_id) REFERENCES recruitment_jobs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS recruitment_ingest_sources (
                source_key TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_thread_id TEXT,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_seen_at TEXT,
                last_source_updated_at TEXT,
                last_item_id TEXT,
                last_event_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recruitment_ingest_events (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_thread_id TEXT,
                received INTEGER NOT NULL DEFAULT 0,
                accepted INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                stale_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                closed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_key) REFERENCES recruitment_ingest_sources(source_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recruitment_watches (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                fetch_url TEXT,
                keywords TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_fingerprint TEXT,
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_http_status INTEGER,
                last_keyword_hits TEXT NOT NULL DEFAULT '[]',
                last_error TEXT,
                last_checked_at TEXT,
                last_changed_at TEXT,
                change_pending INTEGER NOT NULL DEFAULT 0,
                change_version INTEGER NOT NULL DEFAULT 0,
                change_acknowledged_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                method TEXT NOT NULL,
                route TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                ON sessions(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_documents_user
                ON documents(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_chunks_user
                ON chunks(user_id);
            """
        )
        _ensure_column(
            connection,
            "sessions",
            "workspace",
            "TEXT NOT NULL DEFAULT 'general'",
        )
        _ensure_column(
            connection,
            "documents",
            "workspace",
            "TEXT NOT NULL DEFAULT 'general'",
        )
        _ensure_column(connection, "documents", "file_type", "TEXT")
        _ensure_column(connection, "chunks", "page", "INTEGER")
        _ensure_column(connection, "users", "privacy_accepted_at", "TEXT")
        _ensure_column(connection, "users", "privacy_version", "TEXT")
        _ensure_column(connection, "users", "plan", "TEXT NOT NULL DEFAULT 'free'")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_user_workspace "
            "ON documents(user_id, workspace, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_workspace "
            "ON chunks(user_id, document_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_spaces_user_updated "
            "ON spaces(user_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_user_period "
            "ON token_usage(user_id, period)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_space_runs_space_created "
            "ON space_runs(space_id, user_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_space_runs_cache "
            "ON space_runs(space_id, user_id, fingerprint, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitment_jobs_deadline "
            "ON recruitment_jobs(status, closing_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitment_ingest_candidates_source "
            "ON recruitment_ingest_candidates(source_key, verification_status, last_seen_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitment_ingest_candidates_url "
            "ON recruitment_ingest_candidates(canonical_url)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_recruitment_ingest_sources_identity "
            "ON recruitment_ingest_sources(source_id, COALESCE(source_thread_id, ''))"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitment_ingest_events_source_created "
            "ON recruitment_ingest_events(source_key, created_at DESC)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_recruitment_watches_user_url "
            "ON recruitment_watches(user_id, url)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitment_watches_user_updated "
            "ON recruitment_watches(user_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_usage_events_created "
            "ON api_usage_events(created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_usage_events_user_created "
            "ON api_usage_events(user_id, created_at)"
        )
        _ensure_column(
            connection,
            "recruitment_ingest_candidates",
            "verified_opening_date",
            "TEXT",
        )
        _ensure_column(
            connection,
            "recruitment_ingest_candidates",
            "verified_closing_date",
            "TEXT",
        )
        for column, declaration in (
            ("education_level", "TEXT NOT NULL DEFAULT ''"),
            ("major_category", "TEXT NOT NULL DEFAULT ''"),
            ("school_tier", "TEXT NOT NULL DEFAULT ''"),
            ("experience_level", "TEXT NOT NULL DEFAULT ''"),
            ("skill_tags", "TEXT NOT NULL DEFAULT '[]'"),
            ("language_level", "TEXT NOT NULL DEFAULT ''"),
            ("undergraduate_major", "TEXT NOT NULL DEFAULT ''"),
            ("undergraduate_school_tier", "TEXT NOT NULL DEFAULT ''"),
            ("master_major", "TEXT NOT NULL DEFAULT ''"),
            ("master_school_tier", "TEXT NOT NULL DEFAULT ''"),
            ("undergraduate_school", "TEXT NOT NULL DEFAULT ''"),
            ("master_school", "TEXT NOT NULL DEFAULT ''"),
            ("composite_interest", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _ensure_column(connection, "recruitment_profiles", column, declaration)
        added_change_pending = _ensure_column(
            connection,
            "recruitment_watches",
            "change_pending",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            "recruitment_watches",
            "change_acknowledged_at",
            "TEXT",
        )
        added_change_version = _ensure_column(
            connection,
            "recruitment_watches",
            "change_version",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            "recruitment_watches",
            "fetch_url",
            "TEXT",
        )
        _ensure_column(
            connection,
            "recruitment_watches",
            "watch_type",
            "TEXT NOT NULL DEFAULT 'page'",
        )
        _ensure_column(
            connection,
            "recruitment_watches",
            "company_name",
            "TEXT NOT NULL DEFAULT ''",
        )
        connection.execute(
            "UPDATE recruitment_watches SET fetch_url = url "
            "WHERE fetch_url IS NULL OR fetch_url = ''"
        )
        if added_change_pending:
            connection.execute(
                "UPDATE recruitment_watches SET change_pending = 1 "
                "WHERE last_status = 'changed'"
            )
        if added_change_version:
            connection.execute(
                "UPDATE recruitment_watches SET change_version = 1 "
                "WHERE last_status = 'changed' OR change_pending = 1"
            )
        migrate_future_radar(connection)
        install_opportunity_revision(connection)


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> bool:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        return True
    return False


def create_user(
    username: str,
    password_hash: str,
    privacy_version: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    privacy_accepted_at = now if privacy_version else None
    try:
        with connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users
                    (username, password_hash, privacy_accepted_at, privacy_version, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, password_hash, privacy_accepted_at, privacy_version, now),
            )
            return {
                "id": cursor.lastrowid,
                "username": username,
                "privacy_accepted_at": privacy_accepted_at,
                "privacy_version": privacy_version,
                "created_at": now,
            }
    except sqlite3.IntegrityError as exc:
        raise ValueError("Username already exists.") from exc


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT id, username, privacy_accepted_at, privacy_version, plan, created_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def create_space(
    user_id: int,
    name: str,
    description: str,
    icon: str,
    theme: str,
    template_id: str,
    system_prompt: str,
    monthly_token_budget: int,
) -> dict[str, Any]:
    space_id = str(uuid.uuid4())
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO spaces
                (id, user_id, name, description, icon, theme, template_id,
                 system_prompt, monthly_token_budget, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                space_id,
                user_id,
                name,
                description,
                icon,
                theme,
                template_id,
                system_prompt,
                monthly_token_budget,
                now,
                now,
            ),
        )
    return get_space(space_id, user_id) or {}


def list_spaces(user_id: int) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, name, description, icon, theme, template_id,
                   monthly_token_budget, created_at, updated_at
            FROM spaces
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_space(space_id: str, user_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT id, name, description, icon, theme, template_id,
                   system_prompt, monthly_token_budget, created_at, updated_at
            FROM spaces
            WHERE id = ? AND user_id = ?
            """,
            (space_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def count_spaces(user_id: int) -> int:
    with connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM spaces WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["count"])


def usage_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def token_usage(user_id: int, space_id: str | None = None) -> dict[str, int]:
    parameters: list[Any] = [user_id, usage_period()]
    # This ledger powers AI Space budgets. General chat usage is intentionally
    # excluded so monitoring cannot silently consume a Space subscription cap.
    space_filter = "AND space_id IS NOT NULL"
    if space_id:
        space_filter = "AND space_id = ?"
        parameters.append(space_id)
    with connect() as connection:
        row = connection.execute(
            f"""
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_usage
            WHERE user_id = ? AND period = ? {space_filter}
            """,
            parameters,
        ).fetchone()
    return {key: int(row[key]) for key in row.keys()}


def model_token_usage(user_id: int) -> dict[str, int]:
    """Return every recorded model token, including general chat and AI Space."""
    with connect() as connection:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_usage
            WHERE user_id = ? AND period = ?
            """,
            (user_id, usage_period()),
        ).fetchone()
    return {key: int(row[key]) for key in row.keys()}


def record_token_usage(
    user_id: int,
    space_id: str | None,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO token_usage
                (user_id, space_id, period, input_tokens, output_tokens, total_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                space_id,
                usage_period(),
                max(0, input_tokens),
                max(0, output_tokens),
                max(0, total_tokens),
                utc_now(),
            ),
        )
def record_api_usage_event(
    user_id: int | None,
    method: str,
    route: str,
    status_code: int,
    duration_ms: int,
) -> None:
    """Best-effort metrics use a short write budget, not business DB timeouts."""
    safe_route = route[:160] if route.startswith("/api/") else "/api/unknown"
    # The application initializes its WAL database at startup. Do not repeat
    # journal-mode changes here or inherit connect()'s 30-second busy timeout:
    # losing one metric during a scan is preferable to queuing a slow writer.
    is_postgres = getattr(settings, "database_backend", "sqlite") == "postgres"
    connection = (
        connect(timeout=0.25)
        if is_postgres
        else sqlite3.connect(settings.database_path, timeout=API_USAGE_SQLITE_TIMEOUT_SECONDS)
    )
    try:
        if is_postgres:
            connection.execute("SET LOCAL lock_timeout = '50ms'")
            connection.execute("SET LOCAL statement_timeout = '500ms'")
        else:
            connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            tracked_user_id = user_id
            if user_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if not exists:
                    tracked_user_id = None
            connection.execute(
                """
                INSERT INTO api_usage_events
                    (user_id, method, route, status_code, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    tracked_user_id,
                    method.upper()[:12],
                    safe_route,
                    max(100, min(int(status_code), 599)),
                    max(0, min(int(duration_ms), 3_600_000)),
                    utc_now(),
                ),
            )
            retention_cutoff = (
                datetime.now(timezone.utc) - timedelta(days=API_USAGE_RETENTION_DAYS)
            ).isoformat()
            connection.execute(
                "DELETE FROM api_usage_events WHERE created_at < ?",
                (retention_cutoff,),
            )
    finally:
        connection.close()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aggregate_admin_usage(
    hours: int = 24,
    bucket_minutes: int = 60,
) -> dict[str, Any]:
    """Return aggregate product usage without selecting user-generated content."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)
    live_start = now - timedelta(minutes=15)
    bucket_seconds = bucket_minutes * 60
    bucket_count = max(1, math.ceil((now - window_start).total_seconds() / bucket_seconds))
    buckets: list[dict[str, Any]] = []
    bucket_users: list[set[int]] = []
    bucket_sessions: list[set[str]] = []
    for index in range(bucket_count):
        start = window_start + timedelta(seconds=index * bucket_seconds)
        end = min(now, start + timedelta(seconds=bucket_seconds))
        buckets.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "registrations": 0,
                "active_users": 0,
                "sessions_created": 0,
                "active_sessions": 0,
                "messages": 0,
                "chat_calls": 0,
                "assistant_messages": 0,
                "space_calls": 0,
                "ai_requests": 0,
                "successful_space_calls": 0,
                "failed_space_calls": 0,
                "api_requests": 0,
                "api_errors": 0,
                "server_errors": 0,
                "average_latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "_latency_total": 0,
            }
        )
        bucket_users.append(set())
        bucket_sessions.append(set())

    def bucket_index(created_at: str) -> tuple[int, datetime] | None:
        parsed = _parse_timestamp(created_at)
        if parsed is None or parsed < window_start or parsed > now:
            return None
        index = min(
            bucket_count - 1,
            int((parsed - window_start).total_seconds() // bucket_seconds),
        )
        return index, parsed

    active_users: set[int] = set()
    active_sessions: set[str] = set()
    live_users: set[int] = set()
    live_sessions: set[str] = set()
    live = {
        "window_minutes": 15,
        "active_users": 0,
        "active_sessions": 0,
        "api_requests": 0,
        "chat_calls": 0,
        "space_calls": 0,
        "ai_requests": 0,
        "errors": 0,
    }

    with connect() as connection:
        totals_row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS registered_users,
                (SELECT COUNT(*) FROM sessions) AS sessions,
                (SELECT COUNT(*) FROM messages) AS messages,
                (SELECT COUNT(*) FROM documents) AS documents,
                (SELECT COUNT(*) FROM messages WHERE role = 'user') AS chat_calls,
                (SELECT COUNT(*) FROM space_runs) AS space_calls,
                ((SELECT COUNT(*) FROM messages WHERE role = 'user') +
                 (SELECT COUNT(*) FROM space_runs
                  WHERE execution_path IN ('lean', 'deep'))) AS ai_requests,
                (SELECT COUNT(*) FROM space_runs WHERE status = 'failed') AS failed_space_calls,
                (SELECT COUNT(*) FROM api_usage_events) AS api_requests,
                (SELECT COUNT(*) FROM api_usage_events WHERE status_code >= 400) AS api_errors,
                (SELECT COUNT(*) FROM api_usage_events WHERE status_code >= 500) AS server_errors
            """
        ).fetchone()
        token_row = connection.execute(
            """
            SELECT COUNT(*) AS usage_records,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM token_usage
            """
        ).fetchone()
        watch_error_row = connection.execute(
            "SELECT COUNT(*) AS count FROM recruitment_watches WHERE last_status = 'error'"
        ).fetchone()
        first_event_row = connection.execute(
            "SELECT MIN(created_at) AS created_at FROM api_usage_events"
        ).fetchone()
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        recent_24h_row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users WHERE created_at >= ?) AS registrations,
                (SELECT COUNT(*) FROM sessions WHERE updated_at >= ?) AS active_sessions,
                (SELECT COUNT(*) FROM messages WHERE created_at >= ?) AS messages,
                ((SELECT COUNT(*) FROM messages
                  WHERE role = 'user' AND created_at >= ?) +
                 (SELECT COUNT(*) FROM space_runs
                  WHERE execution_path IN ('lean', 'deep') AND created_at >= ?))
                    AS ai_requests
            """,
            (cutoff_24h, cutoff_24h, cutoff_24h, cutoff_24h, cutoff_24h),
        ).fetchone()
        active_users_24h_row = connection.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS count
            FROM (
                SELECT sessions.user_id AS user_id
                FROM messages
                JOIN sessions ON sessions.id = messages.session_id
                WHERE messages.created_at >= ?
                UNION
                SELECT user_id FROM sessions WHERE updated_at >= ?
                UNION
                SELECT user_id FROM space_runs WHERE created_at >= ?
                UNION
                SELECT user_id FROM api_usage_events
                WHERE user_id IS NOT NULL AND created_at >= ?
            ) AS active_user_sources
            """,
            (cutoff_24h, cutoff_24h, cutoff_24h, cutoff_24h),
        ).fetchone()

        users = connection.execute(
            "SELECT id, created_at FROM users WHERE created_at >= ?",
            (window_start.isoformat(),),
        ).fetchall()
        sessions = connection.execute(
            """
            SELECT id, user_id, created_at, updated_at
            FROM sessions
            WHERE created_at >= ? OR updated_at >= ?
            """,
            (window_start.isoformat(), window_start.isoformat()),
        ).fetchall()
        messages = connection.execute(
            """
            SELECT messages.session_id, sessions.user_id, messages.role,
                   messages.created_at
            FROM messages
            JOIN sessions ON sessions.id = messages.session_id
            WHERE messages.created_at >= ?
            """,
            (window_start.isoformat(),),
        ).fetchall()
        space_runs = connection.execute(
            """
            SELECT user_id, status, execution_path, created_at
            FROM space_runs
            WHERE created_at >= ?
            """,
            (window_start.isoformat(),),
        ).fetchall()
        token_records = connection.execute(
            """
            SELECT user_id, input_tokens, output_tokens, total_tokens, created_at
            FROM token_usage
            WHERE created_at >= ?
            """,
            (window_start.isoformat(),),
        ).fetchall()
        api_events = connection.execute(
            """
            SELECT user_id, status_code, duration_ms, created_at
            FROM api_usage_events
            WHERE created_at >= ?
            """,
            (window_start.isoformat(),),
        ).fetchall()

    for row in users:
        located = bucket_index(row["created_at"])
        if located:
            buckets[located[0]]["registrations"] += 1

    for row in sessions:
        created = bucket_index(row["created_at"])
        if created:
            buckets[created[0]]["sessions_created"] += 1
        updated = bucket_index(row["updated_at"])
        if updated:
            index, parsed = updated
            user_id = int(row["user_id"])
            session_id = str(row["id"])
            bucket_users[index].add(user_id)
            bucket_sessions[index].add(session_id)
            active_users.add(user_id)
            active_sessions.add(session_id)
            if parsed >= live_start:
                live_users.add(user_id)
                live_sessions.add(session_id)

    for row in messages:
        located = bucket_index(row["created_at"])
        if not located:
            continue
        index, parsed = located
        user_id = int(row["user_id"])
        session_id = str(row["session_id"])
        buckets[index]["messages"] += 1
        if row["role"] == "user":
            buckets[index]["chat_calls"] += 1
            buckets[index]["ai_requests"] += 1
            if parsed >= live_start:
                live["chat_calls"] += 1
                live["ai_requests"] += 1
        else:
            buckets[index]["assistant_messages"] += 1
        bucket_users[index].add(user_id)
        bucket_sessions[index].add(session_id)
        active_users.add(user_id)
        active_sessions.add(session_id)
        if parsed >= live_start:
            live_users.add(user_id)
            live_sessions.add(session_id)

    for row in space_runs:
        located = bucket_index(row["created_at"])
        if not located:
            continue
        index, parsed = located
        user_id = int(row["user_id"])
        buckets[index]["space_calls"] += 1
        status_value = str(row["status"])
        is_model_call = row["execution_path"] in {"lean", "deep"}
        if is_model_call:
            buckets[index]["ai_requests"] += 1
        if status_value == "failed":
            buckets[index]["failed_space_calls"] += 1
        elif status_value == "completed":
            buckets[index]["successful_space_calls"] += 1
        bucket_users[index].add(user_id)
        active_users.add(user_id)
        if parsed >= live_start:
            live_users.add(user_id)
            live["space_calls"] += 1
            if is_model_call:
                live["ai_requests"] += 1
            if status_value == "failed":
                live["errors"] += 1

    for row in token_records:
        located = bucket_index(row["created_at"])
        if not located:
            continue
        index, parsed = located
        user_id = int(row["user_id"])
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            buckets[index][key] += int(row[key] or 0)
        bucket_users[index].add(user_id)
        active_users.add(user_id)
        if parsed >= live_start:
            live_users.add(user_id)

    for row in api_events:
        located = bucket_index(row["created_at"])
        if not located:
            continue
        index, parsed = located
        status_code = int(row["status_code"])
        buckets[index]["api_requests"] += 1
        buckets[index]["_latency_total"] += int(row["duration_ms"] or 0)
        if status_code >= 400:
            buckets[index]["api_errors"] += 1
            if parsed >= live_start:
                live["errors"] += 1
        if status_code >= 500:
            buckets[index]["server_errors"] += 1
        if row["user_id"] is not None:
            user_id = int(row["user_id"])
            bucket_users[index].add(user_id)
            active_users.add(user_id)
            if parsed >= live_start:
                live_users.add(user_id)
        if parsed >= live_start:
            live["api_requests"] += 1

    for index, bucket in enumerate(buckets):
        bucket["active_users"] = len(bucket_users[index])
        bucket["active_sessions"] = len(bucket_sessions[index])
        if bucket["api_requests"]:
            bucket["average_latency_ms"] = round(
                bucket["_latency_total"] / bucket["api_requests"]
            )
        bucket.pop("_latency_total", None)

    live["active_users"] = len(live_users)
    live["active_sessions"] = len(live_sessions)
    recent = {
        "registrations": sum(item["registrations"] for item in buckets),
        "active_users": len(active_users),
        "sessions_created": sum(item["sessions_created"] for item in buckets),
        "active_sessions": len(active_sessions),
        "messages": sum(item["messages"] for item in buckets),
        "chat_calls": sum(item["chat_calls"] for item in buckets),
        "space_calls": sum(item["space_calls"] for item in buckets),
        "ai_requests": sum(item["ai_requests"] for item in buckets),
        "api_requests": sum(item["api_requests"] for item in buckets),
        "api_errors": sum(item["api_errors"] for item in buckets),
        "server_errors": sum(item["server_errors"] for item in buckets),
        "input_tokens": sum(item["input_tokens"] for item in buckets),
        "output_tokens": sum(item["output_tokens"] for item in buckets),
        "total_tokens": sum(item["total_tokens"] for item in buckets),
    }
    totals = {key: int(totals_row[key] or 0) for key in totals_row.keys()}
    totals["users"] = totals["registered_users"]
    totals["active_users_24h"] = int(active_users_24h_row["count"] or 0)
    totals["active_sessions_24h"] = int(recent_24h_row["active_sessions"] or 0)
    totals["input_tokens"] = int(token_row["input_tokens"] or 0)
    totals["output_tokens"] = int(token_row["output_tokens"] or 0)
    totals["total_tokens"] = int(token_row["total_tokens"] or 0)
    totals["token_usage"] = {
        key: int(token_row[key] or 0)
        for key in ("usage_records", "input_tokens", "output_tokens", "total_tokens")
    }
    recent.update(
        {
            "registrations_24h": int(recent_24h_row["registrations"] or 0),
            "messages_24h": int(recent_24h_row["messages"] or 0),
            "ai_requests_24h": int(recent_24h_row["ai_requests"] or 0),
        }
    )
    series = [
        {
            "date": item["start"],
            "active_users": item["active_users"],
            "messages": item["messages"],
            "ai_requests": item["ai_requests"],
            "tokens": item["total_tokens"],
        }
        for item in buckets
    ]
    return {
        "generated_at": now.isoformat(),
        "window": {
            "hours": hours,
            "bucket_minutes": bucket_minutes,
            "start": window_start.isoformat(),
            "end": now.isoformat(),
        },
        "totals": totals,
        "recent": recent,
        "live": live,
        "buckets": buckets,
        "series": series,
        "errors": {
            "api_errors": totals["api_errors"],
            "server_errors": totals["server_errors"],
            "failed_space_calls": totals["failed_space_calls"],
            "recruitment_watches_currently_failing": int(watch_error_row["count"] or 0),
        },
        "data_coverage": {
            "api_requests_since": first_event_row["created_at"],
            "ai_request_scope": (
                "User chat prompts plus AI Space lean/deep model executions."
            ),
            "token_scope": (
                "Recorded AI Space calls and chat calls whose OpenAI response "
                "included usage metadata."
            ),
            "chat_token_usage_available": True,
            "latency_scope": (
                "HTTP response creation time; streaming duration is not included."
            ),
            "privacy": (
                "Aggregate counts and request metadata only; message, document, "
                "password, prompt and response content are never returned."
            ),
            "api_event_retention_days": API_USAGE_RETENTION_DAYS,
            "api_error_scope": (
                "Errors emitted after an SSE response has started are not reflected "
                "in HTTP status aggregates."
            ),
        },
    }


def _public_space_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    try:
        item["artifact"] = json.loads(item.get("artifact") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["artifact"] = {}
    item["message"] = item.pop("input_text", "")
    item.pop("user_id", None)
    item["usage"] = {
        "input_tokens": int(item.pop("actual_input_tokens", 0) or 0),
        "output_tokens": int(item.pop("actual_output_tokens", 0) or 0),
        "total_tokens": int(item.pop("actual_total_tokens", 0) or 0),
    }
    return item


def create_space_run(
    user_id: int,
    space_id: str,
    fingerprint: str,
    requested_mode: str,
    execution_path: str,
    input_text: str,
    artifact: dict[str, Any],
    reply: str,
    estimated_input_tokens: int = 0,
    max_output_tokens: int = 0,
    actual_input_tokens: int = 0,
    actual_output_tokens: int = 0,
    actual_total_tokens: int = 0,
    saved_tokens: int = 0,
    cached_from_run_id: str | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    now = utc_now()
    completed_at = now if status in {"completed", "failed"} else None
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO space_runs
                (id, space_id, user_id, fingerprint, requested_mode,
                 execution_path, status, input_text, artifact, reply,
                 estimated_input_tokens, max_output_tokens,
                 actual_input_tokens, actual_output_tokens, actual_total_tokens,
                 saved_tokens, cached_from_run_id, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                space_id,
                user_id,
                fingerprint,
                requested_mode,
                execution_path,
                status,
                input_text,
                json.dumps(artifact, ensure_ascii=False),
                reply,
                max(0, estimated_input_tokens),
                max(0, max_output_tokens),
                max(0, actual_input_tokens),
                max(0, actual_output_tokens),
                max(0, actual_total_tokens),
                max(0, saved_tokens),
                cached_from_run_id,
                now,
                completed_at,
            ),
        )
        connection.execute(
            """
            DELETE FROM space_runs
            WHERE space_id = ? AND user_id = ?
              AND id NOT IN (
                  SELECT id FROM space_runs
                  WHERE space_id = ? AND user_id = ?
                  ORDER BY created_at DESC
                  LIMIT ?
              )
            """,
            (
                space_id,
                user_id,
                space_id,
                user_id,
                SPACE_RUN_HISTORY_LIMIT,
            ),
        )
    return get_space_run(run_id, space_id, user_id) or {}


def get_space_run(
    run_id: str,
    space_id: str,
    user_id: int,
) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM space_runs
            WHERE id = ? AND space_id = ? AND user_id = ?
            """,
            (run_id, space_id, user_id),
        ).fetchone()
    return _public_space_run(row)


def find_cached_space_run(
    space_id: str,
    user_id: int,
    fingerprint: str,
) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM space_runs
            WHERE space_id = ? AND user_id = ? AND fingerprint = ?
              AND status = 'completed' AND execution_path IN ('lean', 'deep')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (space_id, user_id, fingerprint),
        ).fetchone()
    return _public_space_run(row)


def list_space_runs(
    space_id: str,
    user_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 100))
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM space_runs
            WHERE space_id = ? AND user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (space_id, user_id, safe_limit),
        ).fetchall()
    return [item for row in rows if (item := _public_space_run(row))]


def record_privacy_consent(user_id: int, privacy_version: str) -> dict[str, Any] | None:
    accepted_at = utc_now()
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE users
            SET privacy_accepted_at = ?, privacy_version = ?
            WHERE id = ?
            """,
            (accepted_at, privacy_version, user_id),
        )
    if cursor.rowcount == 0:
        return None
    return get_user_by_id(user_id)


def delete_user(user_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cursor.rowcount > 0


def get_recruitment_profile(user_id: int) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM recruitment_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return {
            "user_id": user_id,
            "desired_roles": [], "industries": [], "locations": [],
            "employer_types": [], "background": "", "graduation_year": None,
            "education_level": "", "major_category": "", "school_tier": "",
            "experience_level": "", "skill_tags": [], "language_level": "",
            "undergraduate_major": "", "undergraduate_school_tier": "",
            "master_major": "", "master_school_tier": "", "composite_interest": False,
            "undergraduate_school": "", "master_school": "",
            "availability_start": None, "availability_end": None,
        }
    item = dict(row)
    for key in ("desired_roles", "industries", "locations", "employer_types", "skill_tags"):
        try:
            item[key] = json.loads(item[key])
        except (TypeError, json.JSONDecodeError):
            item[key] = []
    return item


def save_recruitment_profile(user_id: int, profile: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    values = (
        user_id,
        json.dumps(profile.get("desired_roles", []), ensure_ascii=False),
        json.dumps(profile.get("industries", []), ensure_ascii=False),
        json.dumps(profile.get("locations", []), ensure_ascii=False),
        json.dumps(profile.get("employer_types", []), ensure_ascii=False),
        profile.get("background", ""), profile.get("education_level", ""),
        profile.get("major_category", ""), profile.get("school_tier", ""),
        profile.get("experience_level", ""),
        json.dumps(profile.get("skill_tags", []), ensure_ascii=False),
        profile.get("language_level", ""), profile.get("graduation_year"),
        profile.get("undergraduate_major", ""), profile.get("undergraduate_school_tier", ""),
        profile.get("master_major", ""), profile.get("master_school_tier", ""),
        profile.get("undergraduate_school", ""), profile.get("master_school", ""),
        int(bool(profile.get("composite_interest", False))),
        profile.get("availability_start"), profile.get("availability_end"), now,
    )
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO recruitment_profiles
                (user_id, desired_roles, industries, locations, employer_types,
                 background, education_level, major_category, school_tier,
                 experience_level, skill_tags, language_level, graduation_year,
                 undergraduate_major, undergraduate_school_tier, master_major,
                 master_school_tier, undergraduate_school, master_school,
                 composite_interest, availability_start, availability_end, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                desired_roles=excluded.desired_roles, industries=excluded.industries,
                locations=excluded.locations, employer_types=excluded.employer_types,
                background=excluded.background, graduation_year=excluded.graduation_year,
                education_level=excluded.education_level, major_category=excluded.major_category,
                school_tier=excluded.school_tier, experience_level=excluded.experience_level,
                skill_tags=excluded.skill_tags, language_level=excluded.language_level,
                undergraduate_major=excluded.undergraduate_major,
                undergraduate_school_tier=excluded.undergraduate_school_tier,
                master_major=excluded.master_major, master_school_tier=excluded.master_school_tier,
                composite_interest=excluded.composite_interest,
                undergraduate_school=excluded.undergraduate_school,
                master_school=excluded.master_school,
                availability_start=excluded.availability_start,
                availability_end=excluded.availability_end, updated_at=excluded.updated_at
            """,
            values,
        )
    return get_recruitment_profile(user_id)


def _public_recruitment_watch(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("keywords", "last_keyword_hits"):
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except (TypeError, json.JSONDecodeError):
            item[key] = []
    item["enabled"] = bool(item.get("enabled"))
    item["change_pending"] = bool(item.get("change_pending"))
    item.pop("user_id", None)
    item.pop("last_fingerprint", None)
    item.pop("fetch_url", None)
    if item.get("watch_type") == "company":
        item["url"] = ""
    return item


def create_recruitment_watch(
    user_id: int,
    name: str,
    url: str,
    fetch_url: str,
    keywords: list[str],
    *,
    watch_type: str = "page",
    company_name: str = "",
) -> dict[str, Any]:
    watch_id = str(uuid.uuid4())
    now = utc_now()
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM recruitment_watches WHERE user_id = ?",
                (user_id,),
            ).fetchone()["count"]
            if int(count) >= 12:
                raise ValueError("每个账号最多创建 12 个企业/官网动态监控。")
            connection.execute(
                """
                INSERT INTO recruitment_watches
                    (id, user_id, name, url, fetch_url, keywords, watch_type, company_name,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    watch_id,
                    user_id,
                    name,
                    url,
                    fetch_url,
                    json.dumps(keywords, ensure_ascii=False),
                    watch_type,
                    company_name,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该监控网址已经存在。") from exc
    watch = get_recruitment_watch(user_id, watch_id)
    if not watch:
        raise RuntimeError("Recruitment watch was not created.")
    return watch


def list_recruitment_watches(user_id: int) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM recruitment_watches
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [_public_recruitment_watch(row) for row in rows]


def list_enabled_recruitment_watches(
    *,
    due_before: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return internal watch rows for the in-process scheduler."""
    parameters: list[Any] = []
    due_filter = ""
    if due_before:
        due_filter = "AND (last_checked_at IS NULL OR last_checked_at <= ?)"
        parameters.append(due_before)
    parameters.append(max(1, min(int(limit), 500)))
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM recruitment_watches
            WHERE enabled = 1
            {due_filter}
            ORDER BY COALESCE(last_checked_at, created_at), created_at
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["keywords"] = json.loads(item.get("keywords") or "[]")
        except (TypeError, json.JSONDecodeError):
            item["keywords"] = []
        result.append(item)
    return result


def get_recruitment_watch(user_id: int, watch_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM recruitment_watches WHERE id = ? AND user_id = ?",
            (watch_id, user_id),
        ).fetchone()
    return _public_recruitment_watch(row) if row else None


def delete_recruitment_watch(user_id: int, watch_id: str) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM recruitment_watches WHERE id = ? AND user_id = ?",
            (watch_id, user_id),
        )
    return cursor.rowcount > 0


def record_recruitment_watch_success(
    user_id: int,
    watch_id: str,
    fingerprint: str,
    hits: list[str],
    http_status: int,
) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT last_fingerprint, last_changed_at, change_pending, change_version
            FROM recruitment_watches
            WHERE id = ? AND user_id = ?
            """,
            (watch_id, user_id),
        ).fetchone()
        if not row:
            return None
        previous = row["last_fingerprint"]
        if previous is None:
            result_status = "baseline"
        elif previous == fingerprint:
            result_status = "unchanged"
        else:
            result_status = "changed"
        changed_at = now if result_status == "changed" else row["last_changed_at"]
        change_pending = 1 if result_status == "changed" else int(row["change_pending"] or 0)
        change_version = int(row["change_version"] or 0)
        if result_status == "changed":
            change_version += 1
        connection.execute(
            """
            UPDATE recruitment_watches
            SET last_fingerprint = ?, last_status = ?, last_http_status = ?,
                last_keyword_hits = ?, last_error = NULL, last_checked_at = ?,
                last_changed_at = ?, change_pending = ?, change_version = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                fingerprint,
                result_status,
                http_status,
                json.dumps(hits, ensure_ascii=False),
                now,
                changed_at,
                change_pending,
                change_version,
                now,
                watch_id,
                user_id,
            ),
        )
    return get_recruitment_watch(user_id, watch_id)


def record_recruitment_watch_error(
    user_id: int,
    watch_id: str,
    error: str,
) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE recruitment_watches
            SET last_status = 'error', last_http_status = NULL, last_error = ?,
                last_checked_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (error[:300], now, now, watch_id, user_id),
        )
    if cursor.rowcount == 0:
        return None
    return get_recruitment_watch(user_id, watch_id)


def acknowledge_recruitment_watch_change(
    user_id: int,
    watch_id: str,
    expected_version: int,
) -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        row = connection.execute(
            "SELECT change_pending, change_version FROM recruitment_watches "
            "WHERE id = ? AND user_id = ?",
            (watch_id, user_id),
        ).fetchone()
        if not row:
            return None
        if int(row["change_version"] or 0) != expected_version:
            raise ValueError("官网在你打开提醒后又发生了变化，请先查看最新版本。")
        if not bool(row["change_pending"]):
            return get_recruitment_watch(user_id, watch_id)
        cursor = connection.execute(
            """
            UPDATE recruitment_watches
            SET change_pending = 0, change_acknowledged_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND change_version = ?
            """,
            (now, now, watch_id, user_id, expected_version),
        )
    if cursor.rowcount == 0:
        return None
    return get_recruitment_watch(user_id, watch_id)


def recruitment_watch_summary(user_id: int) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                SUM(CASE WHEN change_pending = 1 THEN 1 ELSE 0 END) AS changed,
                SUM(CASE WHEN last_status = 'error' THEN 1 ELSE 0 END) AS errors,
                MAX(last_checked_at) AS last_checked_at,
                MAX(last_changed_at) AS last_changed_at
            FROM recruitment_watches
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "enabled": int(row["enabled"] or 0),
        "changed": int(row["changed"] or 0),
        "errors": int(row["errors"] or 0),
        "last_checked_at": row["last_checked_at"],
        "last_changed_at": row["last_changed_at"],
    }


def recruitment_ingest_source_key(source_id: str, source_thread_id: str | None) -> str:
    """Return the stable identity used for one external monitoring source."""
    return f"{source_id.strip()}::{(source_thread_id or '').strip()}"


def ensure_recruitment_ingest_sources(sources: list[dict[str, Any]]) -> None:
    """Register expected sources without making an uncontacted source look synced."""
    now = utc_now()
    with connect() as connection:
        for source in sources:
            source_id = str(source["source_id"]).strip()
            source_thread_id = str(source.get("source_thread_id") or "").strip() or None
            source_key = recruitment_ingest_source_key(source_id, source_thread_id)
            connection.execute(
                """
                INSERT INTO recruitment_ingest_sources
                    (source_key, source_id, source_thread_id, title, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    title=excluded.title
                """,
                (
                    source_key,
                    source_id,
                    source_thread_id,
                    str(source.get("title") or source_id).strip()[:120],
                    now,
                    now,
                ),
            )


def _decode_recruitment_ingest_candidate(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for field in ("tags", "evidence"):
        try:
            item[field] = json.loads(item.get(field) or "[]")
        except (TypeError, json.JSONDecodeError):
            item[field] = []
    return item


def _source_timestamp_is_older(incoming: str | None, current: str | None) -> bool:
    if not incoming or not current:
        return False
    try:
        incoming_value = datetime.fromisoformat(incoming.replace("Z", "+00:00"))
        current_value = datetime.fromisoformat(current.replace("Z", "+00:00"))
        if incoming_value.tzinfo is None:
            incoming_value = incoming_value.replace(tzinfo=timezone.utc)
        if current_value.tzinfo is None:
            current_value = current_value.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return incoming_value < current_value


def _update_existing_recruitment_ingest_candidate(
    connection: sqlite3.Connection,
    existing: sqlite3.Row,
    candidate: dict[str, Any],
    now: str,
) -> str:
    if _source_timestamp_is_older(
        candidate.get("source_updated_at"), existing["source_updated_at"]
    ):
        connection.execute(
            "UPDATE recruitment_ingest_candidates SET last_seen_at = ? WHERE id = ?",
            (now, existing["id"]),
        )
        return "stale"
    if existing["payload_hash"] == candidate["payload_hash"]:
        connection.execute(
            """
            UPDATE recruitment_ingest_candidates
            SET last_seen_at = ?, source_updated_at = COALESCE(?, source_updated_at),
                source_item_id = COALESCE(?, source_item_id)
            WHERE id = ?
            """,
            (
                now,
                candidate.get("source_updated_at"),
                candidate.get("source_item_id"),
                existing["id"],
            ),
        )
        return "duplicate"
    connection.execute(
        """
        UPDATE recruitment_ingest_candidates
        SET source_key=?, source_id=?, source_thread_id=?, source_item_id=?,
            external_id=?, source_updated_at=?, company=?, employer_type=?, title=?,
            city=?, industry=?, official_url=?, canonical_url=?, source=?,
            opening_date=?, closing_date=?, requirements=?, tags=?, evidence=?,
            incoming_status=?, payload_hash=?, verification_status='pending',
            verification_reason=NULL, rejected_at=NULL, last_seen_at=?
        WHERE id=?
        """,
        (
            candidate["source_key"], candidate["source_id"],
            candidate.get("source_thread_id"), candidate.get("source_item_id"),
            candidate.get("external_id"), candidate.get("source_updated_at"),
            candidate["company"], candidate["employer_type"], candidate["title"],
            candidate["city"], candidate.get("industry", ""),
            candidate["official_url"], candidate["canonical_url"], candidate["source"],
            candidate.get("opening_date"), candidate.get("closing_date"),
            candidate.get("requirements", ""),
            json.dumps(candidate.get("tags", []), ensure_ascii=False),
            json.dumps(candidate.get("evidence", []), ensure_ascii=False),
            candidate.get("incoming_status", "open"), candidate["payload_hash"],
            now, existing["id"],
        ),
    )
    return "updated"


def upsert_recruitment_ingest_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Persist an isolated candidate and recover safely from concurrent inserts."""
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM recruitment_ingest_candidates WHERE dedupe_key = ?",
            (candidate["dedupe_key"],),
        ).fetchone()
        if existing is None:
            # PostgreSQL marks the transaction failed after a constraint error.
            # Keep the existing concurrent-insert recovery isolated so its
            # follow-up SELECT can run on both supported databases.
            connection.execute("SAVEPOINT ingest_candidate_insert")
            try:
                connection.execute(
                    """
                    INSERT INTO recruitment_ingest_candidates
                        (id, dedupe_key, source_key, source_id, source_thread_id,
                         source_item_id, external_id, source_updated_at, company,
                         employer_type, title, city, industry, official_url,
                         canonical_url, source, opening_date, closing_date,
                         requirements, tags, evidence, incoming_status, payload_hash,
                         verification_status, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'pending', ?, ?)
                    """,
                    (
                        candidate["id"], candidate["dedupe_key"], candidate["source_key"],
                        candidate["source_id"], candidate.get("source_thread_id"),
                        candidate.get("source_item_id"), candidate.get("external_id"),
                        candidate.get("source_updated_at"), candidate["company"],
                        candidate["employer_type"], candidate["title"], candidate["city"],
                        candidate.get("industry", ""), candidate["official_url"],
                        candidate["canonical_url"], candidate["source"],
                        candidate.get("opening_date"), candidate.get("closing_date"),
                        candidate.get("requirements", ""),
                        json.dumps(candidate.get("tags", []), ensure_ascii=False),
                        json.dumps(candidate.get("evidence", []), ensure_ascii=False),
                        candidate.get("incoming_status", "open"), candidate["payload_hash"],
                        now, now,
                    ),
                )
                disposition = "new"
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK TO SAVEPOINT ingest_candidate_insert")
                connection.execute("RELEASE SAVEPOINT ingest_candidate_insert")
                existing = connection.execute(
                    "SELECT * FROM recruitment_ingest_candidates WHERE dedupe_key = ?",
                    (candidate["dedupe_key"],),
                ).fetchone()
                if existing is None:
                    raise
                disposition = _update_existing_recruitment_ingest_candidate(
                    connection, existing, candidate, now
                )
            else:
                connection.execute("RELEASE SAVEPOINT ingest_candidate_insert")
        else:
            disposition = _update_existing_recruitment_ingest_candidate(
                connection, existing, candidate, now
            )
        row = connection.execute(
            "SELECT * FROM recruitment_ingest_candidates WHERE dedupe_key = ?",
            (candidate["dedupe_key"],),
        ).fetchone()
    result = _decode_recruitment_ingest_candidate(row)
    result["disposition"] = disposition
    return result


def set_recruitment_ingest_candidate_verification(
    candidate_id: str,
    verification_status: str,
    reason: str | None,
    promoted_job_id: str | None = None,
    verified_opening_date: str | None = None,
    verified_closing_date: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    verified_at = now if verification_status == "verified" else None
    rejected_at = now if verification_status == "rejected" else None
    with connect() as connection:
        connection.execute(
            """
            UPDATE recruitment_ingest_candidates
            SET verification_status=?, verification_reason=?,
                promoted_job_id=COALESCE(?, promoted_job_id),
                verified_at=COALESCE(?, verified_at),
                rejected_at=?,
                verified_opening_date=CASE WHEN ? = 'verified' THEN ?
                                           ELSE verified_opening_date END,
                verified_closing_date=CASE WHEN ? = 'verified' THEN ?
                                           ELSE verified_closing_date END,
                last_seen_at=last_seen_at
            WHERE id=?
            """,
            (
                verification_status,
                reason,
                promoted_job_id,
                verified_at,
                rejected_at,
                verification_status,
                verified_opening_date,
                verification_status,
                verified_closing_date,
                candidate_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM recruitment_ingest_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    return _decode_recruitment_ingest_candidate(row) if row else None


def record_recruitment_ingest_event(
    *,
    source_id: str,
    source_thread_id: str | None,
    title: str,
    counts: dict[str, int],
    last_item_id: str | None,
    last_source_updated_at: str | None,
) -> str:
    now = utc_now()
    event_id = str(uuid.uuid4())
    source_key = recruitment_ingest_source_key(source_id, source_thread_id)
    received = int(counts.get("received", 0))
    rejected = int(counts.get("rejected", 0))
    pending = int(counts.get("pending", 0))
    event_status = (
        "error" if received > 0 and rejected == received
        else "partial" if rejected or pending
        else "synced"
    )
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO recruitment_ingest_sources
                (source_key, source_id, source_thread_id, title, status, last_seen_at,
                 last_source_updated_at, last_item_id, last_event_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                title=excluded.title, status=excluded.status,
                last_seen_at=excluded.last_seen_at,
                last_source_updated_at=CASE
                    WHEN excluded.last_source_updated_at IS NULL
                        THEN recruitment_ingest_sources.last_source_updated_at
                    WHEN recruitment_ingest_sources.last_source_updated_at IS NULL
                        THEN excluded.last_source_updated_at
                    WHEN julianday(excluded.last_source_updated_at) >=
                         julianday(recruitment_ingest_sources.last_source_updated_at)
                        THEN excluded.last_source_updated_at
                    ELSE recruitment_ingest_sources.last_source_updated_at
                END,
                last_item_id=COALESCE(excluded.last_item_id,
                                      recruitment_ingest_sources.last_item_id),
                last_event_id=excluded.last_event_id, updated_at=excluded.updated_at
            """,
            (
                source_key, source_id, source_thread_id, title[:120], event_status, now,
                last_source_updated_at, last_item_id, event_id, now, now,
            ),
        )
        connection.execute(
            """
            INSERT INTO recruitment_ingest_events
                (id, source_key, source_id, source_thread_id, received, accepted,
                 new_count, updated_count, duplicate_count, stale_count, pending_count,
                 rejected_count, closed_count, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, source_key, source_id, source_thread_id, received,
                int(counts.get("accepted", 0)), int(counts.get("new", 0)),
                int(counts.get("updated", 0)), int(counts.get("duplicates", 0)),
                int(counts.get("stale", 0)), pending, rejected,
                int(counts.get("closed", 0)), event_status, now,
            ),
        )
    return event_id


def recruitment_sync_status(*, expected_source_count: int = 0) -> dict[str, Any]:
    """Return aggregate source state without exposing the shared ingest token."""
    with connect() as connection:
        source_rows = connection.execute(
            """
            SELECT sources.*,
                   events.accepted AS latest_accepted,
                   events.pending_count AS latest_pending,
                   events.rejected_count AS latest_rejected,
                   events.closed_count AS latest_closed
            FROM recruitment_ingest_sources AS sources
            LEFT JOIN recruitment_ingest_events AS events
              ON events.id = sources.last_event_id
            ORDER BY sources.source_thread_id IS NULL,
                     sources.created_at,
                     sources.source_thread_id
            """
        ).fetchall()
        candidate_rows = connection.execute(
            """
            SELECT source_key,
                   SUM(CASE WHEN verification_status = 'verified' THEN 1 ELSE 0 END) accepted,
                   SUM(CASE WHEN verification_status = 'pending' THEN 1 ELSE 0 END) pending,
                   SUM(CASE WHEN verification_status = 'rejected' THEN 1 ELSE 0 END) rejected
            FROM recruitment_ingest_candidates
            GROUP BY source_key
            """
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT id, source_id, source_thread_id, received, accepted,
                   new_count, updated_count, duplicate_count, stale_count, pending_count,
                   rejected_count, closed_count, status, created_at
            FROM recruitment_ingest_events
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()
    counts_by_source = {
        row["source_key"]: {
            "accepted": int(row["accepted"] or 0),
            "pending": int(row["pending"] or 0),
            "rejected": int(row["rejected"] or 0),
        }
        for row in candidate_rows
    }
    sources: list[dict[str, Any]] = []
    for row in source_rows:
        counts = counts_by_source.get(
            row["source_key"], {"accepted": 0, "pending": 0, "rejected": 0}
        )
        latest_counts = {
            "accepted": int(row["latest_accepted"] or 0),
            "pending": int(row["latest_pending"] or 0),
            "rejected": int(row["latest_rejected"] or 0),
            "closed": int(row["latest_closed"] or 0),
        }
        sources.append({
            "source_id": row["source_id"],
            "source_ref": row["source_thread_id"],
            "title": row["title"],
            "status": row["status"],
            "last_seen_at": row["last_seen_at"],
            "last_source_updated_at": row["last_source_updated_at"],
            "last_item_id": row["last_item_id"],
            **latest_counts,
            "latest_accepted": latest_counts["accepted"],
            "latest_pending": latest_counts["pending"],
            "latest_rejected": latest_counts["rejected"],
            "latest_closed": latest_counts["closed"],
            "inventory_accepted": counts["accepted"],
            "inventory_pending": counts["pending"],
            "inventory_rejected": counts["rejected"],
        })
    last_synced_at = max(
        (source["last_seen_at"] for source in sources if source["last_seen_at"]),
        default=None,
    )
    return {
        "source_count": len(sources),
        "expected_source_count": expected_source_count,
        "connected_source_count": sum(source["last_seen_at"] is not None for source in sources),
        "last_synced_at": last_synced_at,
        "accepted": sum(source["latest_accepted"] for source in sources),
        "pending": sum(source["latest_pending"] for source in sources),
        "rejected": sum(source["latest_rejected"] for source in sources),
        "inventory_accepted": sum(source["inventory_accepted"] for source in sources),
        "inventory_pending": sum(source["inventory_pending"] for source in sources),
        "inventory_rejected": sum(source["inventory_rejected"] for source in sources),
        "sources": sources,
        "recent_events": [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key != "source_thread_id"
                },
                "source_ref": row["source_thread_id"],
            }
            for row in event_rows
        ],
    }


def recruitment_job_summary() -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'open'
                          AND (closing_date IS NULL OR closing_date >= DATE('now'))
                         THEN 1 ELSE 0 END) AS open_jobs,
                MAX(last_verified_at) AS last_verified_at
            FROM recruitment_jobs
            """
        ).fetchone()
    return {
        "open_jobs": int(row["open_jobs"] or 0),
        "last_verified_at": row["last_verified_at"],
    }


def get_system_state(key: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT value, updated_at FROM system_state WHERE key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        value = {"value": row["value"]}
    if not isinstance(value, dict):
        value = {"value": value}
    value["updated_at"] = row["updated_at"]
    return value


def set_system_state(key: str, value: dict[str, Any]) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), now),
        )


def list_recruitment_jobs() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM recruitment_jobs
            WHERE status = 'open'
              AND (closing_date IS NULL OR closing_date > DATE('now'))
              AND url LIKE 'https://%'
            ORDER BY closing_date IS NULL, closing_date
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item["tags"])
        except (TypeError, json.JSONDecodeError):
            item["tags"] = []
        result.append(item)
    return result


def purge_legacy_recruitment_samples() -> None:
    """Remove prototype vacancies and known unsupported role claims."""
    with connect() as connection:
        connection.execute(
            """
            DELETE FROM recruitment_jobs
            WHERE id IN (
                'sample-byteplus-product-2026',
                'sample-hsbc-analyst-2026',
                'sample-state-tech-2026',
                'curated-pdd-2027-early',
                'curated-kearney-2027-ba'
            )
               OR source IN ('示例岗位，等待接入官方源', '示例数据')
               OR company LIKE '九坤%'
               OR source LIKE '九坤%'
               OR (
                    source = 'OpenAI 网页搜索'
                    AND (
                        tags NOT LIKE '%链接已验证%'
                        OR tags LIKE '%待官方核验%'
                        OR tags LIKE '%待打开核对%'
                    )
               )
               OR (
                    source = 'OpenAI 网页搜索'
                    AND company IN ('中国人民银行', '人行', '中国农业发展银行', '农发行')
                    AND (title LIKE '%管培%' OR title LIKE '%管理培训生%')
                    AND tags NOT LIKE '%标题已验证%'
               )
            """
        )
        review_rows = connection.execute(
            """
            SELECT id, tags
            FROM recruitment_jobs
            WHERE source = 'OpenAI 网页搜索'
              AND tags NOT LIKE '%标题已验证%'
            """
        ).fetchall()
        for row in review_rows:
            try:
                tags = json.loads(row["tags"] or "[]")
            except (TypeError, json.JSONDecodeError):
                tags = []
            if "待官方核验" not in tags:
                tags.append("待官方核验")
            connection.execute(
                "UPDATE recruitment_jobs SET tags = ? WHERE id = ?",
                (json.dumps(tags, ensure_ascii=False), row["id"]),
            )


def close_recruitment_job(job_id: str) -> None:
    with connect() as connection:
        connection.execute(
            "UPDATE recruitment_jobs SET status = 'closed', last_verified_at = ? WHERE id = ?",
            (utc_now(), job_id),
        )


def seed_recruitment_jobs(jobs: list[dict[str, Any]]) -> None:
    with connect() as connection:
        for job in jobs:
            connection.execute(
                """
                INSERT OR IGNORE INTO recruitment_jobs
                    (id, company, employer_type, title, city, industry, url, source,
                     opening_date, closing_date, requirements, tags, historical_applicants,
                     historical_offers, last_verified_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"], job["company"], job["employer_type"], job["title"],
                    job.get("city", ""), job.get("industry", ""), job.get("url", ""),
                    job.get("source", "示例数据"), job.get("opening_date"),
                    job.get("closing_date"), job.get("requirements", ""),
                    json.dumps(job.get("tags", []), ensure_ascii=False),
                    job.get("historical_applicants"), job.get("historical_offers"),
                    job.get("last_verified_at", utc_now()), job.get("status", "open"),
                ),
            )


def upsert_recruitment_jobs(jobs: list[dict[str, Any]]) -> None:
    with connect() as connection:
        for job in jobs:
            connection.execute(
                """
                INSERT INTO recruitment_jobs
                    (id, company, employer_type, title, city, industry, url, source,
                     opening_date, closing_date, requirements, tags, historical_applicants,
                     historical_offers, last_verified_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    company=excluded.company, employer_type=excluded.employer_type,
                    title=excluded.title, city=excluded.city, industry=excluded.industry,
                    url=excluded.url, source=excluded.source,
                    opening_date=excluded.opening_date, closing_date=excluded.closing_date,
                    requirements=excluded.requirements, tags=excluded.tags,
                    last_verified_at=excluded.last_verified_at, status=excluded.status
                """,
                (
                    job["id"], job["company"], job.get("employer_type", "公开岗位源"),
                    job["title"], job.get("city", ""), job.get("industry", ""),
                    job.get("url", ""), job.get("source", ""), job.get("opening_date"),
                    job.get("closing_date"), job.get("requirements", ""),
                    json.dumps(job.get("tags", []), ensure_ascii=False),
                    job.get("historical_applicants"), job.get("historical_offers"),
                    job.get("last_verified_at", utc_now()), job.get("status", "open"),
                ),
            )


def replace_recruitment_source_jobs(source: str, jobs: list[dict[str, Any]]) -> None:
    """Atomically replace one crawler snapshot while retaining closed rows for audit."""
    if not jobs:
        return
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE recruitment_jobs SET status = 'closed', last_verified_at = ? WHERE source = ?",
            (now, source),
        )
        for job in jobs:
            connection.execute(
                """
                INSERT INTO recruitment_jobs
                    (id, company, employer_type, title, city, industry, url, source,
                     opening_date, closing_date, requirements, tags, historical_applicants,
                     historical_offers, last_verified_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    company=excluded.company, employer_type=excluded.employer_type,
                    title=excluded.title, city=excluded.city, industry=excluded.industry,
                    url=excluded.url, source=excluded.source,
                    opening_date=excluded.opening_date, closing_date=excluded.closing_date,
                    requirements=excluded.requirements, tags=excluded.tags,
                    last_verified_at=excluded.last_verified_at, status=excluded.status
                """,
                (
                    job["id"], job["company"], job.get("employer_type", "重点雇主"),
                    job["title"], job.get("city", ""), job.get("industry", ""),
                    job.get("url", ""), source, job.get("opening_date"),
                    job.get("closing_date"), job.get("requirements", ""),
                    json.dumps(job.get("tags", []), ensure_ascii=False),
                    job.get("historical_applicants"), job.get("historical_offers"),
                    job.get("last_verified_at", now), job.get("status", "open"),
                ),
            )


def create_session(
    user_id: int,
    title: str = "New conversation",
    workspace: str = DEFAULT_WORKSPACE,
) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    now = utc_now()
    workspace = validate_workspace(workspace)
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO sessions
                (id, user_id, title, workspace, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_id, title, workspace, now, now),
        )
    return {
        "id": session_id,
        "title": title,
        "workspace": workspace,
        "created_at": now,
        "updated_at": now,
    }


def get_session(session_id: str, user_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT id, title, workspace, created_at, updated_at
            FROM sessions
            WHERE id = ? AND user_id = ?
            """,
            (session_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_sessions(user_id: int) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, title, workspace, created_at, updated_at
            FROM sessions
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_session(session_id: str, user_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
    return cursor.rowcount > 0


def append_message(session_id: str, role: str, content: str) -> dict[str, Any]:
    now = utc_now()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO messages (session_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, role, content, now),
        )
        if role == "user":
            title = " ".join(content.split())[:42] or "New conversation"
            connection.execute(
                """
                UPDATE sessions
                SET title = CASE WHEN title = 'New conversation' THEN ? ELSE title END,
                    updated_at = ?
                WHERE id = ?
                """,
                (title, now, session_id),
            )
        else:
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
    return {
        "id": cursor.lastrowid,
        "role": role,
        "content": content,
        "created_at": now,
    }


def list_messages(session_id: str, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    if not get_session(session_id, user_id):
        return []
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, role, content, created_at
            FROM (
                SELECT id, role, content, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) AS recent_messages
            ORDER BY id ASC
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_document(
    user_id: int,
    name: str,
    content: str,
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    workspace: str = DEFAULT_WORKSPACE,
    file_type: str | None = None,
) -> dict[str, Any]:
    document_id = str(uuid.uuid4())
    now = utc_now()
    workspace = validate_workspace(workspace)
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO documents
                (id, user_id, name, content, workspace, file_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (document_id, user_id, name, content, workspace, file_type, now),
        )
        connection.executemany(
            """
            INSERT INTO chunks
                (document_id, user_id, position, content, embedding, page)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    user_id,
                    index,
                    chunk["content"],
                    json.dumps(embedding),
                    chunk.get("page"),
                )
                for index, (chunk, embedding) in enumerate(zip(chunks, embeddings))
            ],
        )
    return {
        "id": document_id,
        "name": name,
        "workspace": workspace,
        "file_type": file_type,
        "chunk_count": len(chunks),
        "created_at": now,
    }


def list_documents(
    user_id: int,
    workspace: str | None = None,
) -> list[dict[str, Any]]:
    parameters: list[Any] = [user_id]
    workspace_filter = ""
    if workspace:
        workspace_filter = "AND d.workspace = ?"
        parameters.append(validate_workspace(workspace))
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT d.id, d.name, d.workspace, d.file_type, d.created_at,
                   COUNT(c.id) AS chunk_count
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            WHERE d.user_id = ?
              {workspace_filter}
            GROUP BY d.id
            ORDER BY d.created_at DESC
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def delete_document(document_id: str, user_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM documents WHERE id = ? AND user_id = ?",
            (document_id, user_id),
        )
    return cursor.rowcount > 0


def search_chunks(
    user_id: int,
    query_embedding: list[float],
    workspace: str = DEFAULT_WORKSPACE,
    limit: int = 4,
) -> list[dict[str, Any]]:
    workspace = validate_workspace(workspace)
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT c.content, c.embedding, c.page,
                   d.id AS document_id, d.name
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.user_id = ? AND d.workspace = ?
            """,
            (user_id, workspace),
        ).fetchall()

    query_norm = math.sqrt(sum(value * value for value in query_embedding)) or 1.0
    scored: list[dict[str, Any]] = []
    for row in rows:
        embedding = json.loads(row["embedding"])
        embedding_norm = math.sqrt(sum(value * value for value in embedding)) or 1.0
        score = sum(
            left * right for left, right in zip(query_embedding, embedding)
        ) / (query_norm * embedding_norm)
        scored.append(
            {
                "document_id": row["document_id"],
                "name": row["name"],
                "content": row["content"],
                "page": row["page"],
                "score": round(score, 4),
            }
        )

    return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]
