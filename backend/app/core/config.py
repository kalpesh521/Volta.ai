"""
Centralized application configuration.

All secrets/config are loaded from environment variables (via a `.env` file
in local dev). Nothing here should ever be hardcoded with real secrets -
`.env.example` documents what's required and `.env` (gitignored) holds the
real values.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    APP_NAME: str = "Volta Auth Service"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # --- Database ---
    # Async SQLAlchemy needs the "postgresql+asyncpg://" driver prefix.
    # Keep this URL WITHOUT any "?ssl=..." query param - asyncpg doesn't parse
    # that the way psycopg2 does. SSL is controlled separately via
    # DB_SSL_REQUIRED so switching Neon <-> local Postgres is a one-flag change.
    DATABASE_URL: str = (
        "postgresql+asyncpg://volta_user:volta_password@localhost:5432/volta_auth"
    )
    # True for hosted Postgres that requires TLS (Neon, Supabase, RDS, ...).
    # False for a local/self-hosted Postgres with no TLS configured.
    DB_SSL_REQUIRED: bool = False

    # --- JWT ---
    # HS256 chosen over RS256 for this service - see README "Security trade-offs".
    JWT_SECRET_KEY: str = "change-this-secret-in-env"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # --- CORS ---
    # Explicit allow-list, never "*", because we allow credentials (cookies/auth headers).
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # --- Google OAuth ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    # --- Rate limiting ---
    RATE_LIMIT_LOGIN: str = "5/minute"
    RATE_LIMIT_PASSWORD_RESET: str = "3/minute"
    RATE_LIMIT_INGEST: str = "120/minute"

    # --- Telemetry ingest (simulator → backend). Not a user JWT. ---
    # Fail closed in production if this is still the development default.
    INGEST_TOKEN: str = "dev-ingest-token"
    ENERGY_HISTORY_MAX_READINGS: int = 2880
    ENERGY_BALANCE_TOLERANCE_KW: float = 0.05

    # --- Frontend (used to redirect after OAuth completes) ---
    FRONTEND_URL: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance - env is read once per process."""
    return Settings()


settings = get_settings()
