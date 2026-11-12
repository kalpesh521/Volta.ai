"""
Prompt registry: every prompt version that can run, and which one is active.

Prompts are code, reviewed and tested like code, and a change to one changes what the assistant says. So a new
version is *added* beside the old one and switched on by configuration, never edited in place:

    AI_PROMPT_ANSWER_VERSION=2026-11-12.v5     switch on, per deployment, no code change
    AI_PROMPT_ANSWER_VERSION=                  back to the registry default: a rollback is an env change

The active version is recorded in every response (`meta.prompt_version`) and every trace, so an evaluation or an
incident can always be tied to the exact wording that produced it. An unknown version fails startup instead of
quietly falling back.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage

from app.ai.config import AISettings
from app.modules.assistant import prompts

ANSWER = "answer"
ROUTER = "router"

# name -> (versions in release order, default). The default is the version that has been measured.
_REGISTERED: dict[str, tuple[tuple[str, ...], str]] = {
    ANSWER: ((prompts.PROMPT_VERSION, prompts.PROMPT_VERSION_V5), prompts.PROMPT_VERSION),
    ROUTER: ((prompts.PROMPT_VERSION,), prompts.PROMPT_VERSION),
}


class UnknownPromptVersionError(ValueError):
    pass


def versions(name: str) -> tuple[str, ...]:
    return _REGISTERED[name][0]


def default_version(name: str) -> str:
    return _REGISTERED[name][1]


def resolve(name: str, requested: str = "") -> str:
    """The version to use: the requested one if registered, the default if none was requested."""
    chosen = requested.strip() or default_version(name)
    if chosen not in versions(name):
        raise UnknownPromptVersionError(
            f"prompt {name!r} has no version {chosen!r}; registered: {', '.join(versions(name))}"
        )
    return chosen


@dataclass(frozen=True)
class ResolvedPrompts:
    """The prompt versions one deployment runs. Built once at startup, so a bad setting fails fast."""

    answer_version: str = prompts.PROMPT_VERSION
    router_version: str = prompts.PROMPT_VERSION

    def answer_messages(self, **kwargs: Any) -> list[BaseMessage]:
        return prompts.answer_messages(version=self.answer_version, **kwargs)

    def as_dict(self) -> dict[str, dict[str, Sequence[str] | str]]:
        return {
            ANSWER: {"active": self.answer_version, "registered": versions(ANSWER)},
            ROUTER: {"active": self.router_version, "registered": versions(ROUTER)},
        }


def resolve_prompts(settings: AISettings) -> ResolvedPrompts:
    return ResolvedPrompts(
        answer_version=resolve(ANSWER, settings.AI_PROMPT_ANSWER_VERSION),
        router_version=resolve(ROUTER, settings.AI_PROMPT_ROUTER_VERSION),
    )
