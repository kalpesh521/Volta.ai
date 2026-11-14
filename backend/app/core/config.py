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
    # Clock-skew tolerance when validating token times. Servers (and WSL/VM clocks) drift by a
    # fraction of a second to a few seconds, and a token must not be rejected as "issued in the
    # future" because the validating clock is slightly behind the issuing one.
    JWT_LEEWAY_SECONDS: int = 10
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
    # Empty keeps counters in this process (fine for one replica, wrong for several).
    # Set a Redis URL (use a database index other than the live-data one) to share counters.
    RATE_LIMIT_STORAGE_URI: str = ""
    RATE_LIMIT_LOGIN: str = "5/minute"
    RATE_LIMIT_PASSWORD_RESET: str = "3/minute"
    RATE_LIMIT_INGEST: str = "120/minute"

    # --- Telemetry ingest (HTTP path = tests/dev only). Not a user JWT. ---
    # Fail closed in production if this is still the development default.
    INGEST_TOKEN: str = "dev-ingest-token"
    INGEST_HTTP_ENABLED: bool = True
    ENERGY_HISTORY_MAX_READINGS: int = 2880
    ENERGY_BALANCE_TOLERANCE_KW: float = 0.05
    # Default DISCOM-style rates used when onboarding omits numeric tariff.
    # Assistant savings and profile.tariff_rate read stored grid_configs first.
    DEFAULT_ENERGY_CHARGE_INR_PER_KWH: float = 8.50
    DEFAULT_EXPORT_CREDIT_INR_PER_KWH: float = 6.20
    # Non-critical appliances at or above this nameplate are "important".
    DEVICE_IMPORTANT_POWER_KW: float = 1.50

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
    NOTIFICATION_KEY_PREFIX: str = "volta:notify:"
    NOTIFICATION_CACHE_TTL_SECONDS: int = 30

    # RabbitMQ: simulator publishes, backend worker consumes.
    RABBITMQ_URL: str = "amqp://volta:volta@localhost:5672/volta"
    RABBITMQ_EXCHANGE: str = "telemetry"
    RABBITMQ_EXCHANGE_TYPE: str = "topic"
    RABBITMQ_ROUTING_KEY: str = "telemetry.ingest"
    RABBITMQ_QUEUE: str = "telemetry.ingest"
    RABBITMQ_PREFETCH: int = 50
    RABBITMQ_DLX: str = "telemetry.dlx"
    RABBITMQ_DLQ: str = "telemetry.ingest.dead"
    # The automation worker reads its own copy of every tick from this queue. Stale ticks are useless to it, so the
    # queue forgets them: a message lives at most RABBITMQ_AUTOMATION_TTL_MS and the queue holds at most
    # RABBITMQ_AUTOMATION_MAX_LENGTH (oldest dropped first), so a stopped worker cannot make the broker grow.
    RABBITMQ_AUTOMATION_QUEUE: str = "telemetry.automation"
    RABBITMQ_AUTOMATION_TTL_MS: int = 60_000
    RABBITMQ_AUTOMATION_MAX_LENGTH: int = 5_000

    # --- Ingest worker: competing consumers and batched Timescale writes ---
    # Run any number of ingest-worker replicas on the one queue. Each replica buffers valid ticks and
    # writes them in one transaction, then acks. A flush happens at INGEST_BATCH_SIZE ticks or when the
    # oldest buffered tick is INGEST_BATCH_MAX_AGE_SECONDS old, whichever comes first. Nothing is acked
    # before its batch is committed. 1 = one tick per transaction (the pre-Phase-5 behaviour).
    # Clamped to RABBITMQ_PREFETCH: the broker never delivers more unacked ticks than that, so a larger
    # batch could only ever flush on age. Keep PREFETCH >= 2 x BATCH_SIZE so reading overlaps writing.
    INGEST_BATCH_SIZE: int = 25
    INGEST_BATCH_MAX_AGE_SECONDS: float = 0.5
    # One batch write may take this long before it counts as a transient failure and is requeued.
    INGEST_WRITE_TIMEOUT_SECONDS: float = 30.0
    # A tick whose own write keeps failing for a non-transient reason is dead-lettered after this many
    # failed writes (counted per replica, and from x-delivery-count when the queue is a quorum queue).
    INGEST_MAX_ATTEMPTS: int = 5
    # Pause before a whole batch is requeued after a transient failure (database down): doubles up to the max.
    INGEST_RETRY_BACKOFF_SECONDS: float = 1.0
    INGEST_RETRY_BACKOFF_MAX_SECONDS: float = 30.0
    # SIGTERM: stop consuming, then flush what is buffered for at most this long. Keep it below the
    # orchestrator's termination grace period (Kubernetes default 30 s).
    INGEST_SHUTDOWN_TIMEOUT_SECONDS: float = 25.0
    # How often each replica publishes the queue depth gauges. 0 turns the poller off.
    INGEST_LAG_POLL_SECONDS: float = 5.0

    # Dashboard live feed (FastAPI WebSocket). Auth is the user JWT.
    WS_PATH: str = "/ws/energy"
    WS_HEARTBEAT_SECONDS: int = 30
    # False in pytest so HTTP tests stay on the in-memory store.
    TELEMETRY_IO_ENABLED: bool = True

    # --- Frontend (used to redirect after OAuth completes) ---
    FRONTEND_URL: str = "http://localhost:3000"

    # Comma-separated emails that may upload shared manuals and policies.
    # A user with is_admin=true is also an admin. Empty means flag-only.
    SURYAA_ADMIN_EMAILS: str = ""

    # Original uploads. Embeddings live in Postgres; this directory holds the file.
    KNOWLEDGE_STORAGE_DIR: str = "var/knowledge"
    KNOWLEDGE_MAX_UPLOAD_BYTES: int = 15 * 1024 * 1024
    KNOWLEDGE_USE_PGVECTOR: bool = True
    RATE_LIMIT_KNOWLEDGE: str = "30/minute"

    # Device commands stay off until both flags allow them.
    # KILL_SWITCH true means every command is rejected.
    DEVICE_CONTROL_ENABLED: bool = False
    DEVICE_KILL_SWITCH: bool = True
    # How long a proposed action may wait for the user's confirmation.
    ACTION_CONFIRMATION_TTL_SECONDS: int = 120
    # Finished or expired proposals keep their LangGraph checkpoint this long before pruning.
    ACTION_CHECKPOINT_RETENTION_HOURS: int = 24
    DEVICE_COMMAND_EXCHANGE: str = "device.commands"
    DEVICE_COMMAND_QUEUE: str = "device.commands"
    DEVICE_COMMAND_SIMULATOR_QUEUE: str = "simulator.device.commands"
    DEVICE_COMMAND_ROUTING_KEY: str = "device.command"
    # Transactional outbox: a confirmed command is recorded with the request's own transaction and published after the
    # commit by the relay (tracker 4.3, 4.5). False publishes inside the request, as before Phase 4.
    DEVICE_COMMAND_OUTBOX_ENABLED: bool = True
    OUTBOX_POLL_SECONDS: float = 2.0
    OUTBOX_BATCH_SIZE: int = 20
    OUTBOX_MAX_ATTEMPTS: int = 5
    OUTBOX_BACKOFF_SECONDS: float = 2.0
    # A command older than this when the relay finally reaches it is dropped: switching a heater on because of a
    # confirmation given ten minutes ago, after an outage, is not what the user asked for.
    DEVICE_COMMAND_MAX_AGE_SECONDS: int = 300

    # Critical alerts only. Leave EMAIL_PROVIDER empty to keep email off.
    # Celery broker defaults to RABBITMQ_URL. Queue: suryaa.notifications.
    CELERY_BROKER_URL: str = ""
    EMAIL_PROVIDER: str = ""
    RESEND_API_KEY: str = ""
    SENDGRID_API_KEY: str = ""
    EMAIL_FROM: str = "Suryaa <alerts@localhost>"

    # --- Observability ---
    LOG_LEVEL: str = "INFO"
    # One JSON object per line (for log shippers). Plain text is easier to read locally.
    LOG_JSON: bool = False
    # Traces are exported over OTLP/HTTP. Off by default; the request-id and log
    # correlation below work without it.
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""  # e.g. http://otel-collector:4318
    OTEL_TRACES_SAMPLER_RATIO: float = 1.0
    # Prometheus. /metrics is unauthenticated: expose it on an internal network only.
    METRICS_ENABLED: bool = False
    # Workers have no HTTP server; a port above 0 starts a metrics endpoint for them.
    WORKER_METRICS_PORT: int = 0

    # --- Process model (Phase 5) ---
    # uvicorn worker processes per API pod (read by `python -m app.serve`). More than 1 needs every
    # piece of shared state to live outside the process: see app/core/startup_guards.py.
    WEB_CONCURRENCY: int = 1
    # In-flight requests get this long to finish after SIGTERM before the worker is stopped.
    # Keep it below the orchestrator's termination grace period.
    API_GRACEFUL_SHUTDOWN_SECONDS: int = 20


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance - env is read once per process."""
    return Settings()


settings = get_settings()
