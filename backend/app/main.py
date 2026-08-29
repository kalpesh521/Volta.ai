"""
Application factory / ASGI app.

Wires CORS, rate limiting, centralized exception handling, and feature routers.
Run locally with:  uvicorn main:app --reload   (backend/main.py re-exports this)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.rate_limit import limiter
from app.modules.auth.oauth_router import router as oauth_router
from app.modules.auth.router import router as auth_router
from app.modules.energy.router import router as energy_router
from app.modules.onboarding.router import router as onboarding_router

_UNSAFE_INGEST_TOKENS = frozenset(
    {"", "dev-ingest-token", "change-this-ingest-token-in-env"}
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.ENVIRONMENT.lower() == "production":
        token = (settings.INGEST_TOKEN or "").strip()
        if token in _UNSAFE_INGEST_TOKENS:
            raise RuntimeError(
                "INGEST_TOKEN must be a strong secret when ENVIRONMENT=production"
            )
    yield


app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG, lifespan=lifespan)

# --- Rate limiting (slowapi) - 429 shape normalized in register_exception_handlers()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# --- CORS: explicit allow-list, never "*", credentials are allowed
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
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


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok"}
