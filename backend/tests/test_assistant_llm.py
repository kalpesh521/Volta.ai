"""AI platform layer: provider factory and structured-output gateway (no network)."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, SecretStr

from app.ai.config import AISettings, LLMRole, ModelTarget
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.factory import ChatModelFactory
from app.ai.llm.gateway import LangChainStructuredLLM


def _settings(**overrides) -> AISettings:
    base = dict(
        _env_file=None,
        AI_LLM_PROVIDER="google_genai",
        AI_LLM_MODEL="gemini-2.5-flash-lite",
        GOOGLE_API_KEY=None,
        ANTHROPIC_API_KEY=None,
        OPENAI_API_KEY=None,
    )
    base.update(overrides)
    return AISettings(**base)


def test_missing_key_is_not_configured():
    factory = ChatModelFactory(_settings())
    with pytest.raises(LLMNotConfiguredError, match="GOOGLE_API_KEY"):
        factory.primary(LLMRole.ANSWER)
    assert LangChainStructuredLLM(factory).is_available(LLMRole.ANSWER) is False


def test_unknown_provider_and_disabled_ai():
    with pytest.raises(LLMNotConfiguredError, match="Unknown LLM provider"):
        ChatModelFactory(_settings(AI_LLM_PROVIDER="nope")).primary(LLMRole.ANSWER)
    with pytest.raises(LLMNotConfiguredError, match="disabled"):
        ChatModelFactory(_settings(AI_ENABLED=False)).primary(LLMRole.ANSWER)


@pytest.mark.parametrize(
    ("provider", "model", "key_field", "cls_name"),
    [
        ("google_genai", "gemini-2.5-flash-lite", "GOOGLE_API_KEY", "ChatGoogleGenerativeAI"),
        ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY", "ChatAnthropic"),
        ("openai", "gpt-5-mini", "OPENAI_API_KEY", "ChatOpenAI"),
    ],
)
def test_builds_each_provider_with_uniform_knobs(provider, model, key_field, cls_name):
    settings = _settings(AI_LLM_PROVIDER=provider, AI_LLM_MODEL=model, **{key_field: SecretStr("k")})
    factory = ChatModelFactory(settings)
    chat = factory.primary(LLMRole.ANSWER)
    assert type(chat).__name__ == cls_name
    assert factory.primary(LLMRole.ANSWER) is chat


def test_router_role_can_use_a_cheaper_model():
    settings = _settings(
        AI_LLM_PROVIDER="anthropic",
        AI_LLM_MODEL="claude-haiku-4-5",
        AI_ROUTER_LLM_PROVIDER="google_genai",
        AI_ROUTER_LLM_MODEL="gemini-2.5-flash-lite",
    )
    assert settings.target(LLMRole.ROUTER) == ModelTarget(provider="google_genai", model="gemini-2.5-flash-lite")
    assert settings.target(LLMRole.ANSWER).provider == "anthropic"


def test_misconfigured_fallback_is_ignored():
    factory = ChatModelFactory(
        _settings(AI_FALLBACK_LLM_PROVIDER="anthropic", AI_FALLBACK_LLM_MODEL="claude-haiku-4-5")
    )
    assert factory.fallback() is None


class Echo(BaseModel):
    text: str


class _StubChat:
    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error

    def with_structured_output(self, schema, include_raw=False):
        async def run(_messages):
            if self._error:
                raise self._error
            return self._result

        return RunnableLambda(lambda m: None, afunc=run)


class _StubFactory:
    def __init__(self, chat, fallback=None):
        self._chat = chat
        self._fallback = fallback

    def primary(self, role):
        return self._chat

    def fallback(self):
        return self._fallback

    def target(self, role):
        return ModelTarget(provider="stub", model="stub-model")


async def test_gateway_parses_output_and_captures_usage():
    raw = AIMessage(
        content="",
        response_metadata={"model_name": "stub-model-001"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    llm = LangChainStructuredLLM(
        _StubFactory(_StubChat({"raw": raw, "parsed": {"text": "hi"}, "parsing_error": None}))
    )
    result = await llm.ainvoke(LLMRole.ANSWER, Echo, [HumanMessage("q")])
    assert result.output == Echo(text="hi")
    assert result.model == "stub-model-001"
    assert result.usage.total_tokens == 15


async def test_gateway_wraps_provider_and_parsing_errors():
    failing = LangChainStructuredLLM(_StubFactory(_StubChat(error=TimeoutError("slow"))))
    with pytest.raises(LLMInvocationError, match="TimeoutError"):
        await failing.ainvoke(LLMRole.ANSWER, Echo, [HumanMessage("q")])

    unparseable = LangChainStructuredLLM(
        _StubFactory(_StubChat({"raw": AIMessage(""), "parsed": None, "parsing_error": ValueError()}))
    )
    with pytest.raises(LLMInvocationError, match="unparseable"):
        await unparseable.ainvoke(LLMRole.ANSWER, Echo, [HumanMessage("q")])


async def test_gateway_uses_fallback_model_on_primary_error():
    ok = {"raw": AIMessage(""), "parsed": {"text": "from fallback"}, "parsing_error": None}
    llm = LangChainStructuredLLM(
        _StubFactory(_StubChat(error=RuntimeError("down")), fallback=_StubChat(ok))
    )
    result = await llm.ainvoke(LLMRole.ANSWER, Echo, [HumanMessage("q")])
    assert result.output.text == "from fallback"
