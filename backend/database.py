import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .workspaces import DEFAULT_WORKSPACE, validate_workspace


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db() -> None:
    with connect() as connection:
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
            "CREATE INDEX IF NOT EXISTS idx_recruitment_jobs_deadline "
            "ON recruitment_jobs(status, closing_date)"
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


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


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
    space_filter = ""
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


def record_token_usage(
    user_id: int,
    space_id: str,
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


def list_recruitment_jobs() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM recruitment_jobs
            WHERE status = 'open'
              AND (closing_date IS NULL OR closing_date >= DATE('now'))
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
    """Remove prototype vacancies and unverified aggregator cards."""
    with connect() as connection:
        connection.execute(
            """
            DELETE FROM recruitment_jobs
            WHERE id IN (
                'sample-byteplus-product-2026',
                'sample-hsbc-analyst-2026',
                'sample-state-tech-2026'
            )
               OR source IN ('示例岗位，等待接入官方源', '示例数据')
            """
        )
        connection.execute(
            """
            DELETE FROM recruitment_jobs
            WHERE source IN ('国资小新', '银行招聘网', 'Adzuna API')
            """
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
            )
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
