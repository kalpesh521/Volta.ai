"""
Pull structured fields out of an electricity bill.

Missing fields stay null. The assistant quotes these database values and uses
retrieval only as supporting text, so a regex miss is better than a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS = (
    re.compile(r"(?:units?\s*(?:consumed|billed)?|consumption)\s*[:\-]?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*kwh\b", re.I),
)
_TARIFF = re.compile(
    r"tariff\s*(?:category|type|code)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 \-/]{0,40})",
    re.I,
)
_LOAD = re.compile(r"sanctioned\s+load\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*k(?:w|va)\b", re.I)
_AMOUNT = re.compile(
    r"(?:amount\s*(?:payable|due)?|total\s*(?:amount|bill)|bill\s*amount)\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)",
    re.I,
)


@dataclass(frozen=True)
class BillFields:
    units_kwh: float | None
    tariff_category: str | None
    sanctioned_load_kw: float | None
    amount_inr: float | None

    @property
    def any_found(self) -> bool:
        return any(
            value is not None
            for value in (self.units_kwh, self.tariff_category, self.sanctioned_load_kw, self.amount_inr)
        )


def extract_bill_fields(text: str) -> BillFields:
    units = _first_float(_UNITS, text)
    load = _first_float((_LOAD,), text)
    amount = _first_float((_AMOUNT,), text)
    tariff_match = _TARIFF.search(text)
    tariff = tariff_match.group(1).strip(" .:-") if tariff_match else None
    if tariff and len(tariff) < 2:
        tariff = None
    return BillFields(
        units_kwh=units,
        tariff_category=tariff,
        sanctioned_load_kw=load,
        amount_inr=amount,
    )


def _first_float(patterns: tuple[re.Pattern[str], ...], text: str) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    return None
