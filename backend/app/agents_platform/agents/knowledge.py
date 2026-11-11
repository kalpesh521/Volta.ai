"""
Knowledge agent (L0, read-only): manuals, fault codes, tariff orders, and the user's own bills and warranties.

It runs the `document` plan profile over the shared insight engine, so retrieval is the Phase 0
indexed vector + full-text search. When nothing relevant is found the engine reports a knowledge gap and
the result carries a deterministic "not in your documents" draft, so the model is never asked to guess.
Retrieved passages come back as `citations`.
"""
from __future__ import annotations

from app.agents_platform.agents.insight_engine import InsightAgent
from app.agents_platform.contracts import AgentName


class KnowledgeAgent(InsightAgent):
    name = AgentName.KNOWLEDGE
