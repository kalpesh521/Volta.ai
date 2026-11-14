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
    JUDGE = "judge"  # L3 answer-quality judge (Phase 3). Offline evals only; never on the request path.


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

    # Judge model (offline answer-quality evals, doc 05 §5). Should be a stronger model than the one it judges, so the
    # eval refuses to run until this is set. Empty provider = the answer provider.
    AI_JUDGE_LLM_PROVIDER: str = ""
    AI_JUDGE_LLM_MODEL: str = ""

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
    # Reciprocal Rank Fusion constant for combining the cosine and BM25 rankings (60 is the paper's value).
    RAG_RRF_K: int = 60
    # pgvector HNSW. `iterative_scan` keeps scanning when a filter (visibility, document type) discards most of the first
    # neighbours, so a filtered search does not come back short; it needs pgvector 0.8 and is skipped quietly before that.
    RAG_HNSW_ITERATIVE_SCAN: Literal["off", "strict_order", "relaxed_order"] = "relaxed_order"
    RAG_HNSW_EF_SEARCH: int = 100

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
    # Planning agents run solvers and may wait on a forecast provider, so their ceilings are higher.
    AI_FORECAST_AGENT_DEADLINE_SECONDS: float = 10.0
    AI_OPTIMIZER_AGENT_DEADLINE_SECONDS: float = 12.0
    AI_BILL_TARIFF_AGENT_DEADLINE_SECONDS: float = 6.0
    # Attempts for a planning agent whose dependency failed in a way worth retrying (`TransientAgentError`). The
    # attempts share the agent's one deadline; a retry never extends it. Device Action is never retried: it proposes.
    AI_PLANNING_AGENT_MAX_ATTEMPTS: int = 2
    AI_AGENT_RETRY_BACKOFF_SECONDS: float = 0.2
    # Agents only a `plus` user may use (comma-separated agent names). Checked at startup against the closed agent set.
    AI_PLUS_ONLY_AGENTS: str = "optimizer"
    # Bill what-if scenarios (add solar, add battery, shift load) need `plus`; a plain bill estimate does not.
    AI_PLUS_ONLY_WHAT_IF: bool = True
    # Automation rules (Phase 4): how many a home may hold, how many may wait for approval, and how long the rule
    # worker trusts a cached list of a home's active rules before reading it again.
    AI_AUTOMATION_MAX_RULES: int = 10
    AI_AUTOMATION_MAX_PENDING: int = 5
    AI_AUTOMATION_CACHE_SECONDS: float = 15.0
    # Attempts the Automation Rule agent gets to write a valid rule: one draft plus repairs from the validator's messages.
    AI_AUTOMATION_MAX_DRAFTS: int = 2
    # Open-Meteo hourly forecast for the Forecast agent. Off = persistence of the latest observed weather.
    AI_FORECAST_WEATHER_ENABLED: bool = False
    AI_FORECAST_WEATHER_TIMEOUT_SECONDS: float = 4.0
    # Answer generations per question: 2 means one regeneration when grounding fails.
    AI_ANSWER_MAX_ATTEMPTS: int = 2
    # Earlier turns used to resolve a follow-up. Older turns stay in the thread but are not read.
    AI_MEMORY_WINDOW_TURNS: int = 4
    # Long-term memory ("What Suryaa remembers"): durable facts the user chose to save. Max items per user and home,
    # max characters per item (the table holds at most 400), and how much of it one answer may read.
    AI_MEMORY_MAX_ITEMS: int = 50
    AI_MEMORY_ITEM_MAX_CHARS: int = 200
    AI_MEMORY_FACTS_LIMIT: int = 8
    AI_MEMORY_FACTS_MAX_CHARS: int = 600
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
        if role is LLMRole.JUDGE and self.AI_JUDGE_LLM_MODEL.strip():
            return ModelTarget(
                provider=(self.AI_JUDGE_LLM_PROVIDER or self.AI_LLM_PROVIDER).strip(),
                model=self.AI_JUDGE_LLM_MODEL.strip(),
            )
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
