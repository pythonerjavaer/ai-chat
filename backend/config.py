import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    jwt_secret: str
    ai_model: str
    embedding_model: str
    database_path: Path
    cors_origins: list[str]
    adzuna_app_id: str
    adzuna_app_key: str
    adzuna_country: str
    recruitment_refresh_minutes: int
    recruitment_ingest_token: str
    recruitment_web_search_enabled: bool
    recruitment_web_search_model: str
    recruitment_web_search_interval_minutes: int
    recruitment_web_search_max_tool_calls: int
    admin_dashboard_token: str
    future_radar_enabled: bool
    future_radar_default_interval_minutes: int
    future_radar_close_confirmations: int
    future_radar_max_workers: int
    future_radar_ai_model: str
    database_backend: str = "sqlite"
    database_url: str = field(default="", repr=False)
    database_schema: str = "frostfire"
    database_pool_size: int = 4


def load_settings() -> Settings:
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    jwt_secret = os.getenv("JWT_SECRET", "").strip()

    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured.")

    database_value = os.getenv("DATABASE_PATH", "").strip()
    database_path = (
        Path(database_value).expanduser()
        if database_value
        else BASE_DIR / "data" / "ai_chat.db"
    )
    database_url = os.getenv("DATABASE_URL", "").strip()
    database_backend = os.getenv("DATABASE_BACKEND", "").strip().lower()
    database_backend = database_backend or ("postgres" if database_url else "sqlite")
    if database_backend == "postgresql":
        database_backend = "postgres"
    if database_backend not in {"sqlite", "postgres"}:
        raise RuntimeError("DATABASE_BACKEND must be sqlite or postgres.")
    if database_backend == "postgres" and not database_url.startswith(
        ("postgresql://", "postgres://")
    ):
        raise RuntimeError("A PostgreSQL DATABASE_URL is required; SQLite fallback is disabled.")
    if os.getenv("RENDER", "").strip().lower() == "true" and database_backend != "postgres":
        raise RuntimeError(
            "Render requires PostgreSQL persistence. Configure DATABASE_BACKEND and DATABASE_URL."
        )
    database_schema = os.getenv("DATABASE_SCHEMA", "frostfire").strip() or "frostfire"
    if (
        not database_schema.isascii()
        or not database_schema.replace("_", "").isalnum()
        or not database_schema[0].isalpha()
        or len(database_schema) > 63
        or database_schema.lower() in {
            "public", "auth", "storage", "realtime", "extensions", "information_schema",
            "graphql", "graphql_public", "supabase_functions", "supabase_migrations",
        }
        or database_schema.lower().startswith("pg_")
    ):
        raise RuntimeError("DATABASE_SCHEMA must name a private application schema.")
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:5500,http://localhost:5500",
        ).split(",")
        if origin.strip()
    ]
    web_search_enabled = os.getenv(
        "RECRUITMENT_WEB_SEARCH_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}

    return Settings(
        openai_api_key=openai_api_key,
        jwt_secret=jwt_secret,
        ai_model=os.getenv("AI_MODEL", "").strip() or "gpt-4o-mini",
        embedding_model=(
            os.getenv("EMBEDDING_MODEL", "").strip() or "text-embedding-3-small"
        ),
        database_path=database_path,
        database_backend=database_backend,
        database_url=database_url,
        database_schema=database_schema,
        database_pool_size=max(1, min(12, int(os.getenv("DATABASE_POOL_SIZE", "4") or "4"))),
        cors_origins=cors_origins,
        adzuna_app_id=os.getenv("ADZUNA_APP_ID", "").strip(),
        adzuna_app_key=os.getenv("ADZUNA_APP_KEY", "").strip(),
        adzuna_country=os.getenv("ADZUNA_COUNTRY", "gb").strip() or "gb",
        recruitment_refresh_minutes=max(0, int(os.getenv("RECRUITMENT_REFRESH_MINUTES", "30").strip() or "30")),
        recruitment_ingest_token=os.getenv("RECRUITMENT_INGEST_TOKEN", "").strip(),
        recruitment_web_search_enabled=web_search_enabled,
        recruitment_web_search_model=(
            os.getenv("RECRUITMENT_WEB_SEARCH_MODEL", "").strip()
            or os.getenv("AI_MODEL", "").strip()
            or "gpt-5.4-mini"
        ),
        recruitment_web_search_interval_minutes=max(
            60,
            int(os.getenv("RECRUITMENT_WEB_SEARCH_INTERVAL_MINUTES", "360").strip() or "360"),
        ),
        recruitment_web_search_max_tool_calls=max(
            1,
            min(
                10,
                int(os.getenv("RECRUITMENT_WEB_SEARCH_MAX_TOOL_CALLS", "10").strip() or "10"),
            ),
        ),
        admin_dashboard_token=os.getenv("ADMIN_DASHBOARD_TOKEN", "").strip(),
        future_radar_enabled=os.getenv(
            "FUTURE_RADAR_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"},
        future_radar_default_interval_minutes=max(
            5,
            int(os.getenv("FUTURE_RADAR_DEFAULT_INTERVAL_MINUTES", "30").strip() or "30"),
        ),
        future_radar_close_confirmations=max(
            2,
            min(10, int(os.getenv("FUTURE_RADAR_CLOSE_CONFIRMATIONS", "2").strip() or "2")),
        ),
        future_radar_max_workers=max(
            1,
            min(8, int(os.getenv("FUTURE_RADAR_MAX_WORKERS", "4").strip() or "4")),
        ),
        future_radar_ai_model=(
            os.getenv("FUTURE_RADAR_AI_MODEL", "").strip()
            or os.getenv("RECRUITMENT_WEB_SEARCH_MODEL", "").strip()
            or "gpt-5.4-mini"
        ),
    )


settings = load_settings()
