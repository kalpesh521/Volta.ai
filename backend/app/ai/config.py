"""
AI configuration, loaded from the same `.env` as `app.core.config`.

Kept separate from the core Settings so the AI surface (providers, models,
budgets, keys) is reviewed and rotated as one unit. Provider API keys use the
vendors' standard env names so LangSmith / SDK tooling recognises them.
"""
from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMRole(StrEnum):
    """Which job a model is used for. Each role may use a different model."""

    ROUTER = "router"
    ANSWER = "answer"
    GUARD = "guard"  # grey-zone input classifier (Phase 2). Off unless AI_GUARD_CLASSIFIER_ENABLED.


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
    # Guard model (grey-zone injection classifier). Empty = router model, then answer model.
    AI_GUARD_LLM_PROVIDER: str = ""
    AI_GUARD_LLM_MODEL: str = ""

    AI_LLM_TEMPERATURE: float = 0.1
    AI_LLM_MAX_OUTPUT_TOKENS: int = 800
    AI_LLM_TIMEOUT_SECONDS: float = 20.0
    AI_LLM_MAX_RETRIES: int = 2

    ANTHROPIC_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    OPENROUTER_API_KEY: SecretStr | None = None
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
    # Every number in an LLM answer must trace back to the facts it was given.
    #   enforce  ungrounded answer is replaced by the deterministic one (default)
    #   observe  logged and counted, the LLM answer is still returned (use to measure first)
    #   off      no check
    AI_GROUNDING_MODE: Literal["off", "observe", "enforce"] = "enforce"

    # Empty provider keeps the local hashing embedder (no API key, stable tests).
    AI_EMBEDDING_PROVIDER: str = ""
    AI_EMBEDDING_MODEL: str = "hashing-256"
    AI_EMBEDDING_DIMENSIONS: int = 256
    RAG_CHUNK_CHARS: int = 900
    RAG_CHUNK_OVERLAP: int = 120
    RAG_TOP_K: int = 5
    RAG_MIN_SCORE: float = 0.08
    # Candidates taken from each indexed search (nearest neighbours, full-text) before ranking.
    RAG_VECTOR_CANDIDATES: int = 40
    RAG_LEXICAL_CANDIDATES: int = 40

    # LangGraph checkpoints (conversation state, pending confirmations).
    # "memory" is per process and is lost on restart: development and tests only.
    AI_CHECKPOINT_BACKEND: Literal["memory", "postgres"] = "memory"
    # Empty reuses DATABASE_URL. Tables live in their own schema, away from OLTP tables.
    AI_CHECKPOINT_DATABASE_URL: str = ""
    AI_CHECKPOINT_SCHEMA: str = "langgraph"
    AI_CHECKPOINT_POOL_MIN: int = 1
    AI_CHECKPOINT_POOL_MAX: int = 10

    # --- Supervisor (v2 multi-agent chat) ---
    # Whole-request ceiling. The LLM call alone may take up to AI_LLM_TIMEOUT_SECONDS.
    AI_SUPERVISOR_DEADLINE_SECONDS: float = 30.0
    # Per-agent ceilings. A slow agent becomes a `timeout` result and never blocks its siblings.
    # Keep each above AI_TOOL_TIMEOUT_SECONDS: the agent waits on tools that run in parallel.
    AI_ENERGY_AGENT_DEADLINE_SECONDS: float = 8.0
    AI_KNOWLEDGE_AGENT_DEADLINE_SECONDS: float = 10.0
    AI_DEVICE_ACTION_AGENT_DEADLINE_SECONDS: float = 8.0
    # Answer generations per question: 2 means one regeneration when grounding fails.
    AI_ANSWER_MAX_ATTEMPTS: int = 2
    # Earlier turns used to resolve a follow-up. Older turns stay in the thread but are not read.
    AI_MEMORY_WINDOW_TURNS: int = 4
    # Conversations idle longer than this are deleted together with their checkpoints.
    AI_CONVERSATION_RETENTION_DAYS: int = 30

    # --- Guardrails (Phase 2) ---
    # Input guard: normalise, score for injection, mask personal data. `off` skips all of it.
    AI_GUARD_MODE: Literal["off", "observe", "enforce"] = "enforce"
    # Fast-path score (0..1) at or above which an input is refused, and the grey zone below it.
    AI_GUARD_BLOCK_SCORE: float = 0.8
    AI_GUARD_GREY_SCORE: float = 0.4
    # Grey-zone inputs are sent to the guard model when true. Costs one small call per grey input.
    AI_GUARD_CLASSIFIER_ENABLED: bool = False
    # Repeated blocked inputs from one user lead to a cool-down that doubles with each further block.
    AI_GUARD_STRIKE_LIMIT: int = 3
    AI_GUARD_STRIKE_WINDOW_SECONDS: int = 600
    AI_GUARD_COOLDOWN_BASE_SECONDS: int = 60
    AI_GUARD_COOLDOWN_MAX_SECONDS: int = 3600
    # Personal data typed into the chat. `enforce` masks it before the model or a checkpoint sees it.
    AI_PII_MODE: Literal["off", "observe", "enforce"] = "enforce"
    # memory = per process (mask and restore happen in one request, so this is correct for one API pod).
    # redis = AES-GCM encrypted, shared (needed once masking and restoring run in different processes).
    AI_PII_VAULT_BACKEND: Literal["memory", "redis"] = "memory"
    AI_PII_VAULT_KEY: SecretStr | None = None  # urlsafe base64 of 32 bytes. Required for the redis vault.
    AI_PII_VAULT_TTL_SECONDS: int = 900
    # Output checks beyond grounding: claims of acting on a device, unsafe electrical advice.
    AI_OUTPUT_POLICY_MODE: Literal["off", "observe", "enforce"] = "enforce"

    # Where budgets, abuse cool-downs and runtime flag overrides live.
    # memory = per process: development and tests. redis = shared by every API pod (use this in production).
    AI_PLATFORM_STORE: Literal["memory", "redis"] = "memory"
    # Redis for those stores. Empty = REDIS_URL. Use another database index than the live-telemetry one.
    AI_REDIS_URL: str = ""

    # --- Token budgets (Phase 2) ---
    AI_BUDGET_ENABLED: bool = True
    # Tokens per user per UTC day, by plan tier. A typical question uses 150 to 2,000.
    AI_BUDGET_DAILY_TOKENS_FREE: int = 30_000
    AI_BUDGET_DAILY_TOKENS_PLUS: int = 200_000
    # degrade = keep answering from deterministic facts without the model; reject = 429 until the next UTC day.
    AI_BUDGET_EXHAUSTED_ACTION: Literal["degrade", "reject"] = "degrade"

    # --- Feature flags, kill switches, registry (Phase 2) ---
    # Comma-separated flags forced off at startup, e.g. "ai.llm.enabled,ai.agent.knowledge.enabled".
    # A runtime override in Redis (set through the admin endpoint) wins over this list.
    AI_FLAGS_DISABLED: str = ""
    # Which registered prompt version each prompt uses. Empty = the registry's default for that prompt.
    AI_PROMPT_ANSWER_VERSION: str = ""
    AI_PROMPT_ROUTER_VERSION: str = ""

    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: SecretStr | None = None
    LANGSMITH_PROJECT: str = "suryaa-assistant"
    LANGSMITH_ENDPOINT: str = ""

    def target(self, role: LLMRole) -> ModelTarget:
        if role is LLMRole.GUARD and self.AI_GUARD_LLM_MODEL.strip():
            return ModelTarget(
                provider=(self.AI_GUARD_LLM_PROVIDER or self.AI_LLM_PROVIDER).strip(),
                model=self.AI_GUARD_LLM_MODEL.strip(),
            )
        if role in (LLMRole.ROUTER, LLMRole.GUARD) and self.AI_ROUTER_LLM_MODEL.strip():
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
