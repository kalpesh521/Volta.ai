"""Errors raised by the AI platform layer. Never carry API keys or prompts."""
from __future__ import annotations


class AIError(Exception):
    """Base class for AI platform failures."""


class LLMNotConfiguredError(AIError):
    """Provider unknown, package missing, or API key absent."""


class LLMInvocationError(AIError):
    """Provider call failed, timed out, or returned unparseable structured output."""
