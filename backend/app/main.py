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

from app.core.config import settings
from app.core.cors import cors_origin_regex
from app.core.exceptions import register_exception_handlers
from app.core.rate_limit import limiter
from app.modules.auth.oauth_router import router as oauth_router
from app.modules.auth.router import router as auth_router
from app.modules.energy.router import router as energy_router
from app.modules.energy.ws import router as energy_ws_router
from app.modules.onboarding.router import router as onboarding_router
from app.webui import router as webui_router

logger = logging.getLogger("volta.app")

_UNSAFE_INGEST_TOKENS = frozenset(
    {"", "dev-ingest-token", "change-this-ingest-token-in-env"}
)


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    timescale = getattr(app.state, "timescale", None)
    redis_live = getattr(app.state, "redis_live", None)
    if timescale is not None:
        await timescale.close()
    if redis_live is not None:
        await redis_live.close()


app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG, lifespan=lifespan)

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
)

# --- All errors → {"success": false, "error": {"code": "...", "message": "..."}}
register_exception_handlers(app)

# --- Routers (paths unchanged)
app.include_router(auth_router)         # /auth/*
app.include_router(oauth_router)        # /auth/google/*
app.include_router(onboarding_router)   # /onboarding/*
app.include_router(energy_router)       # /energy/*
app.include_router(energy_ws_router)    # /ws/energy
app.include_router(webui_router)        # /ui  temporary energy HTML (not production)


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
    status = "ok" if (timescale_ok and redis_ok) or not settings.TELEMETRY_IO_ENABLED else "degraded"
    if settings.ENVIRONMENT.lower() == "production" and not (timescale_ok and redis_ok):
        status = "not_ready"
    return {
        "status": status,
        "timescale": timescale_ok,
        "redis": redis_ok,
        "telemetry_io": settings.TELEMETRY_IO_ENABLED,
    }
