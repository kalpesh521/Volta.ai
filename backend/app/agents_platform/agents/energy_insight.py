"""
Energy Insight agent (L0, read-only): live state, battery, grid, devices, savings, patterns, timing.

A plan profile over the shared insight engine. It reads through the household-bound energy tools and
the deterministic analytics, and returns facts. It never writes wording with an LLM.
"""
from __future__ import annotations

from app.agents_platform.agents.insight_engine import InsightAgent
from app.agents_platform.contracts import AgentName


class EnergyInsightAgent(InsightAgent):
    name = AgentName.ENERGY_INSIGHT
