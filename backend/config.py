import os
from dataclasses import dataclass
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
            or "gpt-5.4-nano"
        ),
        recruitment_web_search_interval_minutes=max(
            60,
            int(os.getenv("RECRUITMENT_WEB_SEARCH_INTERVAL_MINUTES", "360").strip() or "360"),
        ),
        recruitment_web_search_max_tool_calls=max(
            1,
            min(
                8,
                int(os.getenv("RECRUITMENT_WEB_SEARCH_MAX_TOOL_CALLS", "8").strip() or "8"),
            ),
        ),
        admin_dashboard_token=os.getenv("ADMIN_DASHBOARD_TOKEN", "").strip(),
    )


settings = load_settings()
