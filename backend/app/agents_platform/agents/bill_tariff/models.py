"""
Typed schemas for the bill & tariff engine: the tariff table, the monthly usage input and the bill output.

WHY strict: a tariff file is data somebody typed by hand. A slab that overlaps its neighbour, a ToD
day with a hole in it, or a typo'd key would silently produce a wrong bill that the assistant then
states with confidence. Every model forbids unknown fields and every rule that can be checked at load
time IS checked at load time (see `TariffTable` validators), so a bad file fails loudly at startup
instead of producing a plausible-looking wrong number in a chat.

Money is `Decimal` everywhere. In JSON mode it is emitted as a plain number (float) so results are
`json.dumps`-able and a grounding guard can match them against the answer text.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

PAISE = Decimal("0.01")
MINUTES_PER_DAY = 24 * 60

# Decimal in Python, a plain JSON number in JSON mode. Never a string: a string would not be a
# "number" to the grounding guard and would force the model to reformat it.
Number = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
_ID = r"^[a-z0-9][a-z0-9_-]*$"
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")


def D(value: object) -> Decimal:
    """Decimal from any number via `str`, so 0.1 stays 0.1 (never the binary-float expansion)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_money(value: Decimal) -> Decimal:
    """The one rounding rule for money: half-up to the paisa (what a printed bill does)."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def normalise_key(text: str) -> str:
    """Case/space/punctuation-insensitive key so 'MSEDCL', 'msedcl ' and 'M.S.E.D.C.L' compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- tariff table


class Slab(_Strict):
    """One consumption tier. `up_to_units` is the cumulative upper bound; None means open-ended."""

    up_to_units: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    rate: NonNegative


class TodBand(_Strict):
    """A time-of-day band. `end` may be earlier than `start` (wraps midnight); '24:00' is allowed as an end."""

    name: Annotated[str, Field(min_length=1, max_length=32, pattern=_ID)]
    start: str
    end: str
    multiplier: NonNegative

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"time must be HH:MM (00:00-24:00), got {v!r}")
        return v

    @model_validator(mode="after")
    def _non_empty(self) -> "TodBand":
        if _minutes(self.start) == _minutes(self.end):
            raise ValueError(f"band {self.name!r}: start and end are the same instant")
        return self

    def intervals(self) -> list[tuple[int, int]]:
        """Half-open [start, end) minute intervals within one day (a wrapping band yields two)."""
        s, e = _minutes(self.start), _minutes(self.end)
        if s < e:
            return [(s, e)]
        return [(s, MINUTES_PER_DAY)] + ([(0, e)] if e > 0 else [])


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class ExportRules(_Strict):
    """
    How exported (solar) units are treated.

    net_metering: exports offset imports unit-for-unit. Surplus is banked to next month if `bank_units`,
                  otherwise paid at `credit_per_kwh` (0 means it simply lapses).
    net_billing:  exports are valued at `credit_per_kwh` and credited against the bill; imports are billed
                  in full. If the credit exceeds the bill the rest is carried forward when `bank_units`
                  (as INR) and lapses otherwise.
    none:         exports earn nothing.
    """

    mode: Literal["net_metering", "net_billing", "none"]
    credit_per_kwh: NonNegative = Decimal("0")
    bank_units: bool = False

    @model_validator(mode="after")
    def _none_means_none(self) -> "ExportRules":
        if self.mode == "none" and (self.credit_per_kwh != 0 or self.bank_units):
            raise ValueError("export mode 'none' cannot carry a credit or banking")
        return self


class TariffTable(_Strict):
    """
    One version of one tariff (DISCOM + category). Effective range is inclusive on both ends:
    `effective_from` is the first day in force, `effective_to` the last (None = still in force).
    """

    id: Annotated[str, Field(pattern=_ID, max_length=64)]
    version: Annotated[str, Field(min_length=1, max_length=32)]
    discom: Annotated[str, Field(min_length=1, max_length=80)]
    category: Annotated[str, Field(min_length=1, max_length=40)]
    effective_from: date
    effective_to: date | None = None
    currency: Literal["INR"] = "INR"
    slabs: Annotated[list[Slab], Field(min_length=1)]
    fixed_charge_per_month: NonNegative = Decimal("0")
    sanctioned_load_charge_per_kw: NonNegative | None = None
    tod_bands: list[TodBand] = Field(default_factory=list)
    fuel_adjustment_per_kwh: NonNegative = Decimal("0")
    duty_percent: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)] = Decimal("0")
    export: ExportRules = Field(default_factory=lambda: ExportRules(mode="none"))
    source: Annotated[str, Field(min_length=1)]
    verified: bool = False

    @field_validator("slabs")
    @classmethod
    def _slabs_ordered_and_continuous(cls, slabs: list[Slab]) -> list[Slab]:
        # Slabs are expressed as cumulative upper bounds, so "continuity" means: strictly increasing,
        # no open-ended slab except the last, and the last MUST be open-ended so every usage is priced.
        for i, slab in enumerate(slabs):
            last = i == len(slabs) - 1
            if slab.up_to_units is None and not last:
                raise ValueError(f"slab {i + 1}: only the final slab may be open-ended")
            if last and slab.up_to_units is not None:
                raise ValueError("final slab must be open-ended (up_to_units null) so all usage is priced")
            if i > 0 and not last and slab.up_to_units <= slabs[i - 1].up_to_units:  # type: ignore[operator]
                raise ValueError(f"slab {i + 1}: up_to_units must be strictly greater than the previous slab")
        return slabs

    @field_validator("tod_bands")
    @classmethod
    def _tod_covers_24h(cls, bands: list[TodBand]) -> list[TodBand]:
        if not bands:
            return bands
        names = [b.name for b in bands]
        if len(set(names)) != len(names):
            raise ValueError("ToD band names must be unique")
        pieces = sorted(iv for b in bands for iv in b.intervals())
        cursor = 0
        for start, end in pieces:
            if start < cursor:
                raise ValueError("ToD bands overlap")
            if start > cursor:
                raise ValueError(f"ToD bands leave a gap from minute {cursor} to {start}")
            cursor = end
        if cursor != MINUTES_PER_DAY:
            raise ValueError("ToD bands must cover the full 24 hours (gap at the end of the day)")
        return bands

    @model_validator(mode="after")
    def _dates_and_export_ordering(self) -> "TariffTable":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to is before effective_from")
        return self

    def in_force_on(self, on_date: date) -> bool:
        return self.effective_from <= on_date and (self.effective_to is None or on_date <= self.effective_to)

    @property
    def band_names(self) -> list[str]:
        return [b.name for b in self.tod_bands]


# --------------------------------------------------------------------------- usage input


class MonthlyUsage(_Strict):
    """
    One billing period of metered usage.

    `import_by_band` (optional) splits `import_kwh` across the table's ToD band names; it must add up to
    `import_kwh` (within 0.01 kWh) so the two views can never disagree. `banked_units_in` /
    `carried_credit_in_inr` are the opening balances brought forward from earlier months.
    """

    import_kwh: NonNegative
    export_kwh: NonNegative = Decimal("0")
    import_by_band: dict[str, NonNegative] | None = None
    days: Annotated[int, Field(ge=1, le=62)] = 30
    sanctioned_load_kw: NonNegative | None = None
    banked_units_in: NonNegative = Decimal("0")
    carried_credit_in_inr: NonNegative = Decimal("0")

    @model_validator(mode="after")
    def _bands_add_up(self) -> "MonthlyUsage":
        if self.import_by_band is not None:
            total = sum(self.import_by_band.values(), Decimal("0"))
            if abs(total - self.import_kwh) > Decimal("0.01"):
                raise ValueError(f"import_by_band sums to {total}, but import_kwh is {self.import_kwh}")
        return self


# --------------------------------------------------------------------------- bill output


class BillLine(_Strict):
    """One explainable line. `amount_inr` is already rounded to the paisa; credits are negative."""

    code: str
    label: str
    units_kwh: Number | None = None
    rate: Number | None = None
    amount_inr: Number


class BillBreakdown(_Strict):
    """
    A computed bill. Invariant: `total_inr == sum(line.amount_inr for line in lines)` exactly, and
    every figure a good answer might quote is a field (the model never has to add or multiply).
    """

    tariff_id: str
    tariff_version: str
    tariff_discom: str
    tariff_category: str
    tariff_verified: bool
    tariff_source: str
    currency: str
    import_kwh: Number
    export_kwh: Number
    billed_units_kwh: Number
    days: int
    proration_factor: Number
    lines: list[BillLine]
    energy_charge_inr: Number
    fixed_charge_inr: Number
    sanctioned_load_charge_inr: Number
    fuel_adjustment_inr: Number
    duty_inr: Number
    export_credit_inr: Number
    total_inr: Number
    effective_rate_inr_per_kwh: Number | None
    banked_units_out_kwh: Number
    carried_credit_out_inr: Number
    credit_lapsed_inr: Number
    warnings: list[str]

    def to_facts(self) -> dict[str, object]:
        """Compact, rounded dict of every number a good answer might quote."""
        f = to_float
        return {
            "tariff_id": self.tariff_id,
            "tariff_version": self.tariff_version,
            "tariff_discom": self.tariff_discom,
            "tariff_verified": self.tariff_verified,
            "import_kwh": f(self.import_kwh),
            "export_kwh": f(self.export_kwh),
            "billed_units_kwh": f(self.billed_units_kwh),
            "days": self.days,
            "energy_charge_inr": f(self.energy_charge_inr),
            "fixed_charge_inr": f(self.fixed_charge_inr),
            "sanctioned_load_charge_inr": f(self.sanctioned_load_charge_inr),
            "fuel_adjustment_inr": f(self.fuel_adjustment_inr),
            "duty_inr": f(self.duty_inr),
            "export_credit_inr": f(self.export_credit_inr),
            "total_inr": f(self.total_inr),
            "effective_rate_inr_per_kwh": None
            if self.effective_rate_inr_per_kwh is None
            else f(self.effective_rate_inr_per_kwh),
            "banked_units_out_kwh": f(self.banked_units_out_kwh),
            "carried_credit_out_inr": f(self.carried_credit_out_inr),
            "credit_lapsed_inr": f(self.credit_lapsed_inr),
            "lines": [
                {"code": ln.code, "units_kwh": None if ln.units_kwh is None else f(ln.units_kwh),
                 "rate": None if ln.rate is None else f(ln.rate), "amount_inr": f(ln.amount_inr)}
                for ln in self.lines
            ],
            "warnings": list(self.warnings),
        }


def to_float(value: Decimal, places: int = 2) -> float:
    """Half-up round a Decimal to `places` and hand it over as a JSON float."""
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
