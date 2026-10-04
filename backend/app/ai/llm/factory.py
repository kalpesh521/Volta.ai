"""
Build LangChain chat models from `AISettings`.

Every provider gets the same knobs (temperature, output budget, timeout,
retries) so swapping Claude ↔ Gemini ↔ GPT is an `.env` change only.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.ai.config import AISettings, LLMRole, ModelTarget
from app.ai.errors import LLMNotConfiguredError
from app.ai.llm.providers import get_provider, is_installed

logger = logging.getLogger("volta.ai.llm")


class ChatModelFactory:
    def __init__(self, settings: AISettings) -> None:
        self._settings = settings
        self._cache: dict[tuple[str, str], BaseChatModel] = {}

    @property
    def settings(self) -> AISettings:
        return self._settings

    def target(self, role: LLMRole) -> ModelTarget:
        return self._settings.target(role)

    def primary(self, role: LLMRole) -> BaseChatModel:
        if not self._settings.AI_ENABLED:
            raise LLMNotConfiguredError("AI is disabled (AI_ENABLED=false).")
        return self.build(self.target(role))

    def fallback(self) -> BaseChatModel | None:
        target = self._settings.fallback_target()
        if target is None:
            return None
        try:
            return self.build(target)
        except LLMNotConfiguredError as exc:
            logger.warning("fallback LLM ignored: %s", exc)
            return None

    def build(self, target: ModelTarget) -> BaseChatModel:
        key = (target.provider, target.model)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not target.model:
            raise LLMNotConfiguredError(f"No model configured for provider '{target.provider}'.")
        spec = get_provider(target.provider)
        if not is_installed(spec):
            raise LLMNotConfiguredError(
                f"Provider '{spec.id}' needs `pip install {spec.package}`."
            )
        kwargs: dict[str, Any] = {
            "temperature": self._settings.AI_LLM_TEMPERATURE,
            spec.max_tokens_kwarg: self._settings.AI_LLM_MAX_OUTPUT_TOKENS,
            "max_retries": self._settings.AI_LLM_MAX_RETRIES,
        }
        if spec.supports_timeout:
            kwargs["timeout"] = self._settings.AI_LLM_TIMEOUT_SECONDS
        if spec.api_key_setting:
            secret = getattr(self._settings, spec.api_key_setting, None)
            value = secret.get_secret_value().strip() if secret is not None else ""
            if not value:
                raise LLMNotConfiguredError(f"{spec.api_key_setting} is not set.")
            kwargs["api_key"] = value
        base_url = self._settings.AI_LLM_BASE_URL.strip()
        if base_url and spec.supports_base_url:
            kwargs["base_url"] = base_url

        model = init_chat_model(target.model, model_provider=spec.id, **kwargs)
        self._cache[key] = model
        logger.info("LLM ready provider=%s model=%s", spec.id, target.model)
        return model
