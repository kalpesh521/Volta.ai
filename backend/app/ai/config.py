"""
AI configuration, loaded from the same `.env` as `app.core.config`.

Kept separate from the core Settings so the AI surface (providers, models,
budgets, keys) is reviewed and rotated as one unit. Provider API keys use the
vendors' standard env names so LangSmith / SDK tooling recognises them.
"""
from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMRole(StrEnum):
    """Which job a model is used for. Each role may use a different model."""

    ROUTER = "router"
    ANSWER = "answer"


class ModelTarget(BaseModel):
    provider: str
    model: str


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    AI_ENABLED: bool = True

    # Answer model (quality). Default is the cheapest widely available tier.
    AI_LLM_PROVIDER: str = "google_genai"
    AI_LLM_MODEL: str = "gemini-3.5-flash-lite"
    # Router model (intent classification for ambiguous questions). Empty = answer model.
    AI_ROUTER_LLM_PROVIDER: str = ""
    AI_ROUTER_LLM_MODEL: str = ""
    # Used when the primary provider errors or times out. Empty = no fallback.
    AI_FALLBACK_LLM_PROVIDER: str = ""
    AI_FALLBACK_LLM_MODEL: str = ""

    AI_LLM_TEMPERATURE: float = 0.1
    AI_LLM_MAX_OUTPUT_TOKENS: int = 800
    AI_LLM_TIMEOUT_SECONDS: float = 20.0
    AI_LLM_MAX_RETRIES: int = 2

    ANTHROPIC_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    # Ollama / self-hosted OpenAI-compatible gateways.
    AI_LLM_BASE_URL: str = ""

    # Workflow budgets / guards
    AI_TOOL_TIMEOUT_SECONDS: float = 5.0
    AI_STALE_DATA_SECONDS: int = 600
    AI_MAX_QUESTION_CHARS: int = 500
    # Upper bound on the facts JSON sent to the answer model (~4 chars per token).
    AI_MAX_FACTS_CHARS: int = 6000
    AI_TREND_WINDOW_MINUTES: int = 60
    AI_TREND_HISTORY_LIMIT: int = 240
    RATE_LIMIT_ASSISTANT: str = "20/minute"

    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: SecretStr | None = None
    LANGSMITH_PROJECT: str = "suryaa-assistant"
    LANGSMITH_ENDPOINT: str = ""

    def target(self, role: LLMRole) -> ModelTarget:
        if role is LLMRole.ROUTER and self.AI_ROUTER_LLM_MODEL.strip():
            return ModelTarget(
                provider=(self.AI_ROUTER_LLM_PROVIDER or self.AI_LLM_PROVIDER).strip(),
                model=self.AI_ROUTER_LLM_MODEL.strip(),
            )
        return ModelTarget(provider=self.AI_LLM_PROVIDER.strip(), model=self.AI_LLM_MODEL.strip())

    def fallback_target(self) -> ModelTarget | None:
        model = self.AI_FALLBACK_LLM_MODEL.strip()
        if not model:
            return None
        return ModelTarget(
            provider=(self.AI_FALLBACK_LLM_PROVIDER or self.AI_LLM_PROVIDER).strip(),
            model=model,
        )


@lru_cache
def get_ai_settings() -> AISettings:
    return AISettings()


ai_settings = get_ai_settings()
