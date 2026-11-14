"""
Optimizer engine: deterministic schedule solver and battery strategy (pure, no DB, no network).

The agent layer maps telemetry, the forecast and the tariff onto `OptimizationProblem`, calls
`optimize(...)`, and feeds `OptimizationResult.to_facts()` to the answer prompt. The solver runs the
independent verifier (`verify_plan`) on its own output and never returns a plan with a violation.
"""
from .models import (
    ApplianceSchedule,
    ApplianceSpec,
    BatterySpec,
    Goal,
    GoalWeights,
    GridSpec,
    InvalidProblemError,
    OptimizationProblem,
    OptimizationResult,
    PlanTotals,
    PlanVerificationError,
    SlotInput,
    SlotPlan,
    Status,
    Violation,
)
from .solver import optimize
from .verify import baseline_totals, verify_plan

__all__ = [
    "ApplianceSchedule",
    "ApplianceSpec",
    "BatterySpec",
    "Goal",
    "GoalWeights",
    "GridSpec",
    "InvalidProblemError",
    "OptimizationProblem",
    "OptimizationResult",
    "PlanTotals",
    "PlanVerificationError",
    "SlotInput",
    "SlotPlan",
    "Status",
    "Violation",
    "baseline_totals",
    "optimize",
    "verify_plan",
]
