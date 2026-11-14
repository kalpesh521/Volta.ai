"""
Bill & tariff engine: versioned tariff tables, an itemised bill calculator and a what-if simulator.

The model never does arithmetic: every figure an answer may quote is a field (or a `to_facts()` entry).
The shipped tables are ILLUSTRATIVE and marked `verified=false`; every output carries that flag.
"""
from app.agents_platform.agents.bill_tariff.calculator import UNVERIFIED_WARNING, calculate_bill
from app.agents_platform.agents.bill_tariff.models import (
    BillBreakdown,
    BillLine,
    ExportRules,
    MonthlyUsage,
    Slab,
    TariffTable,
    TodBand,
)
from app.agents_platform.agents.bill_tariff.tables import (
    TariffLoadError,
    TariffNotFound,
    TariffRegistry,
    TariffSelection,
    default_registry,
    flat_table,
    load_tariff_file,
    table_for,
)
from app.agents_platform.agents.bill_tariff.whatif import (
    AddBattery,
    AddSolar,
    ChangeTariff,
    ReduceConsumption,
    Scenario,
    ScenarioError,
    ScenarioResult,
    ShiftLoad,
    SimulationResult,
    simulate,
)

__all__ = [
    "UNVERIFIED_WARNING", "calculate_bill", "BillBreakdown", "BillLine", "ExportRules", "MonthlyUsage", "Slab",
    "TariffTable", "TodBand", "TariffLoadError", "TariffNotFound", "TariffRegistry", "TariffSelection",
    "default_registry", "flat_table", "load_tariff_file", "table_for", "AddBattery", "AddSolar", "ChangeTariff",
    "ReduceConsumption", "Scenario", "ScenarioError", "ScenarioResult", "ShiftLoad", "SimulationResult", "simulate",
]
