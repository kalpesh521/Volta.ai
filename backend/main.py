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
from app.routers import auth, oauth

app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

# --- Rate limiting (slowapi) - the 429 response shape is normalized in
# register_exception_handlers() below, so we don't use slowapi's default handler.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# --- CORS: explicit allow-list only, never "*", since credentials are allowed ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Centralized error shape: {"success": false, "error": {...}} ---
register_exception_handlers(app)

# --- Routers ---
app.include_router(auth.router)
app.include_router(oauth.router)


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok"}
