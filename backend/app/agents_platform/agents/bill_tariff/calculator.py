"""
Monthly bill calculator: usage + tariff table -> an itemised `BillBreakdown`.

POLICIES (each one is pinned by a test; change them deliberately):

Rounding   Arithmetic is exact `Decimal`. Each emitted line is rounded ONCE, half-up to the paisa, and
           every total is the sum of the ROUNDED lines, so the lines on screen always add up to the total
           exactly. Duty is computed on the rounded energy-charge line (what a reader can verify by hand).
Proration  Tables are quoted per 30-day month. With `days` != 30 both the fixed/sanctioned-load charges
           and the slab limits are scaled by days/30 (a 15-day bill gets half the monthly slab widths and
           half the fixed charge). At days == 30 the factor is exactly 1.
Slabs      Marginal ("telescopic"): units up to a limit are priced at that slab's rate, units above it at
           the next. Usage exactly AT a limit is entirely in the lower slab.
ToD        If the table has bands and usage is split by band: the slab energy charge is computed on total
           billed units, then each band's units are re-priced by (multiplier - 1) at the bill's average
           slab rate (the `tod_adjustment` line). Equivalent to applying the multiplier to each band's
           pro-rata share of every slab. No split given -> multipliers are NOT applied (warned).
Fuel adj.  `fuel_adjustment_per_kwh` x billed (post-netting) units.
Duty       `duty_percent` of the energy charge only (slabs + ToD adjustment); NOT of fixed charge or FAC.
Export     See `ExportRules`. Under net metering the offset is spread over ToD bands pro rata.
Total      Never below zero; unused export credit is carried (net billing + bank_units) or lapses.
Eff. rate  total / GROSS import kWh (None when import is zero). Net-metered homes can therefore see a
           very low rate: it is cost per kWh imported, not per kWh consumed.
"""
from __future__ import annotations

from decimal import Decimal

from app.agents_platform.agents.bill_tariff.models import (
    BillBreakdown,
    BillLine,
    MonthlyUsage,
    TariffTable,
    round_money,
)

PRORATION_BASIS_DAYS = 30
ZERO = Decimal("0")
UNVERIFIED_WARNING = (
    "UNVERIFIED TARIFF: the rates in this bill come from an illustrative or profile-derived table, "
    "not an official tariff order. Present amounts as estimates only, never as official rates."
)


def calculate_bill(usage: MonthlyUsage, table: TariffTable) -> BillBreakdown:
    """Price one billing period. Raises ValueError if `usage` names ToD bands the table does not have."""
    warnings: list[str] = []
    if not table.verified:
        warnings.append(UNVERIFIED_WARNING)

    factor = Decimal(usage.days) / Decimal(PRORATION_BASIS_DAYS)
    imp, exp = usage.import_kwh, usage.export_kwh

    billed, banked_out, paid_credit, carry_in_inr = _net_units(usage, table, warnings)

    lines: list[BillLine] = []

    # --- slab energy charge
    slab_total_exact = ZERO
    previous_limit = ZERO
    for index, slab in enumerate(table.slabs, start=1):
        limit = None if slab.up_to_units is None else slab.up_to_units * factor
        upper = billed if limit is None else min(billed, limit)
        units = upper - previous_limit
        if units > 0:
            exact = units * slab.rate
            slab_total_exact += exact
            lines.append(BillLine(code=f"energy_slab_{index}", label=f"Energy, slab {index}",
                                  units_kwh=units, rate=slab.rate, amount_inr=round_money(exact)))
        if limit is not None:
            previous_limit = limit
        if limit is None or billed <= limit:
            break

    # --- time-of-day adjustment
    tod_exact = _tod_adjustment(usage, table, billed, imp, slab_total_exact, warnings)
    if tod_exact != 0:
        lines.append(BillLine(code="tod_adjustment", label="Time-of-day adjustment", amount_inr=round_money(tod_exact)))
    energy = sum((ln.amount_inr for ln in lines), ZERO)

    # --- fixed / demand charges (prorated)
    fixed = round_money(table.fixed_charge_per_month * factor)
    if fixed != 0:
        lines.append(BillLine(code="fixed_charge", label="Fixed charge (prorated)" if usage.days != PRORATION_BASIS_DAYS else "Fixed charge",
                              rate=table.fixed_charge_per_month, amount_inr=fixed))
    load_charge = ZERO
    if table.sanctioned_load_charge_per_kw is not None:
        if usage.sanctioned_load_kw is None:
            warnings.append("Sanctioned-load charge not applied: sanctioned load was not provided.")
        else:
            load_charge = round_money(table.sanctioned_load_charge_per_kw * usage.sanctioned_load_kw * factor)
            if load_charge != 0:
                lines.append(BillLine(code="sanctioned_load_charge", label="Sanctioned-load charge",
                                      units_kwh=usage.sanctioned_load_kw, rate=table.sanctioned_load_charge_per_kw,
                                      amount_inr=load_charge))

    # --- fuel adjustment, duty
    fac = round_money(table.fuel_adjustment_per_kwh * billed)
    if fac != 0:
        lines.append(BillLine(code="fuel_adjustment", label="Fuel adjustment charge", units_kwh=billed,
                              rate=table.fuel_adjustment_per_kwh, amount_inr=fac))
    duty = round_money(energy * table.duty_percent / Decimal(100))
    if duty != 0:
        lines.append(BillLine(code="electricity_duty", label=f"Electricity duty ({table.duty_percent.normalize():f}% of energy charge)",
                              rate=table.duty_percent, amount_inr=duty))

    # --- export credit (never drives the total below zero)
    subtotal = energy + fixed + load_charge + fac + duty
    credit_available = round_money(paid_credit) + carry_in_inr
    credit_applied = min(credit_available, subtotal)
    leftover = credit_available - credit_applied
    carried_out = leftover if (table.export.mode == "net_billing" and table.export.bank_units) else ZERO
    lapsed = leftover - carried_out
    if credit_applied != 0:
        lines.append(BillLine(code="export_credit", label="Export credit / net-metering offset", amount_inr=-credit_applied))
    if lapsed != 0:
        warnings.append("Part of the export credit exceeded the bill and is not carried forward by this tariff.")

    total = sum((ln.amount_inr for ln in lines), ZERO)
    return BillBreakdown(
        tariff_id=table.id, tariff_version=table.version, tariff_discom=table.discom,
        tariff_category=table.category, tariff_verified=table.verified, tariff_source=table.source,
        currency=table.currency, import_kwh=imp, export_kwh=exp, billed_units_kwh=billed,
        days=usage.days, proration_factor=factor, lines=lines,
        energy_charge_inr=energy, fixed_charge_inr=fixed, sanctioned_load_charge_inr=load_charge,
        fuel_adjustment_inr=fac, duty_inr=duty, export_credit_inr=credit_applied, total_inr=total,
        effective_rate_inr_per_kwh=None if imp == 0 else round_money(total / imp),
        banked_units_out_kwh=banked_out, carried_credit_out_inr=carried_out, credit_lapsed_inr=lapsed,
        warnings=warnings,
    )


def _net_units(usage: MonthlyUsage, table: TariffTable, warnings: list[str]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Apply the export rules. Returns (billed_units, banked_units_out, INR credit earned, INR credit brought in)."""
    imp, exp, rules = usage.import_kwh, usage.export_kwh, table.export
    if rules.mode != "net_metering" and usage.banked_units_in != 0:
        warnings.append("Opening banked units ignored: this tariff does not bank units.")
    if rules.mode == "net_metering":
        if not rules.bank_units and usage.banked_units_in != 0:
            warnings.append("Opening banked units ignored: this tariff does not bank units.")
        opening = usage.banked_units_in if rules.bank_units else ZERO
        available = exp + opening
        offset = min(imp, available)
        surplus = available - offset
        if rules.bank_units:
            return imp - offset, surplus, ZERO, ZERO
        return imp - offset, ZERO, surplus * rules.credit_per_kwh, ZERO
    if rules.mode == "net_billing":
        carry = usage.carried_credit_in_inr if rules.bank_units else ZERO
        if not rules.bank_units and usage.carried_credit_in_inr != 0:
            warnings.append("Opening export credit ignored: this tariff does not carry credit forward.")
        return imp, ZERO, exp * rules.credit_per_kwh, carry
    if usage.carried_credit_in_inr != 0:
        warnings.append("Opening export credit ignored: this tariff has no export credit.")
    return imp, ZERO, ZERO, ZERO


def _tod_adjustment(usage: MonthlyUsage, table: TariffTable, billed: Decimal, imp: Decimal,
                    slab_total: Decimal, warnings: list[str]) -> Decimal:
    if not table.tod_bands:
        if usage.import_by_band:
            warnings.append("Per-band usage ignored: this tariff has no time-of-day bands.")
        return ZERO
    if usage.import_by_band is None:
        warnings.append("Time-of-day multipliers not applied: usage was not split by band.")
        return ZERO
    unknown = set(usage.import_by_band) - set(table.band_names)
    if unknown:
        raise ValueError(f"usage names ToD bands {sorted(unknown)} that tariff {table.id} does not define ({table.band_names})")
    if billed == 0 or imp == 0:
        return ZERO
    average_rate = slab_total / billed
    share = billed / imp  # net-metering offset is spread over bands pro rata
    return sum(
        (usage.import_by_band.get(b.name, ZERO) * share * average_rate * (b.multiplier - 1) for b in table.tod_bands),
        ZERO,
    )
