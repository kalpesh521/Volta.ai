"""
Tracing setup.

LangSmith reads `os.environ`, while our settings come from `.env` through
pydantic-settings (which does not export). Call `configure_tracing` once at
startup so graph, tool and LLM runs are traced when enabled.
"""
from __future__ import annotations

import logging
import os

from app.ai.config import AISettings

logger = logging.getLogger("volta.ai")


def configure_tracing(settings: AISettings) -> bool:
    key = settings.LANGSMITH_API_KEY.get_secret_value().strip() if settings.LANGSMITH_API_KEY else ""
    if not settings.LANGSMITH_TRACING or not key:
        return False
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.LANGSMITH_PROJECT)
    if settings.LANGSMITH_ENDPOINT.strip():
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.LANGSMITH_ENDPOINT.strip())
    logger.info("LangSmith tracing enabled project=%s", settings.LANGSMITH_PROJECT)
    return True
