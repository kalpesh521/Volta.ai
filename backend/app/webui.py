"""
Temporary static energy console for backend testing.

Served from the FastAPI process so login + /energy/* + /ws/energy share one
origin (no CORS, no extra Vite port). React will replace this later; the API
contract stays.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter(tags=["dev-ui"], include_in_schema=False)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE = _REPO_ROOT / "frontend" / "public" / "energy-console.html"


def _console_file() -> Path:
    if settings.ENVIRONMENT.lower() == "production":
        raise HTTPException(status_code=404, detail="Not found")
    if not _CONSOLE.is_file():
        raise HTTPException(status_code=404, detail="energy console missing")
    return _CONSOLE


@router.get("/ui")
@router.get("/ui/energy")
@router.get("/ui/energy-console.html")
async def energy_console() -> FileResponse:
    return FileResponse(_console_file(), media_type="text/html; charset=utf-8")
