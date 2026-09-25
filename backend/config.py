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
    future_radar_sync_token: str
    public_base_url: str
    enable_future_radar_mcp: bool
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
    ai_interpret_provider: str = "auto"
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_model: str = "openrouter/free"
    openrouter_interpret_fallback_model: str = "openrouter/free"
    openrouter_endpoint: str = "https://openrouter.ai/api/v1/chat/completions"
    gemini_api_key: str = field(default="", repr=False)
    gemini_interpret_model: str = ""
    gemini_endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_allow_paid: bool = False
    database_backend: str = "sqlite"
    database_url: str = field(default="", repr=False)
    database_schema: str = "frostfire"
    database_pool_size: int = 8


def load_settings() -> Settings:
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    jwt_secret = os.getenv("JWT_SECRET", "").strip()

    # Public recruitment sources, title classification, persistence and login
    # work without a model provider. Paid capabilities validate availability
    # only when called; absence of a key must not prevent a zero-token app boot.
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
    ai_interpret_provider = os.getenv("AI_INTERPRET_PROVIDER", "auto").strip().lower() or "auto"
    if ai_interpret_provider not in {"auto", "openrouter", "gemini", "openai"}:
        raise RuntimeError("AI_INTERPRET_PROVIDER must be auto, openrouter, gemini or openai.")

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
        # A Future Radar run uses a run lease plus source leases while browser
        # reads and login requests still need a slot. Four connections can be
        # exhausted by the default worker fan-out before any user request gets
        # a chance to run.
        database_pool_size=max(1, min(12, int(os.getenv("DATABASE_POOL_SIZE", "8") or "8"))),
        cors_origins=cors_origins,
        adzuna_app_id=os.getenv("ADZUNA_APP_ID", "").strip(),
        adzuna_app_key=os.getenv("ADZUNA_APP_KEY", "").strip(),
        adzuna_country=os.getenv("ADZUNA_COUNTRY", "gb").strip() or "gb",
        recruitment_refresh_minutes=max(0, int(os.getenv("RECRUITMENT_REFRESH_MINUTES", "30").strip() or "30")),
        recruitment_ingest_token=os.getenv("RECRUITMENT_INGEST_TOKEN", "").strip(),
        future_radar_sync_token=os.getenv("FUTURE_RADAR_SYNC_TOKEN", "").strip(),
        public_base_url=(
            os.getenv("PUBLIC_BASE_URL", "https://frostfire-ai.onrender.com").strip().rstrip("/")
            or "https://frostfire-ai.onrender.com"
        ),
        enable_future_radar_mcp=os.getenv(
            "ENABLE_FUTURE_RADAR_MCP", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
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
            min(8, int(os.getenv("FUTURE_RADAR_MAX_WORKERS", "1").strip() or "1")),
        ),
        future_radar_ai_model=(
            os.getenv("FUTURE_RADAR_AI_MODEL", "").strip()
            or os.getenv("RECRUITMENT_WEB_SEARCH_MODEL", "").strip()
            or "gpt-5.4-mini"
        ),
        ai_interpret_provider=ai_interpret_provider,
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=(
            os.getenv("OPENROUTER_INTERPRET_MODEL", "").strip()
            or os.getenv("OPENROUTER_MODEL", "").strip()
            or "openrouter/free"
        ),
        openrouter_interpret_fallback_model=(
            os.getenv("OPENROUTER_INTERPRET_FALLBACK_MODEL", "openrouter/free").strip()
            or "openrouter/free"
        ),
        openrouter_endpoint=(
            os.getenv("OPENROUTER_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions").strip()
            or "https://openrouter.ai/api/v1/chat/completions"
        ),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_interpret_model=os.getenv("GEMINI_INTERPRET_MODEL", "").strip(),
        gemini_endpoint=(
            os.getenv("GEMINI_ENDPOINT", "https://generativelanguage.googleapis.com/v1beta").strip().rstrip("/")
            or "https://generativelanguage.googleapis.com/v1beta"
        ),
        gemini_allow_paid=os.getenv("GEMINI_ALLOW_PAID", "false").strip().lower() in {"1", "true", "yes", "on"},
    )


settings = load_settings()
