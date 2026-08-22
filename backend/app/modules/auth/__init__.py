# Auth module — thin wrapper that re-exports the existing auth routers.
# All auth business logic stays in app/routers/, app/services/, etc. so nothing breaks.
# main.py imports from here to keep the top-level consistent with other modules.
from app.routers.auth import router as auth_router
from app.routers.oauth import router as oauth_router

__all__ = ["auth_router", "oauth_router"]
