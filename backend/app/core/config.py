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
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ]

    # --- Google OAuth ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    # --- Rate limiting ---
    RATE_LIMIT_LOGIN: str = "5/minute"
    RATE_LIMIT_PASSWORD_RESET: str = "3/minute"
    RATE_LIMIT_INGEST: str = "120/minute"

    # --- Telemetry ingest (HTTP path = tests/dev only). Not a user JWT. ---
    # Fail closed in production if this is still the development default.
    INGEST_TOKEN: str = "dev-ingest-token"
    INGEST_HTTP_ENABLED: bool = True
    ENERGY_HISTORY_MAX_READINGS: int = 2880
    ENERGY_BALANCE_TOLERANCE_KW: float = 0.05

    # TimescaleDB holds telemetry history. Separate from DATABASE_URL (auth/onboarding).
    TIMESCALE_DATABASE_URL: str = (
        "postgresql+asyncpg://volta:volta@localhost:5433/volta_ts"
    )
    TIMESCALE_SSL_REQUIRED: bool = False
    TIMESCALE_COMPRESS_AFTER_DAYS: int = 7
    TIMESCALE_RETENTION_DAYS: int = 90

    # Redis holds the latest snapshot + pub/sub for WebSocket fan-out.
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_KEY_PREFIX: str = "volta:live:"
    REDIS_LIVE_TTL_SECONDS: int = 120

    # RabbitMQ: simulator publishes, backend worker consumes.
    RABBITMQ_URL: str = "amqp://volta:volta@localhost:5672/volta"
    RABBITMQ_EXCHANGE: str = "telemetry"
    RABBITMQ_EXCHANGE_TYPE: str = "topic"
    RABBITMQ_ROUTING_KEY: str = "telemetry.ingest"
    RABBITMQ_QUEUE: str = "telemetry.ingest"
    RABBITMQ_PREFETCH: int = 50
    RABBITMQ_DLX: str = "telemetry.dlx"
    RABBITMQ_DLQ: str = "telemetry.ingest.dead"

    # Dashboard live feed (FastAPI WebSocket). Auth is the user JWT.
    WS_PATH: str = "/ws/energy"
    WS_HEARTBEAT_SECONDS: int = 30
    # False in pytest so HTTP tests stay on the in-memory store.
    TELEMETRY_IO_ENABLED: bool = True

    # --- Frontend (used to redirect after OAuth completes) ---
    FRONTEND_URL: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance - env is read once per process."""
    return Settings()


settings = get_settings()
