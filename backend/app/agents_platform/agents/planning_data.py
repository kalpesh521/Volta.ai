"""
The household data the planning agents read, behind one small port.

Planning agents (forecast, optimiser, bill & tariff) compute from the home's telemetry history, live reading and
profile instead of calling the LLM-facing tools. This port is the only door: it is bound to one household (the
gateway closure, so no argument can address another tenant) and every read passes through gate G5's data scopes
(`guardrails.policy.AGENT_DATA`), so an agent can read only what its job needs.

`PlanningData` is a Protocol so agent tests need no database, and `GatewayPlanningData` is the production adapter over
the same `EnergyDataGateway` the Energy Insight tools use, so history is fetched once per request for everyone.
"""
from __future__ import annotations

from typing import Protocol

from app.agents_platform.contracts import AgentName
from app.agents_platform.guardrails.policy import DataScope, require_scope
from app.modules.assistant.domain.context import OnboardingDetails
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.energy.schemas import LiveEnergyOut, SimulatorProfileOut, TelemetryRecord


class PlanningData(Protocol):
    async def live(self) -> LiveEnergyOut | None: ...

    async def history(self, limit: int) -> list[TelemetryRecord]: ...

    def profile(self) -> SimulatorProfileOut: ...

    def onboarding(self) -> OnboardingDetails: ...


class GatewayPlanningData:
    def __init__(self, gateway: EnergyDataGateway) -> None:
        self._gateway = gateway

    async def live(self) -> LiveEnergyOut | None:
        return await self._gateway.live()

    async def history(self, limit: int) -> list[TelemetryRecord]:
        return await self._gateway.history(limit)

    def profile(self) -> SimulatorProfileOut:
        return self._gateway.profile()

    def onboarding(self) -> OnboardingDetails:
        return self._gateway.onboarding_details()


class AuthorizedData:
    """The port an agent is handed: each read checks the agent's data scopes first."""

    def __init__(self, agent: AgentName, inner: PlanningData) -> None:
        self._agent = agent
        self._inner = inner

    async def live(self) -> LiveEnergyOut | None:
        require_scope(self._agent, DataScope.LIVE)
        return await self._inner.live()

    async def history(self, limit: int) -> list[TelemetryRecord]:
        require_scope(self._agent, DataScope.HISTORY)
        return await self._inner.history(limit)

    def profile(self) -> SimulatorProfileOut:
        require_scope(self._agent, DataScope.PROFILE)
        return self._inner.profile()

    def onboarding(self) -> OnboardingDetails:
        require_scope(self._agent, DataScope.PROFILE)
        return self._inner.onboarding()
