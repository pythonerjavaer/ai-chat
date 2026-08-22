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
    )


settings = load_settings()
