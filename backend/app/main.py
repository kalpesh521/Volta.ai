"""
Application factory / ASGI app.

Wires CORS, rate limiting, centralized exception handling, and feature routers.
Run locally with:  uvicorn main:app --reload   (backend/main.py re-exports this)
"""
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware

from app.agents_platform.api.router import router as supervisor_router
from app.ai.checkpointer import checkpointer_provider
from app.ai.config import ai_settings
from app.ai.observability import configure_tracing
from app.core.config import settings
from app.core.cors import cors_origin_regex
from app.core.exceptions import register_exception_handlers
from app.core.logging_config import configure_logging
from app.core.metrics import metrics_endpoint
from app.core.rate_limit import limiter
from app.core.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from app.core.telemetry import configure_telemetry, shutdown_telemetry
from app.modules.assistant.actions.router import router as action_router
from app.modules.assistant.ops.router import router as home_ops_router
from app.modules.assistant.router import router as assistant_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.auth.oauth_router import router as oauth_router
from app.modules.auth.router import router as auth_router
from app.modules.energy.router import router as energy_router
from app.modules.assistant.notifications.ws import router as notification_ws_router
from app.modules.energy.ws import router as energy_ws_router
from app.modules.onboarding.router import router as onboarding_router
from app.webui import router as webui_router

logger = logging.getLogger("volta.app")

_UNSAFE_INGEST_TOKENS = frozenset(
    {"", "dev-ingest-token", "change-this-ingest-token-in-env"}
)


def _is_production() -> bool:
    return settings.ENVIRONMENT.lower() == "production"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)

    if _is_production():
        if ai_settings.AI_CHECKPOINT_BACKEND != "postgres":
            raise RuntimeError(
                "AI_CHECKPOINT_BACKEND must be 'postgres' when ENVIRONMENT=production "
                "(pending device confirmations would be lost on restart)"
            )
        if not settings.RATE_LIMIT_STORAGE_URI.strip():
            logger.warning(
                "RATE_LIMIT_STORAGE_URI is empty: rate limits are per process, not shared across replicas"
            )
    if settings.ENVIRONMENT.lower() == "production":
        token = (settings.INGEST_TOKEN or "").strip()
        if token in _UNSAFE_INGEST_TOKENS:
            raise RuntimeError(
                "INGEST_TOKEN must be a strong secret when ENVIRONMENT=production"
            )
        if settings.INGEST_HTTP_ENABLED:
            raise RuntimeError(
                "INGEST_HTTP_ENABLED must be false when ENVIRONMENT=production "
                "(simulator publishes to RabbitMQ; the worker consumes)"
            )

    configure_tracing(ai_settings)

    # Tables are created by the migration job in production; elsewhere the API creates them.
    await checkpointer_provider.start(ai_settings, auto_setup=not _is_production())

    app.state.timescale = None
    app.state.redis_live = None

    if settings.TELEMETRY_IO_ENABLED:
        from app.modules.energy.redis_live import RedisLiveStore
        from app.modules.energy.timescale_store import TimescaleTelemetryStore

        timescale = TimescaleTelemetryStore()
        try:
            await timescale.connect()
            await timescale.ensure_schema()
            app.state.timescale = timescale
        except Exception:
            logger.exception("TimescaleDB unavailable")
            await timescale.close()
            if settings.ENVIRONMENT.lower() == "production":
                raise

        redis_live = RedisLiveStore()
        try:
            await redis_live.connect()
            await redis_live.ping()
            app.state.redis_live = redis_live
        except Exception:
            logger.exception("Redis unavailable")
            await redis_live.close()
            if settings.ENVIRONMENT.lower() == "production":
                raise

    yield

    from app.agents_platform.api.deps import get_supervisor_graph
    from app.modules.assistant.actions.deps import get_action_graph

    get_action_graph.cache_clear()
    get_supervisor_graph.cache_clear()
    await checkpointer_provider.stop()
    shutdown_telemetry()

    timescale = getattr(app.state, "timescale", None)
    redis_live = getattr(app.state, "redis_live", None)
    if timescale is not None:
        await timescale.close()
    if redis_live is not None:
        await redis_live.close()


app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG, lifespan=lifespan)
configure_telemetry(settings, service_name="volta-api", app=app)

# --- Rate limiting (slowapi) - 429 shape normalized in register_exception_handlers()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# --- CORS: explicit allow-list, never "*". Localhost any port in non-production
# so Cursor/WSL forwarding (:8765 → :8766) can still open the live WebSocket.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER],
)

# Outermost, so the request id exists for every other layer and every log line.
app.add_middleware(RequestContextMiddleware)

# --- All errors → {"success": false, "error": {"code": "...", "message": "..."}}
register_exception_handlers(app)

# --- Routers (paths unchanged)
app.include_router(auth_router)         # /auth/*
app.include_router(oauth_router)        # /auth/google/*
app.include_router(onboarding_router)   # /onboarding/*
app.include_router(energy_router)       # /energy/*
app.include_router(energy_ws_router)    # /ws/energy
app.include_router(notification_ws_router)  # /ws/notifications
app.include_router(assistant_router)    # /assistant/*  read-only AI assistant
app.include_router(action_router)       # /assistant/actions/*  confirm before any command
app.include_router(supervisor_router)   # /v2/assistant/*  supervisor + specialist agents, conversation memory
app.include_router(home_ops_router)     # recommendations, alerts, automation rules
app.include_router(knowledge_router)    # /knowledge/*  admin and user documents
app.include_router(webui_router)        # /ui  temporary energy HTML (not production)


if settings.METRICS_ENABLED:
    # Prometheus scrape target. Unauthenticated: keep it on an internal network.
    app.add_api_route("/metrics", metrics_endpoint, include_in_schema=False)


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok"}


@app.get("/ready", tags=["health"])
async def ready_check() -> dict:
    timescale_ok = False
    redis_ok = False
    timescale = getattr(app.state, "timescale", None)
    redis_live = getattr(app.state, "redis_live", None)
    if timescale is not None:
        try:
            timescale_ok = await timescale.ping()
        except Exception:
            timescale_ok = False
    if redis_live is not None:
        try:
            redis_ok = await redis_live.ping()
        except Exception:
            redis_ok = False
    checkpointer_ok = await checkpointer_provider.ping()
    status = "ok" if (timescale_ok and redis_ok) or not settings.TELEMETRY_IO_ENABLED else "degraded"
    if _is_production() and not (timescale_ok and redis_ok and checkpointer_ok):
        status = "not_ready"
    elif not checkpointer_ok:
        status = "degraded"
    return {
        "status": status,
        "timescale": timescale_ok,
        "redis": redis_ok,
        "checkpointer": checkpointer_ok,
        "telemetry_io": settings.TELEMETRY_IO_ENABLED,
    }
