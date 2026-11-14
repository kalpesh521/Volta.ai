"""
Bill & tariff engine: schema/loader validation, tariff selection, bill arithmetic and the what-if simulator.

Every expected bill below is computed BY HAND in the comment next to it, from the small `table()` fixture
(slabs 100@4, next 200@6, rest@8; fixed 100; FAC 0.5/kWh; duty 10% of energy). Do not "fix" a failing
expectation by re-running the code: re-derive it.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents_platform.agents.bill_tariff import (
    AddBattery,
    AddSolar,
    ChangeTariff,
    MonthlyUsage,
    ReduceConsumption,
    ScenarioError,
    ShiftLoad,
    TariffLoadError,
    TariffRegistry,
    TariffTable,
    calculate_bill,
    default_registry,
    flat_table,
    load_tariff_file,
    simulate,
    table_for,
)
from app.agents_platform.agents.bill_tariff.calculator import UNVERIFIED_WARNING
from app.agents_platform.agents.bill_tariff.tables import TARIFF_DIR, TariffNotFound

D = Decimal

TOD = [
    {"name": "night", "start": "22:00", "end": "06:00", "multiplier": 0.5},
    {"name": "day", "start": "06:00", "end": "18:00", "multiplier": 1.0},
    {"name": "peak", "start": "18:00", "end": "22:00", "multiplier": 2.0},
]


def raw_table(**overrides) -> dict:
    base = {
        "id": "test-table",
        "version": "1",
        "discom": "Testco",
        "category": "residential",
        "effective_from": "2025-01-01",
        "effective_to": None,
        "currency": "INR",
        "slabs": [
            {"up_to_units": 100, "rate": 4},
            {"up_to_units": 300, "rate": 6},
            {"up_to_units": None, "rate": 8},
        ],
        "fixed_charge_per_month": 100,
        "fuel_adjustment_per_kwh": 0.5,
        "duty_percent": 10,
        "export": {"mode": "none", "credit_per_kwh": 0, "bank_units": False},
        "source": "test fixture",
        "verified": True,
    }
    base.update(overrides)
    return base


def table(**overrides) -> TariffTable:
    return TariffTable.model_validate(raw_table(**overrides))


def usage(imp, **kw) -> MonthlyUsage:
    return MonthlyUsage(import_kwh=imp, **kw)


def total(imp, tbl=None, **kw) -> Decimal:
    return calculate_bill(usage(imp, **kw), tbl or table()).total_inr


def write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


# --------------------------------------------------------------------------- hand-computed bills


def test_exactly_at_first_slab_limit():
    # 100 units: energy 100*4=400; fixed 100; FAC 100*0.5=50; duty 10% of 400=40 -> 590
    b = calculate_bill(usage(100), table())
    assert [ln.code for ln in b.lines if ln.code.startswith("energy")] == ["energy_slab_1"]
    assert (b.energy_charge_inr, b.fuel_adjustment_inr, b.duty_inr, b.total_inr) == (D(400), D(50), D(40), D(590))


def test_one_unit_over_slab_limit():
    # 101: energy 400+1*6=406; fixed 100; FAC 50.50; duty 40.60 -> 597.10
    b = calculate_bill(usage(101), table())
    assert b.energy_charge_inr == D(406)
    assert b.total_inr == D("597.10")
    assert [ln.units_kwh for ln in b.lines if ln.code.startswith("energy")] == [D(100), D(1)]


def test_exactly_at_second_limit_and_one_over():
    # 300: energy 400+200*6=1600; FAC 150; duty 160; fixed 100 -> 2010
    assert total(300) == D(2010)
    # 301: energy 1600+8=1608; FAC 150.50; duty 160.80; fixed 100 -> 2019.30
    assert total(301) == D("2019.30")


def test_zero_usage_is_fixed_charge_only():
    b = calculate_bill(usage(0), table())
    assert b.total_inr == D(100)
    assert [ln.code for ln in b.lines] == ["fixed_charge"]
    assert b.effective_rate_inr_per_kwh is None


def test_fractional_units():
    # 100.5 units: 400 + 0.5*6 = 403; FAC 50.25; duty 40.30; fixed 100 -> 593.55
    assert total(D("100.5")) == D("593.55")


def test_proration_scales_fixed_charge_and_slab_limits():
    # 15 days => factor 0.5: slab limits 50/150, fixed 50. 60 units: 50*4 + 10*6 = 260; FAC 30; duty 26 -> 366
    b = calculate_bill(usage(60, days=15), table())
    assert b.proration_factor == D("0.5")
    assert b.fixed_charge_inr == D(50)
    assert b.total_inr == D(366)
    # 50 units sits exactly on the prorated first limit: 200 + fixed 50 + FAC 25 + duty 20 = 295
    assert total(50, days=15) == D(295)


def test_thirty_days_is_not_prorated():
    assert calculate_bill(usage(100, days=30), table()).proration_factor == D(1)


def test_duty_base_is_energy_charge_only():
    b = calculate_bill(usage(100), table())
    # Not 10% of the 590 total, not of energy+fixed: exactly 10% of the 400 energy line.
    assert b.duty_inr == D(40)
    no_fixed = calculate_bill(usage(100), table(fixed_charge_per_month=0, fuel_adjustment_per_kwh=0))
    assert no_fixed.duty_inr == D(40)


def test_sanctioned_load_charge_prorated_and_warns_when_missing():
    t = table(sanctioned_load_charge_per_kw=20, fixed_charge_per_month=0, fuel_adjustment_per_kwh=0, duty_percent=0)
    assert calculate_bill(usage(0, sanctioned_load_kw=5), t).total_inr == D(100)
    assert calculate_bill(usage(0, sanctioned_load_kw=5, days=15), t).total_inr == D(50)
    missing = calculate_bill(usage(0), t)
    assert missing.total_inr == D(0)
    assert any("Sanctioned-load" in w for w in missing.warnings)


def test_rounding_is_half_up_to_the_paisa():
    # 1 unit at 0.005 = 0.005 -> 0.01 (half-up). Banker's rounding would give 0.00.
    t = table(slabs=[{"up_to_units": None, "rate": 0.005}], fixed_charge_per_month=0, fuel_adjustment_per_kwh=0, duty_percent=0)
    assert calculate_bill(usage(1), t).total_inr == D("0.01")
    # 2.675 is the classic float trap: Decimal keeps it exact, so it rounds to 2.68.
    t2 = table(slabs=[{"up_to_units": None, "rate": 2.675}], fixed_charge_per_month=0, fuel_adjustment_per_kwh=0, duty_percent=0)
    assert calculate_bill(usage(1), t2).total_inr == D("2.68")


def test_lines_always_sum_to_total():
    for imp in (0, 1, 99, 100, 101, 250, 300, 301, 777):
        b = calculate_bill(usage(imp, import_by_band={"night": imp / 2, "day": imp / 2, "peak": 0}), table(tod_bands=TOD))
        assert sum(ln.amount_inr for ln in b.lines) == b.total_inr


# --------------------------------------------------------------------------- ToD


def test_tod_adjustment_hand_computed():
    # 200 units (night 50, day 100, peak 50). Slab energy 100*4+100*6=1000, avg rate 5.
    # adjustment = 5*(50*(0.5-1) + 100*0 + 50*(2-1)) = 5*25 = 125 -> energy 1125.
    # fixed 100, FAC 100, duty 10% of 1125 = 112.50 -> 1437.50
    t = table(tod_bands=TOD)
    b = calculate_bill(usage(200, import_by_band={"night": 50, "day": 100, "peak": 50}), t)
    assert b.energy_charge_inr == D(1125)
    assert b.duty_inr == D("112.50")
    assert b.total_inr == D("1437.50")
    assert "tod_adjustment" in [ln.code for ln in b.lines]


def test_tod_without_band_split_is_not_applied_and_warns():
    b = calculate_bill(usage(200), table(tod_bands=TOD))
    assert b.energy_charge_inr == D(1000)
    assert any("not split by band" in w for w in b.warnings)


def test_band_split_on_flat_table_is_ignored_with_warning():
    b = calculate_bill(usage(200, import_by_band={"night": 200}), table())
    assert b.energy_charge_inr == D(1000)
    assert any("no time-of-day bands" in w for w in b.warnings)


def test_unknown_band_name_is_rejected():
    with pytest.raises(ValueError, match="does not define"):
        calculate_bill(usage(10, import_by_band={"midnight": 10}), table(tod_bands=TOD))


def test_band_split_must_match_import():
    with pytest.raises(ValidationError, match="sums to"):
        usage(100, import_by_band={"night": 10, "day": 10})


# --------------------------------------------------------------------------- export, banking


def nm(bank: bool, credit: float = 0) -> dict:
    return {"mode": "net_metering", "credit_per_kwh": credit, "bank_units": bank}


def test_net_metering_offsets_units_one_for_one():
    # import 200, export 150 -> bill 50 units: 200 + fixed 100 + FAC 25 + duty 20 = 345
    b = calculate_bill(usage(200, export_kwh=150), table(export=nm(True)))
    assert b.billed_units_kwh == D(50)
    assert b.total_inr == D(345)
    assert b.banked_units_out_kwh == D(0)


def test_net_metering_banks_surplus():
    # export 300 vs import 200 -> 0 billed, 100 banked, bill = fixed only
    b = calculate_bill(usage(200, export_kwh=300), table(export=nm(True)))
    assert b.billed_units_kwh == D(0)
    assert b.banked_units_out_kwh == D(100)
    assert b.total_inr == D(100)


def test_banked_units_are_consumed_next_month():
    # opening 100 banked, import 150, export 0 -> bill 50 units (345), bank empty
    b = calculate_bill(usage(150, banked_units_in=100), table(export=nm(True)))
    assert b.billed_units_kwh == D(50)
    assert b.banked_units_out_kwh == D(0)
    assert b.total_inr == D(345)


def test_opening_bank_ignored_when_tariff_does_not_bank():
    b = calculate_bill(usage(150, banked_units_in=100), table(export=nm(False)))
    assert b.billed_units_kwh == D(150)
    assert any("banked units ignored" in w for w in b.warnings)


def test_net_metering_without_banking_pays_surplus_and_credit_cannot_make_bill_negative():
    # export 300, import 200: surplus 100 * Rs2 = 200 credit; subtotal = fixed 100 -> applied 100, 100 lapses
    b = calculate_bill(usage(200, export_kwh=300), table(export=nm(False, credit=2)))
    assert b.export_credit_inr == D(100)
    assert b.credit_lapsed_inr == D(100)
    assert b.total_inr == D(0)


def test_net_billing_credits_exports_against_full_import_bill():
    # import 100 -> 590; export 50 * 3 = 150 credit -> 440
    b = calculate_bill(usage(100, export_kwh=50), table(export={"mode": "net_billing", "credit_per_kwh": 3, "bank_units": False}))
    assert b.billed_units_kwh == D(100)
    assert b.export_credit_inr == D(150)
    assert b.total_inr == D(440)


def test_net_billing_carries_unused_credit_when_banking():
    # import 0, export 100 * 3 = 300 credit vs fixed 100: applied 100, carried 200
    nb = {"mode": "net_billing", "credit_per_kwh": 3, "bank_units": True}
    b = calculate_bill(usage(0, export_kwh=100), table(export=nb))
    assert (b.total_inr, b.carried_credit_out_inr, b.credit_lapsed_inr) == (D(0), D(200), D(0))
    # next month the carried 200 is applied to a 590 bill -> 390
    nxt = calculate_bill(usage(100, carried_credit_in_inr=200), table(export=nb))
    assert nxt.total_inr == D(390)


def test_export_mode_none_pays_nothing():
    assert total(100, export_kwh=500) == D(590)


# --------------------------------------------------------------------------- monotonicity


@pytest.mark.parametrize("tbl", default_registry().tables, ids=lambda t: f"{t.id}-{t.version}")
def test_more_import_never_lowers_the_bill(tbl):
    previous = D(-1)
    for imp in range(0, 801, 7):
        bands = {n: D(imp) / len(tbl.tod_bands) for n in tbl.band_names} if tbl.tod_bands else None
        cost = calculate_bill(usage(imp, import_by_band=bands, sanctioned_load_kw=5), tbl).total_inr
        assert cost >= previous, f"{tbl.id}: bill fell at {imp} units"
        previous = cost


# --------------------------------------------------------------------------- schema / loader rejections


def bad(tmp_path, mutate, match):
    data = raw_table()
    mutate(data)
    with pytest.raises(TariffLoadError, match=match):
        load_tariff_file(write(tmp_path, "bad.json", data))


def test_valid_file_loads_with_exact_decimals(tmp_path):
    t = load_tariff_file(write(tmp_path, "ok.json", '{"id":"x","version":"1","discom":"d","category":"c",'
                               '"effective_from":"2025-01-01","slabs":[{"up_to_units":null,"rate":4.35}],"source":"s"}'))
    assert t.slabs[0].rate == D("4.35")
    assert t.verified is False  # default is the safe one


def test_rejects_non_increasing_slabs(tmp_path):
    bad(tmp_path, lambda d: d.update(slabs=[{"up_to_units": 300, "rate": 4}, {"up_to_units": 100, "rate": 5}, {"up_to_units": None, "rate": 6}]), "strictly greater")
    bad(tmp_path, lambda d: d.update(slabs=[{"up_to_units": 100, "rate": 4}, {"up_to_units": 100, "rate": 5}, {"up_to_units": None, "rate": 6}]), "strictly greater")


def test_rejects_slab_without_open_final(tmp_path):
    bad(tmp_path, lambda d: d.update(slabs=[{"up_to_units": 100, "rate": 4}, {"up_to_units": 200, "rate": 5}]), "open-ended")


def test_rejects_open_slab_in_the_middle(tmp_path):
    bad(tmp_path, lambda d: d.update(slabs=[{"up_to_units": None, "rate": 4}, {"up_to_units": None, "rate": 5}]), "only the final slab")


def test_rejects_empty_slabs_and_zero_limit(tmp_path):
    bad(tmp_path, lambda d: d.update(slabs=[]), "slabs")
    bad(tmp_path, lambda d: d.update(slabs=[{"up_to_units": 0, "rate": 4}, {"up_to_units": None, "rate": 5}]), "up_to_units")


@pytest.mark.parametrize(
    "field,value",
    [("fixed_charge_per_month", -1), ("fuel_adjustment_per_kwh", -0.1), ("sanctioned_load_charge_per_kw", -5), ("duty_percent", -1), ("duty_percent", 101)],
)
def test_rejects_negative_or_out_of_range_values(tmp_path, field, value):
    bad(tmp_path, lambda d: d.update({field: value}), field)


def test_rejects_negative_slab_rate_and_export_credit(tmp_path):
    bad(tmp_path, lambda d: d.update(slabs=[{"up_to_units": None, "rate": -4}]), "rate")
    bad(tmp_path, lambda d: d.update(export={"mode": "net_billing", "credit_per_kwh": -1, "bank_units": False}), "credit_per_kwh")


def test_rejects_nan_rate(tmp_path):
    path = write(tmp_path, "nan.json", '{"id":"x","version":"1","discom":"d","category":"c","effective_from":"2025-01-01",'
                 '"slabs":[{"up_to_units":null,"rate":NaN}],"source":"s"}')
    with pytest.raises(TariffLoadError):
        load_tariff_file(path)


def test_rejects_tod_gap(tmp_path):
    gap = [{"name": "a", "start": "00:00", "end": "10:00", "multiplier": 1}, {"name": "b", "start": "11:00", "end": "24:00", "multiplier": 1}]
    bad(tmp_path, lambda d: d.update(tod_bands=gap), "gap")


def test_rejects_tod_gap_at_end_of_day(tmp_path):
    short = [{"name": "a", "start": "00:00", "end": "23:00", "multiplier": 1}]
    bad(tmp_path, lambda d: d.update(tod_bands=short), "cover the full 24 hours")


def test_rejects_tod_overlap_and_duplicate_names(tmp_path):
    overlap = [{"name": "a", "start": "00:00", "end": "13:00", "multiplier": 1}, {"name": "b", "start": "12:00", "end": "24:00", "multiplier": 1}]
    bad(tmp_path, lambda d: d.update(tod_bands=overlap), "overlap")
    dup = [{"name": "a", "start": "00:00", "end": "12:00", "multiplier": 1}, {"name": "a", "start": "12:00", "end": "24:00", "multiplier": 1}]
    bad(tmp_path, lambda d: d.update(tod_bands=dup), "unique")


def test_rejects_bad_time_zero_width_band_and_negative_multiplier(tmp_path):
    bad(tmp_path, lambda d: d.update(tod_bands=[{"name": "a", "start": "25:00", "end": "24:00", "multiplier": 1}]), "HH:MM")
    bad(tmp_path, lambda d: d.update(tod_bands=[{"name": "a", "start": "06:00", "end": "06:00", "multiplier": 1}]), "same instant")
    bad(tmp_path, lambda d: d.update(tod_bands=[{"name": "a", "start": "00:00", "end": "24:00", "multiplier": -1}]), "multiplier")


def test_accepts_wrapping_tod_bands_covering_the_day():
    assert table(tod_bands=TOD).band_names == ["night", "day", "peak"]


def test_rejects_dates_out_of_order(tmp_path):
    bad(tmp_path, lambda d: d.update(effective_from="2025-06-01", effective_to="2025-05-31"), "effective_to")


def test_rejects_unknown_field_missing_source_and_bad_export(tmp_path):
    bad(tmp_path, lambda d: d.update(surprise=1), "surprise")
    bad(tmp_path, lambda d: d.pop("source"), "source")
    bad(tmp_path, lambda d: d.update(export={"mode": "none", "credit_per_kwh": 2, "bank_units": False}), "cannot carry")
    bad(tmp_path, lambda d: d.update(export={"mode": "teleport"}), "mode")


def test_rejects_broken_json_and_non_object_root(tmp_path):
    with pytest.raises(TariffLoadError, match="cannot read"):
        load_tariff_file(write(tmp_path, "broken.json", "{not json"))
    with pytest.raises(TariffLoadError, match="one JSON object"):
        load_tariff_file(write(tmp_path, "list.json", "[]"))


def test_load_error_names_the_file(tmp_path):
    with pytest.raises(TariffLoadError, match="named_file.json"):
        load_tariff_file(write(tmp_path, "named_file.json", raw_table(duty_percent=500)))


def test_directory_load_rejects_overlapping_ranges(tmp_path):
    write(tmp_path, "a.json", raw_table(version="1", effective_from="2025-01-01", effective_to="2025-12-31"))
    write(tmp_path, "b.json", raw_table(version="2", effective_from="2025-12-31"))  # starts ON a's last day
    with pytest.raises(TariffLoadError, match="overlaps"):
        TariffRegistry.from_directory(tmp_path)


def test_directory_load_rejects_open_ended_predecessor(tmp_path):
    write(tmp_path, "a.json", raw_table(version="1", effective_to=None))
    write(tmp_path, "b.json", raw_table(version="2", effective_from="2026-01-01"))
    with pytest.raises(TariffLoadError, match="overlaps"):
        TariffRegistry.from_directory(tmp_path)


def test_directory_load_rejects_duplicate_id_version(tmp_path):
    write(tmp_path, "a.json", raw_table(effective_to="2025-12-31"))
    write(tmp_path, "b.json", raw_table(effective_from="2026-01-01"))
    with pytest.raises(TariffLoadError, match="duplicate"):
        TariffRegistry.from_directory(tmp_path)


def test_overlap_is_per_discom_and_category(tmp_path):
    write(tmp_path, "a.json", raw_table(id="a", discom="One"))
    write(tmp_path, "b.json", raw_table(id="b", discom="Two"))
    write(tmp_path, "c.json", raw_table(id="c", category="commercial"))
    assert len(TariffRegistry.from_directory(tmp_path).tables) == 3


def test_empty_directory_fails_loudly(tmp_path):
    with pytest.raises(TariffLoadError, match="no tariff files"):
        TariffRegistry.from_directory(tmp_path)


# --------------------------------------------------------------------------- shipped data


def test_every_shipped_file_loads_and_is_unverified():
    files = sorted(TARIFF_DIR.glob("*.json"))
    assert len(files) >= 3
    for path in files:
        t = load_tariff_file(path)
        assert t.verified is False, path.name
        assert "illustrative" in t.source.lower() and "replace with the official tariff order" in t.source.lower()
    assert len(default_registry().tables) == len(files)


def test_shipped_bills_carry_the_unverified_flag_and_warning():
    for t in default_registry().tables:
        b = calculate_bill(usage(120, import_by_band={n: D(120) / len(t.tod_bands) for n in t.band_names} if t.tod_bands else None), t)
        facts = b.to_facts()
        assert b.tariff_verified is False and facts["tariff_verified"] is False
        assert UNVERIFIED_WARNING in b.warnings and UNVERIFIED_WARNING in facts["warnings"]


def test_verified_table_has_no_unverified_warning():
    b = calculate_bill(usage(100), table(verified=True))
    assert b.tariff_verified is True
    assert UNVERIFIED_WARNING not in b.warnings


# --------------------------------------------------------------------------- selection


def test_effective_date_selection_including_boundary_day():
    alpha = "Illustrative DISCOM Alpha"
    assert table_for(alpha, "residential", date(2025, 4, 1)).table.version == "illustrative-2025"  # first day of v1
    assert table_for(alpha, "residential", date(2026, 3, 31)).table.version == "illustrative-2025"  # LAST day of v1
    assert table_for(alpha, "residential", date(2026, 4, 1)).table.version == "illustrative-2026"  # first day of v2
    assert table_for(alpha, "residential", date(2030, 1, 1)).table.version == "illustrative-2026"  # open-ended
    assert table_for(alpha, "residential", date(2026, 3, 31)).is_fallback is False


def test_before_first_version_falls_back_and_says_so():
    sel = table_for("Illustrative DISCOM Alpha", "residential", date(2025, 3, 31))
    assert sel.is_fallback and sel.table.id == "generic-residential"
    assert "no Illustrative DISCOM Alpha / residential version in force" in sel.reason


def test_discom_matching_ignores_case_and_punctuation():
    assert table_for("  illustrative discom ALPHA ", "Residential", date(2026, 5, 1)).is_fallback is False


def test_unknown_discom_uses_profile_flat_rate():
    sel = table_for("Nowhere Power", "residential", date(2026, 1, 1), flat_rate=7.5, flat_export_credit=2)
    assert sel.is_fallback and sel.table.id == "flat-profile"
    assert sel.table.verified is False
    assert "unknown DISCOM" in sel.reason and "flat rate" in sel.reason
    b = calculate_bill(usage(100, export_kwh=10), sel.table)
    assert b.total_inr == D(730)  # 100*7.5 - 10*2, no fixed/duty/FAC
    assert UNVERIFIED_WARNING in b.warnings
    assert sel.to_facts()["tariff_is_fallback"] is True


def test_unknown_discom_without_profile_rate_uses_generic_table():
    sel = table_for(None, "residential", date(2026, 1, 1))
    assert sel.is_fallback and sel.table.id == "generic-residential"
    assert "no DISCOM given" in sel.reason


def test_no_generic_table_raises(tmp_path):
    write(tmp_path, "a.json", raw_table())
    with pytest.raises(TariffNotFound):
        table_for("zzz", "residential", date(2026, 1, 1), registry=TariffRegistry.from_directory(tmp_path))


def test_flat_table_without_export_credit_has_no_export():
    assert flat_table(6).export.mode == "none"
    assert flat_table(6, 3).export.mode == "net_billing"


# --------------------------------------------------------------------------- what-if


def test_reduce_consumption_hand_computed():
    # base 200 units: energy 1000, fixed 100, FAC 100, duty 100 = 1300. 10% off -> 180: 400+80*6 = 880;
    # FAC 90; duty 88; fixed 100 = 1158. Saving 142 / month, 1704 / year.
    res = simulate(usage(200), [ReduceConsumption(percent=10)], table()).results[0]
    assert res.baseline_total_inr == D(1300)
    assert res.scenario_total_inr == D(1158)
    assert res.delta_import_kwh == D(-20)
    assert res.saving_inr_month == D(142) and res.delta_inr_month == D(-142)
    assert res.saving_inr_year == D(1704) and res.delta_inr_year == D(-1704)
    assert res.direction == "saving"


def test_add_solar_hand_computed_with_payback():
    # 2 kWp * 100 = 200 kWh, 50% self-used = 100 -> import 100 (590). Baseline 1300 -> saving 710/month.
    sc = AddSolar(kwp=2, yield_kwh_per_kwp_month=100, self_consumption_share=0.5, capex_inr=71000)
    res = simulate(usage(200), [sc], table()).results[0]
    assert (res.delta_import_kwh, res.delta_export_kwh) == (D(-100), D(100))
    assert res.saving_inr_month == D(710)
    assert res.saving_inr_year == D(8520)
    assert res.payback_months == D("100.0")


def test_solar_self_consumption_is_capped_at_import():
    sc = AddSolar(kwp=10, yield_kwh_per_kwp_month=100, self_consumption_share=1)
    res = simulate(usage(200), [sc], table()).results[0]
    assert res.scenario_bill.import_kwh == D(0)
    assert res.scenario_bill.export_kwh == D(800)  # 1000 generated - 200 used on site
    assert res.scenario_total_inr == D(100)  # fixed charge only


def test_solar_under_net_metering_reports_banking():
    sc = AddSolar(kwp=10, yield_kwh_per_kwp_month=100, self_consumption_share=0.5)
    res = simulate(usage(200), [sc], table(export=nm(True))).results[0]
    assert res.scenario_bill.banked_units_out_kwh == D(800)
    assert any("not valued" in a for a in res.assumptions)


def test_shift_load_hand_computed():
    # Baseline 1437.50 (see ToD test). Move 50 peak -> night: night 100, peak 0.
    # slab energy 1000 avg 5; adj = 5*(100*-0.5) = -250 -> 750; FAC 100; duty 75; fixed 100 = 1025. Saving 412.50.
    base = usage(200, import_by_band={"night": 50, "day": 100, "peak": 50})
    res = simulate(base, [ShiftLoad(kwh=50, from_band="peak", to_band="night")], table(tod_bands=TOD)).results[0]
    assert res.scenario_total_inr == D(1025)
    assert res.saving_inr_month == D("412.50")
    assert res.delta_import_kwh == D(0)
    # asking for more than the band holds is capped, not invented
    big = simulate(base, [ShiftLoad(kwh=500, from_band="peak", to_band="night")], table(tod_bands=TOD)).results[0]
    assert big.scenario_total_inr == D(1025)


def test_battery_absorb_exports_hand_computed():
    # export 100 @ Rs3 credit. Baseline: 1300 - 300 = 1000.
    # Battery throughput 5*1*20 = 100, eff 0.8 -> stores 100, returns 80: import 120, export 0.
    # energy 400+20*6=520; fixed 100; FAC 60; duty 52 = 732. Saving 268.
    nb = {"mode": "net_billing", "credit_per_kwh": 3, "bank_units": False}
    sc = AddBattery(capacity_kwh=5, usable_fraction=1, round_trip_efficiency=0.8, cycles_per_month=20, strategy="absorb_exports")
    res = simulate(usage(200, export_kwh=100), [sc], table(export=nb)).results[0]
    assert res.baseline_total_inr == D(1000)
    assert res.scenario_total_inr == D(732)
    assert res.saving_inr_month == D(268)
    assert (res.delta_import_kwh, res.delta_export_kwh) == (D(-80), D(-100))


def test_battery_shift_import_hand_computed():
    # throughput 5*1*10 = 50, eff 0.5, all 50 peak kWh covered: peak 0, night 50+100=150, day 100 -> import 250.
    # slabs 100*4+150*6 = 1300, avg 5.2; adj = 5.2*(150*-0.5) = -390 -> 910; FAC 125; duty 91; fixed 100 = 1226.
    base = usage(200, import_by_band={"night": 50, "day": 100, "peak": 50})
    sc = AddBattery(capacity_kwh=5, usable_fraction=1, round_trip_efficiency=0.5, cycles_per_month=10,
                    strategy="shift_import", from_band="peak", to_band="night", shiftable_share=1)
    res = simulate(base, [sc], table(tod_bands=TOD)).results[0]
    assert res.scenario_total_inr == D(1226)
    assert res.saving_inr_month == D("211.50")
    assert res.delta_import_kwh == D(50)  # losses mean MORE grid energy, but cheaper hours


def test_battery_absorb_loses_against_net_metering_banking():
    sc = AddBattery(capacity_kwh=5, usable_fraction=1, round_trip_efficiency=0.8, cycles_per_month=20, strategy="absorb_exports")
    res = simulate(usage(200, export_kwh=100), [sc], table(export=nm(True))).results[0]
    assert res.saving_inr_month <= 0
    assert res.direction in {"extra_cost", "no_change"}
    assert any("only lose value" in a for a in res.assumptions)


def test_change_tariff_compares_two_tables():
    other = table(id="other", slabs=[{"up_to_units": None, "rate": 5}], fixed_charge_per_month=0, fuel_adjustment_per_kwh=0, duty_percent=0)
    res = simulate(usage(200), [ChangeTariff(other_table=other)], table()).results[0]
    assert res.scenario_total_inr == D(1000)  # 200 * 5
    assert res.saving_inr_month == D(300)
    assert res.delta_import_kwh == D(0)
    assert res.scenario_bill.tariff_id == "other"


def test_extra_cost_direction_and_no_payback_without_saving():
    pricier = table(id="pricey", slabs=[{"up_to_units": None, "rate": 20}])
    res = simulate(usage(200), [ChangeTariff(other_table=pricier, capex_inr=1000)], table()).results[0]
    assert res.direction == "extra_cost" and res.saving_inr_month < 0
    assert res.payback_months is None


def test_no_payback_without_capex():
    assert simulate(usage(200), [ReduceConsumption(percent=10)], table()).results[0].payback_months is None


def test_inapplicable_scenarios_raise():
    with pytest.raises(ScenarioError, match="ToD"):
        simulate(usage(200), [ShiftLoad(kwh=5, from_band="peak", to_band="night")], table())
    with pytest.raises(ScenarioError, match="unknown ToD band"):
        simulate(usage(200, import_by_band={"night": 200}), [ShiftLoad(kwh=5, from_band="peak", to_band="nope")], table(tod_bands=TOD))
    other_tod = table(id="o", tod_bands=[{"name": "all", "start": "00:00", "end": "24:00", "multiplier": 1}])
    with pytest.raises(ScenarioError, match="does not define"):
        simulate(usage(200, import_by_band={"night": 200}), [ChangeTariff(other_table=other_tod)], table(tod_bands=TOD))


def test_scenario_models_validate_input():
    with pytest.raises(ValidationError):
        AddSolar(kwp=2, yield_kwh_per_kwp_month=100, self_consumption_share=1.5)
    with pytest.raises(ValidationError):  # yield must be stated, never defaulted
        AddSolar(kwp=2, self_consumption_share=0.5)
    with pytest.raises(ValidationError, match="shift_import needs"):
        AddBattery(capacity_kwh=5, usable_fraction=1, round_trip_efficiency=0.9, cycles_per_month=30, strategy="shift_import")
    with pytest.raises(ValidationError):
        ReduceConsumption(percent=0)
    with pytest.raises(ValidationError):
        ReduceConsumption(percent=10, surprise=True)


def all_scenarios():
    return [
        AddSolar(kwp=2, yield_kwh_per_kwp_month=100, self_consumption_share=0.6, capex_inr=100000),
        AddBattery(capacity_kwh=5, usable_fraction=0.9, round_trip_efficiency=0.9, cycles_per_month=30,
                   strategy="shift_import", from_band="peak", to_band="night", shiftable_share=0.8),
        ShiftLoad(kwh=20, from_band="peak", to_band="night"),
        ChangeTariff(other_table=table(id="alt", fixed_charge_per_month=0)),
        ReduceConsumption(percent=5),
    ]


def sim_inputs():
    return usage(200, import_by_band={"night": 50, "day": 100, "peak": 50}), table(tod_bands=TOD)


def test_assumptions_always_listed():
    base, tbl = sim_inputs()
    for r in simulate(base, all_scenarios(), tbl).results:
        assert len(r.assumptions) >= 2
        assert any("12 times" in a for a in r.assumptions)
        assert r.to_facts()["assumptions"] == r.assumptions


def test_simulation_is_deterministic():
    base, tbl = sim_inputs()
    a = simulate(base, all_scenarios(), tbl).model_dump(mode="json")
    b = simulate(base, all_scenarios(), tbl).model_dump(mode="json")
    assert a == b


def test_duplicate_scenario_names_are_made_unique():
    res = simulate(usage(200), [ReduceConsumption(percent=5), ReduceConsumption(percent=10), ReduceConsumption(percent=15, label="x")], table())
    assert [r.name for r in res.results] == ["reduce_consumption_percent", "reduce_consumption_percent_2", "x"]


def test_to_facts_round_trips_through_json_and_is_rounded():
    base, tbl = sim_inputs()
    facts = simulate(base, all_scenarios(), tbl).to_facts()
    assert json.loads(json.dumps(facts)) == facts
    for sc in facts["scenarios"]:
        for key in ("baseline_bill_inr", "new_bill_inr", "saving_inr_month", "saving_inr_year", "delta_import_kwh"):
            assert isinstance(sc[key], float)
            assert round(sc[key], 2) == sc[key]
    first = facts["scenarios"][0]
    assert first["payback_months"] == round(first["payback_months"], 1)
    assert facts["scenarios"][0]["saving_inr_year"] == pytest.approx(12 * facts["scenarios"][0]["saving_inr_month"], abs=0.02)


def test_full_result_is_json_dumpable_with_numeric_money():
    base, tbl = sim_inputs()
    dumped = json.loads(json.dumps(simulate(base, all_scenarios(), tbl).model_dump(mode="json")))
    assert isinstance(dumped["baseline_bill"]["total_inr"], (int, float))
    assert isinstance(dumped["results"][0]["saving_inr_month"], (int, float))


def test_facts_carry_unverified_flag_and_warning_for_shipped_tables():
    sel = table_for("Illustrative DISCOM Beta", "residential", date(2026, 6, 1))
    facts = simulate(usage(250), [ReduceConsumption(percent=10)], sel.table).to_facts()
    assert facts["tariff_verified"] is False
    assert facts["baseline"]["tariff_verified"] is False
    assert any("UNVERIFIED" in w for w in facts["warnings"])
    assert facts["scenarios"][0]["new_tariff_verified"] is False


def test_facts_cover_every_quotable_number():
    facts = simulate(usage(200), [ReduceConsumption(percent=10, capex_inr=5000)], table()).to_facts()["scenarios"][0]
    for key in ("baseline_bill_inr", "new_bill_inr", "saving_inr_month", "saving_inr_year", "payback_months",
                "capex_inr", "delta_import_kwh", "new_bill_effective_rate_inr_per_kwh"):
        assert facts[key] is not None, key
    assert facts["payback_months"] == 35.2  # 5000 / 142 = 35.21
