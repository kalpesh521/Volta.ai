"""
Application entrypoint. Wires together CORS, rate limiting, centralized
exception handling, and all routers.

Run locally with:  uvicorn main:app --reload
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.rate_limit import limiter

# Module-based router imports — each feature lives in app/modules/<name>/
from app.modules.auth import auth_router, oauth_router
from app.modules.onboarding.router import router as onboarding_router

app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

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

# --- Routers
app.include_router(auth_router)         # /auth/*
app.include_router(oauth_router)        # /auth/google/*
app.include_router(onboarding_router)   # /onboarding/*


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok"}
