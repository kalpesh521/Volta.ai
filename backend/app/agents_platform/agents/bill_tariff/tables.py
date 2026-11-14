"""
Versioned tariff registry: load + validate JSON files, and pick the version in force on a date.

WHY a registry that validates the WHOLE set: per-file schema checks cannot see that two files for the
same DISCOM and category claim the same day. That is exactly the mistake made when a new tariff year is
added by copy-paste, so it is rejected at load time with both file names in the message.

Loading is explicit and loud. `default_registry()` loads the shipped `tariffs/*.json` once and caches
it; the platform should call it at startup so a bad file stops the boot rather than a chat turn.

HONESTY: the shipped tables are illustrative (`verified: false`). Selection never upgrades that.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.agents_platform.agents.bill_tariff.models import (
    D,
    ExportRules,
    Slab,
    TariffTable,
    normalise_key,
)

TARIFF_DIR = Path(__file__).parent / "tariffs"
GENERIC_DISCOM = "generic"
GENERIC_CATEGORY = "residential"
FLAT_TABLE_ID = "flat-profile"
_FLAT_SOURCE = (
    "Derived from the home profile's flat tariff_rate and export credit. "
    "Not an official DISCOM tariff: no slabs, fixed charge, duty or ToD."
)


class TariffLoadError(ValueError):
    """A tariff file or the set of files is invalid. Carries the offending path in the message."""


class TariffNotFound(LookupError):
    """No table could be selected and no fallback was possible."""


def load_tariff_file(path: Path) -> TariffTable:
    """Parse and validate one file. Floats are read as Decimal so 4.35 is exactly 4.35."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise TariffLoadError(f"{path.name}: cannot read tariff file: {exc}") from exc
    if not isinstance(raw, dict):
        raise TariffLoadError(f"{path.name}: a tariff file must hold one JSON object (one version per file)")
    try:
        return TariffTable.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
        )
        raise TariffLoadError(f"{path.name}: invalid tariff table: {problems}") from exc


class TariffRegistry:
    """An immutable, cross-validated set of tariff versions."""

    def __init__(self, tables: list[tuple[str, TariffTable]]) -> None:
        """`tables` is [(source_label, table)]; the label is only used for error messages."""
        seen_ids: dict[tuple[str, str], str] = {}
        by_family: dict[tuple[str, str], list[tuple[str, TariffTable]]] = {}
        for label, table in tables:
            key = (table.id, table.version)
            if key in seen_ids:
                raise TariffLoadError(f"{label}: duplicate id+version {key} (also in {seen_ids[key]})")
            seen_ids[key] = label
            family = (normalise_key(table.discom), normalise_key(table.category))
            by_family.setdefault(family, []).append((label, table))
        for family, members in by_family.items():
            _reject_overlaps(family, members)
        self._tables = [t for _, t in tables]
        self._by_family = {
            fam: sorted((t for _, t in members), key=lambda t: t.effective_from)
            for fam, members in by_family.items()
        }

    @classmethod
    def from_directory(cls, directory: Path = TARIFF_DIR) -> "TariffRegistry":
        files = sorted(directory.glob("*.json"))
        if not files:
            raise TariffLoadError(f"no tariff files found in {directory}")
        return cls([(p.name, load_tariff_file(p)) for p in files])

    @property
    def tables(self) -> list[TariffTable]:
        return list(self._tables)

    def versions(self, discom: str, category: str) -> list[TariffTable]:
        return list(self._by_family.get((normalise_key(discom), normalise_key(category)), []))

    def find(self, discom: str, category: str, on_date: date) -> TariffTable | None:
        """The version in force on `on_date` (both ends of its range inclusive), else None."""
        for table in self.versions(discom, category):
            if table.in_force_on(on_date):
                return table
        return None


def _reject_overlaps(family: tuple[str, str], members: list[tuple[str, TariffTable]]) -> None:
    ordered = sorted(members, key=lambda m: m[1].effective_from)
    for (la, a), (lb, b) in zip(ordered, ordered[1:]):
        # Ranges are inclusive, so b starting ON a's last day is an overlap.
        if a.effective_to is None or b.effective_from <= a.effective_to:
            raise TariffLoadError(
                f"{lb}: effective range overlaps {la} for discom/category {family[0]}/{family[1]}"
            )


@lru_cache(maxsize=1)
def default_registry() -> TariffRegistry:
    """The shipped tariffs, validated once. Call at startup so a bad file fails the boot."""
    return TariffRegistry.from_directory(TARIFF_DIR)


def flat_table(rate: Decimal | float | str, export_credit: Decimal | float | str = 0, *, on_date: date | None = None) -> TariffTable:
    """
    A single-slab table from the profile's flat INR/kWh. Used when no real table applies. Export credit,
    if any, is modelled as net billing (a per-kWh credit), the closest match to a flat profile value.
    Always `verified=False`: the number is a user/profile input, not an official tariff.
    """
    credit = D(export_credit)
    return TariffTable(
        id=FLAT_TABLE_ID,
        version="profile",
        discom="profile",
        category=GENERIC_CATEGORY,
        effective_from=on_date or date(2000, 1, 1),
        slabs=[Slab(up_to_units=None, rate=D(rate))],
        export=ExportRules(mode="net_billing" if credit > 0 else "none", credit_per_kwh=credit),
        source=_FLAT_SOURCE,
        verified=False,
    )


@dataclass(frozen=True)
class TariffSelection:
    """Which table was used and why, so the answer can say it (never silently substituted)."""

    table: TariffTable
    is_fallback: bool
    reason: str

    def to_facts(self) -> dict[str, object]:
        return {
            "tariff_id": self.table.id,
            "tariff_version": self.table.version,
            "tariff_discom": self.table.discom,
            "tariff_verified": self.table.verified,
            "tariff_is_fallback": self.is_fallback,
            "tariff_selection_reason": self.reason,
        }


def table_for(
    discom: str | None,
    category: str,
    on_date: date,
    *,
    registry: TariffRegistry | None = None,
    flat_rate: Decimal | float | str | None = None,
    flat_export_credit: Decimal | float | str = 0,
) -> TariffSelection:
    """
    Pick the table for a DISCOM and category on a date.

    Order: (1) exact DISCOM+category version in force on `on_date`; (2) otherwise a flat table from the
    profile (`flat_rate`), if given; (3) otherwise the shipped generic table. Cases 2 and 3 are flagged
    `is_fallback=True` with a reason. One bill uses one table: a bill period that straddles a tariff change is
    priced entirely on the table in force on the date passed (pass the period start or end, be consistent).
    """
    reg = registry or default_registry()
    if discom and discom.strip():
        hit = reg.find(discom, category, on_date)
        if hit is not None:
            return TariffSelection(hit, False, f"matched {hit.id} v{hit.version} for {discom} / {category} on {on_date}")
        known = reg.versions(discom, category)
        why = (
            f"no {discom} / {category} version in force on {on_date}"
            if known
            else f"unknown DISCOM/category {discom} / {category}"
        )
    else:
        why = "no DISCOM given"
    if flat_rate is not None:
        flat = flat_table(flat_rate, flat_export_credit, on_date=on_date)
        return TariffSelection(flat, True, f"{why}; using the flat rate from the home profile")
    generic = reg.find(GENERIC_DISCOM, GENERIC_CATEGORY, on_date)
    if generic is None:
        raise TariffNotFound(f"{why}; and no generic table in force on {on_date}")
    return TariffSelection(generic, True, f"{why}; using the generic illustrative table")
