"""
Optimizer engine tests: hand-computable optima, hard-constraint guarantees, seeded property tests,
verifier tampering, determinism and the facts round-trip. Pure and synchronous: no DB, no network.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.agents_platform.agents.optimizer import (
    ApplianceSpec,
    BatterySpec,
    Goal,
    GridSpec,
    InvalidProblemError,
    OptimizationProblem,
    PlanVerificationError,
    SlotInput,
    Status,
    baseline_totals,
    optimize,
    verify_plan,
)
from app.agents_platform.agents.optimizer import solver as solver_module
from app.agents_platform.agents.optimizer.appliances import fixed_loads
from app.agents_platform.agents.optimizer.battery import baseline_intents, build_context, execute, score
from app.agents_platform.agents.optimizer.convex_dp import ConvexBatteryDP
from app.agents_platform.agents.optimizer.grid_dp import GridBatteryDP
from app.modules.assistant.domain.grounding import check_grounding

T0 = datetime(2026, 11, 15, 0, 0)


def make_problem(
    prices,
    loads,
    *,
    solar=None,
    credit=None,
    outage=(),
    slot_minutes=60,
    battery=None,
    grid=None,
    appliances=(),
    start=T0,
    **kwargs,
) -> OptimizationProblem:
    step = timedelta(minutes=slot_minutes)
    slots = tuple(
        SlotInput(
            start=start + i * step,
            solar_kw=(solar[i] if solar else 0.0),
            base_load_kw=loads[i],
            import_price_inr_per_kwh=prices[i],
            export_credit_inr_per_kwh=(credit[i] if credit else 0.0),
            grid_available=i not in outage,
        )
        for i in range(len(prices))
    )
    return OptimizationProblem(
        slots=slots,
        grid=grid or GridSpec(sanctioned_load_kw=10.0),
        battery=battery,
        appliances=tuple(appliances),
        slot_minutes=slot_minutes,
        **kwargs,
    )


def battery(capacity=10.0, soc=50.0, charge=5.0, discharge=5.0, min_soc=10.0, rte=1.0, reserve=0.0) -> BatterySpec:
    return BatterySpec(
        usable_capacity_kwh=capacity,
        current_soc_percent=soc,
        max_charge_kw=charge,
        max_discharge_kw=discharge,
        min_soc_percent=min_soc,
        round_trip_efficiency=rte,
        backup_reserve_kwh=reserve,
    )


# ------------------------------------------------------------------------------ hand-computed optima


def test_arbitrage_charges_in_cheap_slot_and_discharges_in_expensive_slot():
    # 10 kWh battery at 50 %, floor 10 %, lossless. Slot 1 needs 6 kWh at 10 INR; discharge is capped at
    # 5 kW so the best plan stores 1 more kWh at 2 INR, discharges 5 kWh and imports 1 kWh:
    # 1*2 + 1*10 = 12 INR. Doing nothing special (no grid charging) draws 4 kWh and imports 2: 20 INR.
    problem = make_problem([2, 10], [0, 6], battery=battery(), allow_grid_charging=True)
    result = optimize(problem)
    assert result.status is Status.OPTIMAL
    assert result.violations == ()
    assert result.totals.cost_inr == pytest.approx(12.0, abs=1e-6)
    assert result.baseline.cost_inr == pytest.approx(20.0, abs=1e-6)
    assert result.savings_inr == pytest.approx(8.0, abs=1e-6)
    assert result.savings_kwh == pytest.approx(0.0, abs=1e-6)  # same kWh imported, just cheaper ones
    assert result.slots[0].action == "charge"
    assert result.slots[0].battery_charge_kw == pytest.approx(1.0, abs=1e-6)
    assert result.slots[1].battery_discharge_kw == pytest.approx(5.0, abs=1e-6)


def test_grid_charging_is_off_by_default():
    problem = make_problem([2, 10], [0, 6], battery=battery())
    result = optimize(problem)
    assert result.totals.battery_charged_kwh == pytest.approx(0.0)
    assert result.totals.cost_inr == pytest.approx(20.0, abs=1e-6)


def test_solar_surplus_charges_then_evening_discharge_serves_load():
    # Empty 4 kWh battery. 3 kWh of solar surplus at 1 INR credit, then 2 kW of evening load for 2 h at
    # 10 INR. Optimum: store all 3 kWh, discharge it, import the last 1 kWh: 10 INR, no export.
    problem = make_problem(
        [5, 5, 10, 10],
        [0, 0, 2, 2],
        solar=[3, 0, 0, 0],
        credit=[1, 1, 1, 1],
        battery=battery(capacity=4.0, soc=0.0, charge=4.0, discharge=4.0, min_soc=0.0),
    )
    result = optimize(problem)
    assert result.violations == ()
    assert result.totals.cost_inr == pytest.approx(10.0, abs=1e-6)
    assert result.totals.grid_export_kwh == pytest.approx(0.0, abs=1e-6)
    assert result.totals.battery_charged_kwh == pytest.approx(3.0, abs=1e-6)
    assert result.totals.battery_discharged_kwh == pytest.approx(3.0, abs=1e-6)
    assert result.slots[0].action == "charge"
    assert result.slots[1].action == "idle"


def test_round_trip_efficiency_is_applied():
    # rte 0.81 -> 0.9 each way. 3 kWh needed in the dear slot: 3/0.9 = 3.333 kWh stored, which takes
    # 3.333/0.9 = 3.704 kWh from the grid at 2 INR = 7.41 INR. Lossless would be 6.00.
    problem = make_problem(
        [2, 10],
        [0, 3],
        battery=battery(capacity=10.0, soc=10.0, rte=0.81),
        allow_grid_charging=True,
        soc_step_percent=0.2,
    )
    result = optimize(problem)
    assert result.violations == ()
    assert 7.40 < result.totals.cost_inr < 7.7
    # SOC moved by exactly one-way efficiency: stored = charged * 0.9
    stored = result.slots[0].soc_kwh - 1.0
    assert stored == pytest.approx(result.slots[0].battery_charge_kw * 0.9, abs=1e-6)


def test_export_credit_earns_money():
    result = optimize(make_problem([8], [1], solar=[3], credit=[3]))
    assert result.totals.grid_export_kwh == pytest.approx(2.0)
    assert result.totals.cost_inr == pytest.approx(-6.0)


def test_thirty_minute_slots_scale_energy_by_half():
    result = optimize(make_problem([10, 10], [2, 2], slot_minutes=30))
    assert result.totals.grid_import_kwh == pytest.approx(2.0)
    assert result.totals.cost_inr == pytest.approx(20.0)
    assert result.to_facts()["slot_minutes"] == 30


def test_inverter_capacity_curtails_solar_and_limits_discharge():
    problem = make_problem(
        [5, 5],
        [1, 6],
        solar=[6, 0],
        battery=battery(soc=100.0, discharge=5.0),
        grid=GridSpec(sanctioned_load_kw=10.0, inverter_capacity_kw=4.0),
    )
    result = optimize(problem)
    assert result.violations == ()
    assert result.slots[0].solar_used_kw <= 4.0 + 1e-9
    assert result.slots[0].curtailed_kw >= 2.0 - 1e-9
    assert result.slots[1].battery_discharge_kw <= 4.0 + 1e-9


# --------------------------------------------------------------------------------- battery guarantees


def test_reserve_floor_is_never_crossed():
    problem = make_problem([10] * 6, [2] * 6, battery=battery(soc=100.0, min_soc=30.0, discharge=5.0))
    result = optimize(problem)
    assert result.violations == ()
    assert min(s.soc_percent for s in result.slots) >= 30.0 - 1e-6
    assert result.slots[-1].soc_percent == pytest.approx(30.0, abs=1e-6)  # uses everything above the floor


def test_backup_reserve_raises_the_floor_above_min_soc():
    problem = make_problem([10] * 6, [2] * 6, battery=battery(soc=100.0, min_soc=10.0, reserve=6.0))
    result = optimize(problem)
    assert min(s.soc_kwh for s in result.slots) >= 6.0 - 1e-6
    assert result.meta["battery_reserve_kwh"] == pytest.approx(6.0)


def test_battery_below_reserve_is_not_discharged_and_warns():
    problem = make_problem([10] * 3, [2] * 3, battery=battery(soc=5.0, min_soc=20.0))
    result = optimize(problem)
    assert result.violations == ()
    assert result.totals.battery_discharged_kwh == pytest.approx(0.0)
    assert any("below its reserve" in w for w in result.warnings)


def test_discharge_limit_is_respected():
    result = optimize(make_problem([10], [5], battery=battery(soc=100.0, discharge=2.0)))
    assert result.slots[0].battery_discharge_kw == pytest.approx(2.0, abs=1e-6)
    assert result.slots[0].grid_import_kw == pytest.approx(3.0, abs=1e-6)
    assert result.totals.cost_inr == pytest.approx(30.0, abs=1e-6)


def test_charge_limit_and_capacity_ceiling_are_respected():
    # 8 kW of solar into a 6 kWh battery that can only take 2 kW: it fills over three slots, never above
    # 6 kWh, then serves 2 kW of evening load for three hours.
    problem = make_problem(
        [10] * 6,
        [0, 0, 0, 2, 2, 2],
        solar=[8, 8, 8, 0, 0, 0],
        battery=battery(capacity=6.0, soc=0.0, charge=2.0, discharge=2.0, min_soc=0.0),
    )
    result = optimize(problem)
    assert all(s.battery_charge_kw <= 2.0 + 1e-9 for s in result.slots)
    assert max(s.soc_kwh for s in result.slots) <= 6.0 + 1e-9
    assert result.slots[2].soc_kwh == pytest.approx(6.0, abs=0.07)
    assert result.totals.cost_inr == pytest.approx(0.0, abs=0.7)


def test_battery_never_charges_from_grid_unless_allowed():
    problem = make_problem([1, 20, 20], [0, 3, 3], battery=battery(soc=10.0), allow_grid_charging=False)
    result = optimize(problem)
    assert result.totals.battery_charged_kwh == pytest.approx(0.0)


def test_backup_goal_keeps_a_high_reserve_and_does_not_cycle():
    prices = [2, 2, 2, 12, 12, 12]
    loads = [0.5, 0.5, 0.5, 2.0, 2.0, 2.0]
    common = dict(battery=battery(soc=40.0), allow_grid_charging=True)
    bill = optimize(make_problem(prices, loads, goal=Goal.REDUCE_BILL, **common))
    backup = optimize(make_problem(prices, loads, goal=Goal.MAXIMIZE_BACKUP, **common))
    assert backup.violations == ()
    assert bill.totals.battery_discharged_kwh > 1.0  # arbitrage cycling for the bill
    assert backup.totals.battery_discharged_kwh == pytest.approx(0.0, abs=1e-6)  # never below the reserve
    assert backup.slots[-1].soc_percent >= 80.0 - 1e-6  # refilled to the default 80 % reserve
    assert backup.meta["battery_reserve_kwh"] == pytest.approx(8.0)


def test_self_consumption_goal_exports_less_than_the_bill_goal():
    # Exporting 1 kWh earns 3.5; storing it returns 0.81 kWh later worth 4 -> 3.24 < 3.5, so the bill goal
    # exports. The self-consumption goal penalises export and import, so it stores.
    prices = [4, 4, 4, 4]
    kwargs = dict(
        solar=[3, 0, 0, 0],
        credit=[3.5] * 4,
        battery=battery(soc=10.0, rte=0.81),
    )
    loads = [0, 1, 1, 1]
    bill = optimize(make_problem(prices, loads, goal=Goal.REDUCE_BILL, **kwargs))
    selfc = optimize(make_problem(prices, loads, goal=Goal.MAXIMIZE_SELF_CONSUMPTION, **kwargs))
    assert selfc.violations == ()
    assert selfc.totals.grid_export_kwh < bill.totals.grid_export_kwh - 0.5
    assert selfc.totals.self_consumption_percent > bill.totals.self_consumption_percent


# ----------------------------------------------------------------------------- outage and zero export


def test_outage_slots_have_no_import_and_no_export():
    problem = make_problem(
        [5] * 6,
        [1.0] * 6,
        solar=[0, 0, 2, 4, 0, 0],
        outage={2, 3},
        battery=battery(soc=100.0),
    )
    result = optimize(problem)
    assert result.violations == ()
    for i in (2, 3):
        s = result.slots[i]
        assert s.grid_import_kw == 0.0 and s.grid_export_kw == 0.0 and not s.grid_available
    assert result.totals.unserved_kwh == pytest.approx(0.0, abs=1e-9)
    assert result.to_facts()["outage_windows"] == ["02:00-04:00"]


def test_solver_saves_battery_energy_for_a_coming_outage():
    # Slot 0 is dear (20 INR) and has load; the outage in slots 1-2 needs 4 kWh of the 4 kWh battery.
    # Discharging in slot 0 would save 20 INR/kWh but strand the outage load, so it must not.
    problem = make_problem(
        [20, 5, 5, 5],
        [1.0, 2.0, 2.0, 0.0],
        outage={1, 2},
        battery=battery(capacity=4.0, soc=100.0, min_soc=0.0, discharge=4.0),
    )
    result = optimize(problem)
    assert result.violations == ()
    assert result.totals.unserved_kwh == pytest.approx(0.0, abs=1e-9)
    assert result.slots[0].battery_discharge_kw == pytest.approx(0.0, abs=1e-6)


def test_load_that_cannot_be_served_in_an_outage_is_reported_not_dropped():
    problem = make_problem(
        [5] * 4, [2.0] * 4, outage={1, 2, 3}, battery=battery(capacity=4.0, soc=100.0, min_soc=0.0)
    )
    result = optimize(problem)
    assert result.status is not Status.INFEASIBLE
    assert result.violations == ()
    assert result.totals.unserved_kwh == pytest.approx(2.0 * 3 - 4.0, abs=1e-6)
    assert result.to_facts()["unserved_kwh"] == pytest.approx(2.0, abs=0.05)
    assert any("cannot be served" in w for w in result.warnings)
    assert all(s.grid_import_kw == 0.0 for s in result.slots if not s.grid_available)


def test_zero_export_curtails_surplus_instead_of_exporting():
    problem = make_problem(
        [5] * 3, [0.5] * 3, solar=[0, 4, 0], credit=[3] * 3, grid=GridSpec(sanctioned_load_kw=5.0, zero_export=True)
    )
    result = optimize(problem)
    assert result.violations == ()
    assert result.totals.grid_export_kwh == 0.0
    assert result.slots[1].curtailed_kw == pytest.approx(3.5, abs=1e-9)
    assert any("curtailed" in w for w in result.warnings)


def test_max_export_limit_is_respected():
    problem = make_problem(
        [5], [0.0], solar=[4], credit=[3], grid=GridSpec(sanctioned_load_kw=5.0, max_export_kw=1.5)
    )
    result = optimize(problem)
    assert result.slots[0].grid_export_kw == pytest.approx(1.5)
    assert result.slots[0].curtailed_kw == pytest.approx(2.5)


# ------------------------------------------------------------------------------------- appliances


def washing_machine(**kw) -> ApplianceSpec:
    base = dict(name="washing machine", rated_kw=1.0, duration_slots=2, preferred_start=0)
    base.update(kw)
    return ApplianceSpec(**base)


def test_single_appliance_moves_to_the_cheapest_window_and_saves_the_difference():
    problem = make_problem([10, 10, 2, 2, 10, 10], [0] * 6, appliances=[washing_machine()])
    result = optimize(problem)
    assert result.status is Status.OPTIMAL
    item = result.appliances[0]
    assert item.start_slot == 2 and item.shifted
    assert item.saves_inr == pytest.approx(16.0, abs=1e-6)
    assert result.totals.cost_inr == pytest.approx(4.0)
    assert result.baseline.cost_inr == pytest.approx(20.0)
    assert result.to_facts()["appliances"][0]["summary"] == "run washing machine at 02:00, saves 16 INR vs 00:00"


def test_appliance_stays_put_when_the_saving_is_not_worth_the_inconvenience():
    problem = make_problem([5.00, 5.00, 4.99, 4.99, 5.00], [0] * 5, appliances=[washing_machine()])
    result = optimize(problem)
    assert result.appliances[0].start_slot == 0 and not result.appliances[0].shifted


def test_appliance_window_is_respected_even_when_a_cheaper_slot_is_outside():
    problem = make_problem(
        [10, 10, 10, 10, 1, 1],
        [0] * 6,
        appliances=[washing_machine(earliest_start=0, latest_end=4, preferred_start=0)],
    )
    result = optimize(problem)
    start = result.appliances[0].start_slot
    assert 0 <= start and start + 2 <= 4
    assert verify_plan(problem, result) == []


def test_appliance_placement_that_exceeds_sanctioned_load_is_infeasible():
    problem = make_problem(
        [5] * 4,
        [0] * 4,
        grid=GridSpec(sanctioned_load_kw=3.0),
        appliances=[washing_machine(name="geyser", rated_kw=6.0)],
    )
    result = optimize(problem)
    assert result.status is Status.INFEASIBLE
    assert [v.code for v in result.violations] == ["appliance_unplaceable"]
    assert result.slots == ()
    facts = result.to_facts()
    assert facts["status"] == "infeasible" and facts["problems"]


def test_sanctioned_load_forces_the_appliance_into_a_slot_with_headroom():
    # base load 3 kW in slots 0-2, sanctioned 4 kW: a 2 kW appliance fits only in slots 3-4 although
    # slot 0-1 are far cheaper.
    problem = make_problem(
        [1, 1, 1, 9, 9],
        [3, 3, 3, 0, 0],
        grid=GridSpec(sanctioned_load_kw=4.0),
        appliances=[washing_machine(rated_kw=2.0, duration_slots=1, preferred_start=3)],
    )
    result = optimize(problem)
    assert result.appliances[0].start_slot in (3, 4)
    assert all(s.grid_import_kw <= 4.0 + 1e-9 for s in result.slots)
    assert result.totals.unserved_kwh == 0.0


def test_optional_appliance_that_cannot_be_placed_is_skipped_with_a_warning():
    problem = make_problem(
        [5] * 4,
        [0] * 4,
        grid=GridSpec(sanctioned_load_kw=3.0),
        appliances=[washing_machine(name="geyser", rated_kw=6.0, must_run=False)],
    )
    result = optimize(problem)
    assert result.status is not Status.INFEASIBLE
    assert not result.appliances[0].placed
    assert any("geyser" in w for w in result.warnings)


def test_critical_load_is_never_shifted():
    problem = make_problem(
        [10, 10, 2, 2, 2, 2],
        [0] * 6,
        appliances=[
            ApplianceSpec(name="dialysis", rated_kw=1.0, duration_slots=2, preferred_start=0, critical=True, latest_end=4),
        ],
    )
    result = optimize(problem)
    item = result.appliances[0]
    assert item.start_slot == 0 and not item.shifted
    assert result.slots[0].appliance_kw == 1.0 and result.slots[2].appliance_kw == 0.0
    assert result.to_facts()["appliances"][0]["summary"].endswith("(critical load, never shifted)")


def test_multiple_appliances_beat_or_tie_the_preferred_time_baseline():
    prices = [9, 9, 9, 4, 4, 4, 4, 9, 9, 9, 12, 12]
    problem = make_problem(
        prices,
        [0.3] * 12,
        grid=GridSpec(sanctioned_load_kw=3.0),
        appliances=[
            ApplianceSpec(name="washing machine", rated_kw=1.0, duration_slots=2, preferred_start=10),
            ApplianceSpec(name="dishwasher", rated_kw=1.5, duration_slots=2, preferred_start=10),
            ApplianceSpec(name="geyser", rated_kw=1.5, duration_slots=1, preferred_start=11),
        ],
    )
    result = optimize(problem)
    assert result.status is Status.FEASIBLE_HEURISTIC
    assert result.totals.cost_inr <= result.baseline.cost_inr + 1e-9
    assert result.savings_inr > 5.0
    assert all(s.grid_import_kw <= 3.0 + 1e-9 for s in result.slots)
    assert result.totals.unserved_kwh == 0.0


def test_single_appliance_start_is_optimal_against_every_alternative_start():
    # Force the appliance (as a critical load) to each feasible start in turn and re-solve everything else
    # optimally: no forced start may beat the solver's own choice.
    for seed in range(6):
        rng = random.Random(100 + seed)
        n = 16
        prices = [rng.uniform(3, 12) for _ in range(n)]
        solar = [max(0.0, 3.0 * math.sin(math.pi * (i - 3) / 9)) if 3 <= i <= 12 else 0.0 for i in range(n)]
        spec = ApplianceSpec(name="wm", rated_kw=rng.uniform(0.8, 2.0), duration_slots=rng.randint(1, 3), preferred_start=1,
                             latest_end=n)
        common = dict(solar=solar, credit=[2.0] * n, battery=battery(soc=rng.uniform(15, 90), rte=0.9),
                      allow_grid_charging=rng.random() < 0.5, min_shift_saving_inr=0.0)
        chosen = optimize(make_problem(prices, [0.4] * n, appliances=[spec], **common))
        assert chosen.violations == ()
        objectives = []
        for start in spec.feasible_starts(n):
            forced = replace(spec, critical=True, preferred_start=start)
            objectives.append(optimize(make_problem(prices, [0.4] * n, appliances=[forced], **common)).meta["objective_inr_equivalent"])
        assert chosen.meta["objective_inr_equivalent"] <= min(objectives) + 1e-3, seed


def test_appliance_order_does_not_change_the_plan():
    specs = [
        ApplianceSpec(name="a", rated_kw=1.0, duration_slots=2, preferred_start=8),
        ApplianceSpec(name="b", rated_kw=1.0, duration_slots=1, preferred_start=9),
    ]
    prices = [9, 9, 3, 3, 3, 9, 9, 9, 9, 9, 9, 9]
    one = optimize(make_problem(prices, [0] * 12, appliances=specs))
    two = optimize(make_problem(prices, [0] * 12, appliances=specs[::-1]))
    assert {(a.name, a.start_slot) for a in one.appliances} == {(a.name, a.start_slot) for a in two.appliances}


# ------------------------------------------------------------------------------------ property tests


def random_problem(seed: int) -> OptimizationProblem:
    rng = random.Random(seed)
    slot_minutes = rng.choice([60, 60, 60, 30])
    n = 24 if slot_minutes == 60 else 36
    hours = [i * slot_minutes / 60.0 for i in range(n)]
    peak = rng.uniform(0.0, 6.0)
    solar = [max(0.0, peak * math.sin(math.pi * (h - 6) / 12) * rng.uniform(0.6, 1.0)) if 6 <= h <= 18 else 0.0 for h in hours]
    loads = [rng.uniform(0.2, 0.8) + (rng.uniform(0.3, 1.2) if 18 <= h < 23 else 0.0) for h in hours]
    prices = [(4.0 if h < 6 else 7.0 if h < 18 else 11.0) * rng.uniform(0.9, 1.1) for h in hours]
    credit = rng.choice([0.0, 0.0, 2.5, 3.5])
    outage = set()
    if rng.random() < 0.3:
        first = rng.randrange(0, n - 3)
        outage = set(range(first, first + rng.randint(1, 3)))
    bat = None
    if rng.random() < 0.85:
        cap = rng.uniform(3.0, 15.0)
        bat = BatterySpec(
            usable_capacity_kwh=cap,
            current_soc_percent=rng.uniform(0.0, 100.0),
            min_soc_percent=rng.uniform(0.0, 30.0),
            max_charge_kw=rng.uniform(1.5, 5.0),
            max_discharge_kw=rng.uniform(1.5, 5.0),
            round_trip_efficiency=rng.uniform(0.8, 0.95),
            backup_reserve_kwh=rng.uniform(0.0, 0.7 * cap) if rng.random() < 0.3 else 0.0,
        )
    grid = GridSpec(
        sanctioned_load_kw=rng.uniform(4.0, 10.0),
        zero_export=rng.random() < 0.3,
        max_export_kw=rng.choice([None, None, 2.0]),
        inverter_capacity_kw=rng.choice([None, None, rng.uniform(2.0, 6.0)]),
    )
    specs = []
    for k in range(rng.randint(0, 2)):
        duration = rng.randint(1, 3)
        lo = rng.randrange(0, n - duration)
        hi = rng.randint(lo + duration, n)
        specs.append(
            ApplianceSpec(
                name=f"app{k}",
                rated_kw=rng.uniform(0.5, 2.5),
                duration_slots=duration,
                earliest_start=lo,
                latest_end=hi,
                preferred_start=rng.randint(lo, hi - duration),
                must_run=rng.random() < 0.7,
            )
        )
    if rng.random() < 0.2:
        specs.append(ApplianceSpec(name="fridge-block", rated_kw=0.3, duration_slots=2, preferred_start=3, critical=True))
    return make_problem(
        prices,
        loads,
        solar=solar,
        credit=[credit] * n,
        outage=outage,
        slot_minutes=slot_minutes,
        battery=bat,
        grid=grid,
        appliances=specs,
        goal=rng.choice(list(Goal)),
        allow_grid_charging=rng.random() < 0.5,
        soc_step_percent=5.0,
    )


@pytest.mark.parametrize("block", range(4))
def test_random_scenarios_never_violate_a_constraint_and_never_lose_to_baseline(block):
    feasible = 0
    for seed in range(block * 50, block * 50 + 50):
        problem = random_problem(seed)
        result = optimize(problem)
        if result.status is Status.INFEASIBLE:
            # the only legitimate reason is an appliance that cannot be placed, never a plan violation
            assert {v.code for v in result.violations} == {"appliance_unplaceable"}, seed
            continue
        feasible += 1
        assert result.violations == (), seed
        assert verify_plan(problem, result) == [], seed
        battery_spec = problem.battery
        if battery_spec is not None:
            cap = battery_spec.usable_capacity_kwh
            for s in result.slots:
                assert -1e-9 <= s.soc_kwh <= cap + 1e-9, seed
        for s, slot in zip(result.slots, problem.slots):
            if not slot.grid_available:
                assert s.grid_import_kw == 0.0 and s.grid_export_kw == 0.0, seed
            assert s.grid_import_kw <= problem.grid.sanctioned_load_kw + 1e-9, seed
            if problem.grid.zero_export:
                assert s.grid_export_kw == 0.0, seed
        # objective never worse than doing nothing; money never worse for the bill goal
        if problem.goal is not Goal.MAXIMIZE_BACKUP:  # the baseline cycles on-grid, which backup forbids
            assert result.meta["objective_inr_equivalent"] <= result.meta["baseline_objective_inr_equivalent"] + 1e-3, seed
        if problem.goal is Goal.REDUCE_BILL and result.baseline.unserved_kwh <= 1e-9:
            assert result.totals.cost_inr <= result.baseline.cost_inr + 1e-6, seed
            assert result.savings_inr >= -1e-6, seed
        # the independent baseline recomputation agrees with the solver's
        independent = baseline_totals(problem, {a.name for a in result.appliances if a.placed})
        assert independent["cost_inr"] == pytest.approx(result.baseline.cost_inr, abs=1e-6), seed
    assert feasible >= 35  # the generator must mostly produce feasible problems or the test proves little


def test_determinism_same_input_same_output():
    problem = random_problem(7)
    first = optimize(problem).to_dict()
    second = optimize(random_problem(7)).to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --------------------------------------------------------------------------------- engine cross-checks


def battery_seeds(count: int) -> list[int]:
    """Seeds of random problems with a battery that starts at or above its reserve (exact engine applies)."""
    out = []
    for seed in range(400):
        problem = random_problem(seed)
        if problem.battery is None:
            continue
        params = build_context(problem).params
        if params is not None and params.e0_kwh >= params.floor_kwh:
            out.append(seed)
        if len(out) == count:
            break
    return out


def test_exact_engine_value_equals_the_executed_plan_objective():
    for seed in battery_seeds(25):
        problem = random_problem(seed)
        ctx = build_context(problem)
        loads = fixed_loads(problem)
        engine = ConvexBatteryDP(ctx)
        assert score(ctx, engine.run(loads)) == pytest.approx(engine.value(loads), rel=1e-7, abs=1e-6), seed


def test_exact_engine_is_never_beaten_by_a_random_feasible_plan_or_the_default_policy():
    rng = random.Random(11)
    for seed in battery_seeds(12):
        problem = random_problem(seed)
        ctx = build_context(problem)
        loads = fixed_loads(problem)
        best = score(ctx, ConvexBatteryDP(ctx).run(loads))
        hold = problem.goal is Goal.MAXIMIZE_BACKUP
        default = execute(ctx, loads, baseline_intents(ctx, loads), allow_grid_charging=False, respect_hold=hold)
        assert best <= score(ctx, default) + 1e-6, seed
        for _ in range(150):
            intents = [rng.choice([0.0, rng.uniform(-5, 5), rng.uniform(-1, 1)]) for _ in loads]
            trial = execute(ctx, loads, intents, allow_grid_charging=problem.allow_grid_charging, respect_hold=True)
            assert best <= score(ctx, trial) + 1e-6, seed


def test_grid_engine_agrees_with_the_exact_engine_within_discretisation_error():
    for seed in battery_seeds(10):
        problem = random_problem(seed)
        ctx = build_context(problem)
        loads = fixed_loads(problem)
        exact = score(ctx, ConvexBatteryDP(ctx).run(loads))
        grid = score(ctx, GridBatteryDP(ctx, 0.5).run(loads))
        assert exact <= grid + 1e-6, seed  # the exact engine can only be better
        assert grid - exact <= 0.06 * (abs(exact) + 1.0), seed  # and a fine grid is close


def test_grid_engine_can_be_selected_and_still_passes_verification():
    problem = make_problem(
        [2, 10, 10], [0, 3, 3], battery=battery(), allow_grid_charging=True, battery_engine="grid", soc_step_percent=1.0
    )
    result = optimize(problem)
    assert result.violations == () and result.meta["battery_engine"] == "grid"
    assert result.status is Status.FEASIBLE_HEURISTIC  # grid engine is near-optimal, never labelled optimal
    exact = optimize(replace(problem, battery_engine="exact"))
    assert exact.meta["battery_engine"] == "exact" and exact.status is Status.OPTIMAL
    assert exact.totals.cost_inr <= result.totals.cost_inr + 1e-6


def test_battery_below_reserve_falls_back_to_the_grid_engine():
    problem = make_problem([5] * 4, [1] * 4, solar=[0, 3, 3, 0], battery=battery(soc=5.0, min_soc=20.0))
    result = optimize(problem)
    assert result.meta["battery_engine"] == "grid" and result.violations == ()
    assert result.slots[2].soc_kwh > result.slots[0].soc_kwh  # it recharges from solar surplus


# ----------------------------------------------------------------------------------------- verifier


def tampered(problem, result, index, **changes):
    slots = list(result.slots)
    slots[index] = replace(slots[index], **changes)
    return replace(result, slots=tuple(slots))


def codes(problem, result):
    return {v.code for v in verify_plan(problem, result)}


@pytest.fixture
def outage_case():
    problem = make_problem(
        [5] * 4,
        [1.0] * 4,
        solar=[0, 2, 0, 0],
        outage={1, 2},
        battery=battery(soc=100.0),
        grid=GridSpec(sanctioned_load_kw=4.0, zero_export=True),
        appliances=[washing_machine(duration_slots=1, preferred_start=3, latest_end=4)],
    )
    return problem, optimize(problem)


def test_verifier_accepts_the_solvers_plan(outage_case):
    problem, result = outage_case
    assert result.violations == () and codes(problem, result) == set()


def test_verifier_catches_tampered_plans(outage_case):
    problem, result = outage_case
    assert "outage_grid_flow" in codes(problem, tampered(problem, result, 1, grid_import_kw=0.5))
    assert "zero_export" in codes(problem, tampered(problem, result, 0, grid_export_kw=0.3))
    assert "sanctioned_load" in codes(problem, tampered(problem, result, 0, grid_import_kw=9.0))
    assert "discharge_limit" in codes(problem, tampered(problem, result, 1, battery_discharge_kw=9.0))
    assert "soc_mismatch" in codes(problem, tampered(problem, result, 1, soc_kwh=9.99))
    assert "energy_balance" in codes(problem, tampered(problem, result, 0, curtailed_kw=0.7))
    assert "charge_limit" in codes(problem, tampered(problem, result, 0, battery_charge_kw=9.0))
    assert "cost_mismatch" in codes(problem, tampered(problem, result, 0, cost_inr=123.0))


def test_verifier_catches_on_grid_cycling_under_the_backup_goal():
    problem = make_problem([5, 5], [1, 1], battery=battery(soc=100.0), goal=Goal.MAXIMIZE_BACKUP)
    result = optimize(problem)
    assert result.violations == () and result.totals.battery_discharged_kwh == 0.0
    cycled = tampered(problem, result, 0, battery_discharge_kw=1.0, grid_import_kw=0.0, cost_inr=0.0)
    assert "backup_cycling" in codes(problem, cycled)


def test_verifier_catches_a_discharge_below_the_reserve():
    problem = make_problem([5], [5], battery=battery(capacity=4.0, soc=100.0, min_soc=50.0))
    result = optimize(problem)
    assert result.violations == ()
    assert result.slots[0].battery_discharge_kw == pytest.approx(2.0, abs=0.05)
    assert "soc_below_reserve" in codes(problem, tampered(problem, result, 0, battery_discharge_kw=4.0))


def test_verifier_catches_appliance_violations():
    problem = make_problem(
        [5] * 6,
        [0] * 6,
        appliances=[
            washing_machine(duration_slots=2, earliest_start=1, latest_end=5, preferred_start=1),
            ApplianceSpec(name="fridge", rated_kw=0.5, duration_slots=1, preferred_start=0, critical=True),
        ],
    )
    result = optimize(problem)
    assert result.violations == ()
    wm = next(a for a in result.appliances if a.name == "washing machine")
    fridge = next(a for a in result.appliances if a.name == "fridge")

    def with_appliance(old, new):
        return replace(result, appliances=tuple(new if a is old else a for a in result.appliances))

    early = replace(wm, start_slot=0, start=problem.slots[0].start)
    assert "appliance_window" in codes(problem, with_appliance(wm, early))
    late = replace(wm, start_slot=4, start=problem.slots[4].start)
    assert "appliance_window" in codes(problem, with_appliance(wm, late))
    moved = replace(fridge, start_slot=3, start=problem.slots[3].start)
    assert "critical_shifted" in codes(problem, with_appliance(fridge, moved))
    gone = replace(wm, placed=False, start_slot=None, start=None, end=None)
    assert "appliance_not_placed" in codes(problem, with_appliance(wm, gone))


def test_solver_runs_the_verifier_and_refuses_a_broken_plan(monkeypatch):
    problem = make_problem([5, 5], [1, 1], battery=battery(soc=50.0))
    real_assemble = solver_module._assemble

    def corrupt(*args, **kwargs):
        result = real_assemble(*args, **kwargs)
        slots = list(result.slots)
        slots[0] = replace(slots[0], grid_import_kw=slots[0].grid_import_kw + 50.0)  # breaks sanctioned load + balance
        return replace(result, slots=tuple(slots))

    monkeypatch.setattr(solver_module, "_assemble", corrupt)
    result = optimize(problem)
    assert result.status is Status.INFEASIBLE
    assert result.slots == () and result.violations
    assert "sanctioned_load" in {v.code for v in result.violations}
    with pytest.raises(PlanVerificationError) as raised:
        optimize(problem, strict=True)
    assert raised.value.violations


# ---------------------------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "build",
    [
        lambda: SlotInput(start=T0, solar_kw=-1, base_load_kw=0, import_price_inr_per_kwh=5),
        lambda: SlotInput(start=T0, solar_kw=0, base_load_kw=0, import_price_inr_per_kwh=float("nan")),
        lambda: battery(soc=120.0),
        lambda: battery(rte=1.2),
        lambda: GridSpec(sanctioned_load_kw=-1),
        lambda: ApplianceSpec(name="x", rated_kw=1, duration_slots=0),
        lambda: make_problem([5, 5], [1, 1], appliances=[washing_machine(duration_slots=3)]),
        lambda: make_problem([5, 5], [1, 1], appliances=[washing_machine(latest_end=5)]),
        lambda: make_problem([5, 5], [1, 1], appliances=[washing_machine(preferred_start=1)]),
        lambda: make_problem([5, 5], [1, 1], appliances=[washing_machine(), washing_machine()]),
        lambda: make_problem([5, 5], [1, 1], slot_minutes=45),
        lambda: OptimizationProblem(slots=(), grid=GridSpec(sanctioned_load_kw=1)),
    ],
)
def test_invalid_problems_are_rejected(build):
    with pytest.raises(InvalidProblemError):
        build()


def test_non_contiguous_slots_are_rejected():
    slots = (
        SlotInput(start=T0, solar_kw=0, base_load_kw=1, import_price_inr_per_kwh=5),
        SlotInput(start=T0 + timedelta(hours=2), solar_kw=0, base_load_kw=1, import_price_inr_per_kwh=5),
    )
    with pytest.raises(InvalidProblemError):
        OptimizationProblem(slots=slots, grid=GridSpec(sanctioned_load_kw=5))


def test_no_battery_problem_is_solved():
    result = optimize(make_problem([5, 6], [1, 1]))
    assert result.totals.final_soc_percent is None
    assert "battery" not in result.to_facts()


# ---------------------------------------------------------------------------------------- facts


def test_to_facts_round_trips_through_json_and_carries_the_numbers_an_answer_needs():
    problem = make_problem(
        [5] * 6 + [9, 9, 9, 9, 9, 9, 4, 4, 4, 4, 9, 9, 12, 12, 12, 12, 12, 12],
        [0.4] * 24,
        solar=[0] * 8 + [1, 2, 3, 3.5, 3.5, 3, 2, 1] + [0] * 8,
        battery=battery(capacity=8.0, soc=20.0, charge=3.0, discharge=3.0, min_soc=10.0, rte=0.9),
        appliances=[washing_machine(preferred_start=19, duration_slots=2, rated_kw=1.2)],
        soc_step_percent=2.0,
    )
    result = optimize(problem)
    assert result.violations == ()
    facts = result.to_facts()
    assert json.loads(json.dumps(facts)) == facts
    assert json.loads(json.dumps(result.to_dict()))["status"] == result.status.value
    assert facts["savings_inr"] == pytest.approx(result.savings_inr, abs=0.005)
    assert facts["plan_cost_inr"] == pytest.approx(result.totals.cost_inr, abs=0.005)
    assert facts["import_reduction_kwh"] == pytest.approx(result.savings_kwh, abs=0.05)
    assert facts["battery"]["charge_windows"], "solar surplus should charge the battery"
    window = facts["battery"]["charge_windows"][0]["window"]
    assert len(window) == 11 and window[2] == ":" and window[5] == "-"
    assert any(line.startswith("charge battery ") for line in facts["plan_summary"])
    app = facts["appliances"][0]
    assert app["name"] == "washing machine" and "start" in app and app["preferred_start"] == "19:00"
    # an answer that quotes the summary numbers is grounded; an invented number is not
    saves = round(app.get("saves_inr", 0.0))
    answer = {"recommendation": f"Run the washing machine at {app['start']} to save {saves} INR versus 19:00."}
    assert check_grounding(answer, facts=[facts]).ok
    invented = {"recommendation": "You will save 9876 INR."}
    assert not check_grounding(invented, facts=[facts]).ok


def test_to_facts_is_compact_and_does_not_leak_per_slot_detail():
    result = optimize(random_problem(3))
    facts = result.to_facts()
    assert len(json.dumps(facts)) < 6000
    assert "slots" not in facts
