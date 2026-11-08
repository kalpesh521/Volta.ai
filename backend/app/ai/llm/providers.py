"""
Registry of chat-model providers supported through `init_chat_model`.

Adding a provider = one entry here + `pip install <package>`. Provider ids
match LangChain's `model_provider` values.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from app.ai.errors import LLMNotConfiguredError


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    package: str
    module: str
    api_key_setting: str | None
    max_tokens_kwarg: str = "max_tokens"
    supports_timeout: bool = True
    supports_base_url: bool = False
    # LangChain `model_provider` when it differs from `id` (e.g. OpenRouter speaks the OpenAI API).
    langchain_provider: str | None = None
    # Endpoint used when AI_LLM_BASE_URL is empty.
    default_base_url: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in (
        ProviderSpec("anthropic", "langchain-anthropic", "langchain_anthropic", "ANTHROPIC_API_KEY"),
        ProviderSpec(
            "openai",
            "langchain-openai",
            "langchain_openai",
            "OPENAI_API_KEY",
            supports_base_url=True,
        ),
        ProviderSpec(
            "openrouter",
            "langchain-openai",
            "langchain_openai",
            "OPENROUTER_API_KEY",
            supports_base_url=True,
            langchain_provider="openai",
            default_base_url="https://openrouter.ai/api/v1",
        ),
        ProviderSpec("google_genai", "langchain-google-genai", "langchain_google_genai", "GOOGLE_API_KEY"),
        ProviderSpec("groq", "langchain-groq", "langchain_groq", "GROQ_API_KEY"),
        ProviderSpec(
            "ollama",
            "langchain-ollama",
            "langchain_ollama",
            None,
            max_tokens_kwarg="num_predict",
            supports_timeout=False,
            supports_base_url=True,
        ),
    )
}


def get_provider(provider_id: str) -> ProviderSpec:
    spec = PROVIDERS.get(provider_id.strip().lower())
    if spec is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise LLMNotConfiguredError(
            f"Unknown LLM provider '{provider_id}'. Supported: {supported}."
        )
    return spec


def is_installed(spec: ProviderSpec) -> bool:
    return importlib.util.find_spec(spec.module) is not None
