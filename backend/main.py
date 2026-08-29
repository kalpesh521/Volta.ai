"""
ASGI entrypoint kept at the backend root so existing commands still work:

    uvicorn main:app --reload

The application object is defined in `app.main`.
"""
from app.main import app

__all__ = ["app"]
