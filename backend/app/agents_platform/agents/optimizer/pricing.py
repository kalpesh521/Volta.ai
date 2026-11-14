"""
Marginal electricity prices per hour, taken from the tariff engine rather than re-derived.

The optimiser needs "what does one more kWh cost me in this hour". On a slab tariff with time-of-day bands, duty and a
fuel adjustment that is not any single number in the table, so it is measured: price the home's typical month, price
it again with one extra kWh in the band that hour falls in, and take the difference. The calculator stays the one place
that knows how a bill is made, and the optimiser can never disagree with it.

The month used is an estimate (`bill_tariff.usage.usage_from_bill`), spread over the bands by how many hours each
covers, so the marginal slab is the one the user's real usage sits in.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.agents_platform.agents.bill_tariff.calculator import calculate_bill
from app.agents_platform.agents.bill_tariff.models import MonthlyUsage, TariffTable, TodBand

_ONE_KWH = Decimal("1")
_MINUTES_PER_DAY = 24 * 60


def band_at(table: TariffTable, moment: datetime) -> TodBand | None:
    minute = moment.hour * 60 + moment.minute
    for band in table.tod_bands:
        if any(start <= minute < end for start, end in band.intervals()):
            return band
    return None


def _hours_in(band: TodBand) -> float:
    return sum(end - start for start, end in band.intervals()) / 60


def _with_bands(usage: MonthlyUsage, table: TariffTable) -> MonthlyUsage:
    """The same month with its import spread over the bands in proportion to the hours each covers."""
    if not table.tod_bands:
        return usage
    spread = {b.name: (usage.import_kwh * Decimal(str(_hours_in(b) / 24))) for b in table.tod_bands}
    drift = usage.import_kwh - sum(spread.values(), Decimal("0"))
    first = table.tod_bands[0].name
    spread[first] += drift  # rounding drift goes into one band so the parts add up exactly
    return usage.model_copy(update={"import_by_band": spread})


def marginal_prices(table: TariffTable, usage: MonthlyUsage) -> dict[str | None, float]:
    """INR per extra kWh for each band name (or `None` for a tariff without bands)."""
    base_usage = _with_bands(usage, table)
    base = calculate_bill(base_usage, table).total_inr
    names: list[str | None] = [b.name for b in table.tod_bands] or [None]
    prices: dict[str | None, float] = {}
    for name in names:
        bands = dict(base_usage.import_by_band) if base_usage.import_by_band else None
        if bands is not None and name is not None:
            bands[name] = bands[name] + _ONE_KWH
        bumped = base_usage.model_copy(update={"import_kwh": base_usage.import_kwh + _ONE_KWH, "import_by_band": bands})
        prices[name] = float(calculate_bill(bumped, table).total_inr - base)
    return prices


def export_credit(table: TariffTable, import_price: float) -> float:
    """What a kWh sent to the grid earns. Net metering offsets an imported unit, so it is worth that band's price."""
    rules = table.export
    if rules.mode == "none":
        return 0.0
    if rules.mode == "net_metering":
        return import_price  # one-for-one offset (banking and month-end lapse are not modelled: a stated limit)
    return float(rules.credit_per_kwh)
