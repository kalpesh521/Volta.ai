"""
Bill & Tariff agent (L0, read-only): an itemised bill estimate on the tariff in force, and what-if scenarios.

It never writes the answer and never does arithmetic in a prompt: the engine (`calculator.py`, `whatif.py`)
produces every figure, and the agent hands them to the synthesizer as facts. Three things it refuses to hide:

* **Which tariff table was used and whether it is official.** The shipped tables are illustrative
  (`verified=false`). That flag, and whether the table was a fallback, are in the facts and in the result's
  warnings, and the answer prompt must say so, so illustrative rates can never be presented as an official tariff.
* **Where the units came from.** Monthly usage is estimated from the user's stated average bill (see `usage.py`);
  without one the agent says what it needs instead of guessing.
* **What the what-if assumed.** Every default behind a scenario is a number in the facts.

What-if scenarios need the Plus plan when the deployment says so (`Entitlements.what_if_allowed`); a plain bill
estimate does not.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from langchain_core.runnables import RunnableConfig

from app.agents_platform.agents.bill_tariff.calculator import calculate_bill
from app.agents_platform.agents.bill_tariff.tables import TariffNotFound, table_for
from app.agents_platform.agents.bill_tariff.usage import (
    assumptions_facts,
    scenarios_for,
    usage_from_bill,
    wants_what_if,
)
from app.agents_platform.agents.bill_tariff.whatif import ScenarioError, simulate
from app.agents_platform.agents.planning_data import PlanningData
from app.agents_platform.contracts import AgentCall, AgentName, AgentResult, AgentStatus, AnswerParts

CATEGORY = "residential"
NEEDS_BASIS_REASON = "bill_basis_missing"


def _inr(value: object) -> str:
    return f"{float(value):,.0f}"  # type: ignore[arg-type]


class BillTariffAgent:
    name = AgentName.BILL_TARIFF

    def __init__(self, data: PlanningData, *, now: Callable[[], datetime], what_if_allowed: bool = True) -> None:
        self._data = data
        self._now = now
        self._what_if_allowed = what_if_allowed

    async def run(self, call: AgentCall, *, config: RunnableConfig | None = None) -> AgentResult:
        started = time.perf_counter()
        profile, onboarding = self._data.profile(), self._data.onboarding()
        today: date = self._now().date()

        try:
            selection = table_for(
                onboarding.discom,
                CATEGORY,
                today,
                flat_rate=profile.tariff_rate,
                flat_export_credit=profile.export_credit_inr_per_kwh or 0,
            )
        except TariffNotFound:
            return self._needs(started, "I do not have a tariff to price your bill on.", call)

        if onboarding.avg_monthly_bill_inr is None:
            return self._needs(started, "I need your typical monthly electricity bill to estimate it.", call)

        table = selection.table
        usage, capped = usage_from_bill(table, Decimal(str(onboarding.avg_monthly_bill_inr)))
        warnings = list(calculate_bill(usage, table).warnings)
        if capped:
            warnings.append("Your stated bill is above anything this tariff can reproduce, so usage is a lower bound.")

        what_if_asked = wants_what_if(call.question)
        scenarios, notes = ([], [])
        if what_if_asked and self._what_if_allowed:
            scenarios, notes = scenarios_for(call.question, usage, table)
        elif what_if_asked:
            warnings.append("What-if scenarios need the Plus plan. Showing your estimated bill only.")

        facts: dict[str, object] = {
            "tariff": selection.to_facts(),
            "usage_basis": {
                "source": "your stated average monthly bill",
                "stated_monthly_bill_inr": float(onboarding.avg_monthly_bill_inr),
                "estimated_import_kwh": float(usage.import_kwh),
            },
        }
        try:
            if scenarios:
                simulation = simulate(usage, scenarios, table)
                facts["bill"] = simulation.baseline_bill.to_facts()
                facts["what_if"] = simulation.to_facts()["scenarios"]
                facts["assumptions"] = assumptions_facts(scenarios)
                warnings.extend(w for w in simulation.warnings if w not in warnings)
            else:
                facts["bill"] = calculate_bill(usage, table).to_facts()
        except ScenarioError as exc:
            facts["bill"] = calculate_bill(usage, table).to_facts()
            notes.append(f"A scenario could not be applied: {exc}")
        if notes:
            facts["notes"] = notes

        bill = facts["bill"]
        assert isinstance(bill, dict)
        return AgentResult(
            agent=self.name,
            status=AgentStatus.OK,
            facts={"bill_tariff": facts},
            analytics_used=["bill_estimate", *(s.kind for s in scenarios)],
            draft=self._draft(bill, table.verified, scenarios_ran=bool(scenarios), facts=facts),
            warnings=warnings,
            confidence="medium",  # units are estimated from a stated bill, never measured
            live_advice=False,
            requires_telemetry=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _needs(self, started: float, observation: str, call: AgentCall) -> AgentResult:
        return AgentResult(
            agent=self.name,
            status=AgentStatus.PARTIAL,
            draft=AnswerParts(
                observation=observation,
                explanation="Without it any figure would be a guess, so I did not make one.",
                recommendation="Add your average monthly bill in your home setup, then ask again.",
                estimated_impact="Not enough data to estimate.",
            ),
            fallback_reason=NEEDS_BASIS_REASON,
            confidence="low",
            live_advice=False,
            requires_telemetry=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _draft(bill: dict[str, object], verified: bool, *, scenarios_ran: bool, facts: dict[str, object]) -> AnswerParts:
        label = "" if verified else " (illustrative tariff, not an official rate)"
        impact = "See the scenario figures."
        if scenarios_ran:
            first = facts["what_if"][0]  # type: ignore[index]
            impact = f"About {_inr(first['saving_inr_month'])} rupees a month."  # type: ignore[index]
        return AnswerParts(
            observation=f"Your estimated monthly bill is about {_inr(bill['total_inr'])} rupees{label}.",
            explanation=f"That is for roughly {bill['import_kwh']} units, priced on tariff {bill['tariff_id']}.",
            recommendation="Ask what a change would do, for example adding solar or a battery.",
            estimated_impact=impact,
        )
