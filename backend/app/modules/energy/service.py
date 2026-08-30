"""
Energy business logic: normalize ingest, project live views, aggregate kWh.

The store is in-memory. Aggregation walks the ring buffer — sufficient until
a time-series table exists. Monthly summaries are not offered here because
the buffer cannot represent a month.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.exceptions import EnergyNotFoundError
from app.modules.energy.schemas import (
    BatteryStatusOut,
    DailySummaryOut,
    DevicesOut,
    EnergyFlows,
    EnergyTotals,
    GridStatusOut,
    HistoryOut,
    HomeLocationOut,
    HourlyBucketOut,
    HourlySummaryOut,
    LiveEnergyOut,
    LoadSnapshotOut,
    SolarSnapshotOut,
    TelemetryRecord,
    WeatherOut,
)
from app.modules.energy.store import EnergyStore
from app.modules.energy.timescale_store import TimescaleTelemetryStore
from app.modules.energy.redis_live import RedisLiveStore

logger = logging.getLogger("volta.energy")

_DEFAULT_INTERVAL_HOURS = 60.0 / 3600.0


def _round_kwh(value: float) -> float:
    return round(value, 6)


def _extra(record: TelemetryRecord) -> dict[str, Any]:
    extra = record.model_extra or {}
    return extra if isinstance(extra, dict) else {}


def infer_interval_hours(record: TelemetryRecord) -> float:
    """
    Recover the tick length so charge/discharge kWh can be derived from kW.

    Prefer solar or load interval energy / power (what the simulator already
    computed). Fall back to one simulated minute.
    """
    if record.solar_power_kw > 1e-6:
        hours = record.solar_energy_interval_kwh / record.solar_power_kw
        if 0 < hours <= 1.0:
            return hours
    if record.home_load_power_kw > 1e-6:
        hours = record.home_consumption_interval_kwh / record.home_load_power_kw
        if 0 < hours <= 1.0:
            return hours
    return _DEFAULT_INTERVAL_HOURS


def apply_energy_balance(record: TelemetryRecord, tolerance_kw: float) -> TelemetryRecord:
    """Recompute AC-bus check so a producer cannot self-certify a bad tick."""
    inputs = (
        record.solar_power_kw
        + record.grid_import_power_kw
        + record.battery_discharge_power_kw
    )
    outputs = (
        record.home_load_power_kw
        + record.grid_export_power_kw
        + record.battery_charge_power_kw
    )
    error = inputs - outputs
    valid = abs(error) <= tolerance_kw
    payload = record.model_dump()
    payload["energy_balance_error_kw"] = round(error, 6)
    payload["energy_balance_valid"] = valid
    payload["energy_balance_status"] = "valid" if valid else "warning"
    warnings = list(payload.get("warnings") or [])
    if not valid:
        msg = f"energy_balance_error_kw={error:.4f} exceeds tolerance={tolerance_kw:.4f}"
        if msg not in warnings:
            warnings.append(msg)
        logger.warning("household=%s %s", record.household_id, msg)
    payload["warnings"] = warnings
    return TelemetryRecord.model_validate(payload)


def normalize_record(record: TelemetryRecord, tolerance_kw: float) -> TelemetryRecord:
    hours = infer_interval_hours(record)
    payload = record.model_dump()

    if record.grid_import_interval_kwh <= 0 and record.grid_import_power_kw > 0:
        payload["grid_import_interval_kwh"] = _round_kwh(record.grid_import_power_kw * hours)
    if record.grid_export_interval_kwh <= 0 and record.grid_export_power_kw > 0:
        payload["grid_export_interval_kwh"] = _round_kwh(record.grid_export_power_kw * hours)
    if record.battery_charge_interval_kwh <= 0 and record.battery_charge_power_kw > 0:
        payload["battery_charge_interval_kwh"] = _round_kwh(record.battery_charge_power_kw * hours)
    if record.battery_discharge_interval_kwh <= 0 and record.battery_discharge_power_kw > 0:
        payload["battery_discharge_interval_kwh"] = _round_kwh(
            record.battery_discharge_power_kw * hours
        )

    normalized = TelemetryRecord.model_validate(payload)
    return apply_energy_balance(normalized, tolerance_kw)


def _totals(records: list[TelemetryRecord]) -> EnergyTotals:
    solar = home = imported = exported = charge = discharge = 0.0
    for row in records:
        solar += row.solar_energy_interval_kwh
        home += row.home_consumption_interval_kwh
        imported += row.grid_import_interval_kwh
        exported += row.grid_export_interval_kwh
        charge += row.battery_charge_interval_kwh
        discharge += row.battery_discharge_interval_kwh
    return EnergyTotals(
        solar_generation_kwh=_round_kwh(solar),
        home_consumption_kwh=_round_kwh(home),
        grid_import_kwh=_round_kwh(imported),
        grid_export_kwh=_round_kwh(exported),
        battery_charge_kwh=_round_kwh(charge),
        battery_discharge_kwh=_round_kwh(discharge),
    )


def _local_date(ts: datetime) -> date:
    return ts.date()


def _timezone_name(ts: datetime) -> str:
    tz = ts.tzinfo or timezone.utc
    name = getattr(tz, "key", None)
    if isinstance(name, str) and name:
        return name
    return ts.tzname() or "UTC"


def to_battery(record: TelemetryRecord) -> BatteryStatusOut:
    extra = _extra(record)
    fault = extra.get("battery_fault_code")
    return BatteryStatusOut(
        soc_percent=record.battery_soc_percent,
        soh_percent=record.battery_soh_percent,
        charge_power_kw=record.battery_charge_power_kw,
        discharge_power_kw=record.battery_discharge_power_kw,
        energy_available_kwh=record.battery_energy_available_kwh,
        status=record.battery_status,
        temperature_c=extra.get("battery_temperature_c"),
        fault_code=str(fault) if fault not in (None, "") else None,
    )


def to_grid(record: TelemetryRecord) -> GridStatusOut:
    extra = _extra(record)
    return GridStatusOut(
        status=record.grid_status,
        import_power_kw=record.grid_import_power_kw,
        export_power_kw=record.grid_export_power_kw,
        total_import_kwh=record.total_import_kwh,
        total_export_kwh=record.total_export_kwh,
        voltage_v=extra.get("grid_voltage_v"),
        frequency_hz=extra.get("grid_frequency_hz"),
    )


def _location_out(extra: dict[str, Any]) -> HomeLocationOut | None:
    loc = extra.get("location")
    if isinstance(loc, str) and loc.strip():
        text = loc.strip()
        return HomeLocationOut(name=text, label=text, query=text)
    if not isinstance(loc, dict):
        return None
    name = loc.get("name") or loc.get("query")
    if not name and loc.get("latitude") is None:
        return None
    admin1 = loc.get("admin1")
    country = loc.get("country")
    parts = [str(name)] if name else []
    if admin1 and admin1 != name:
        parts.append(str(admin1))
    if country:
        parts.append(str(country))
    return HomeLocationOut(
        name=str(name) if name else None,
        label=", ".join(parts) if parts else None,
        latitude=loc.get("latitude"),
        longitude=loc.get("longitude"),
        timezone=loc.get("timezone"),
        admin1=str(admin1) if admin1 else None,
        country=str(country) if country else None,
        query=str(loc["query"]) if loc.get("query") else None,
        source=str(loc["source"]) if loc.get("source") else None,
    )


def _is_gps_or_unnamed(loc: object) -> bool:
    if loc is None:
        return True
    if isinstance(loc, str):
        return not loc.strip()
    if not isinstance(loc, dict):
        return True
    name = str(loc.get("name") or "").strip()
    return loc.get("source") == "browser-gps" or name in {"", "Current location"}


def stamp_onboarding_place(
    record: TelemetryRecord, place: str | None
) -> TelemetryRecord:
    """Keep the onboarding city on the tick when GPS leftover coords are stored."""
    text = (place or "").strip()
    if not text:
        return record
    extra = _extra(record)
    if not _is_gps_or_unnamed(extra.get("location")):
        return record
    payload = record.model_dump()
    payload["location"] = {
        "name": text,
        "label": text,
        "query": text,
        "source": "onboarding",
    }
    return TelemetryRecord.model_validate(payload)


def to_live(record: TelemetryRecord) -> LiveEnergyOut:
    extra = _extra(record)
    warnings = extra.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = []
    scenario = extra.get("scenario")
    return LiveEnergyOut(
        household_id=record.household_id,
        timestamp=record.timestamp,
        data_source=record.data_source,
        data_quality=record.data_quality,
        solar=SolarSnapshotOut(
            power_kw=record.solar_power_kw,
            energy_today_kwh=record.solar_energy_today_kwh,
            energy_total_kwh=record.solar_energy_total_kwh,
            energy_interval_kwh=record.solar_energy_interval_kwh,
        ),
        load=LoadSnapshotOut(
            power_kw=record.home_load_power_kw,
            consumption_today_kwh=record.home_consumption_today_kwh,
            consumption_total_kwh=record.home_consumption_total_kwh,
            consumption_interval_kwh=record.home_consumption_interval_kwh,
        ),
        battery=to_battery(record),
        grid=to_grid(record),
        flows=EnergyFlows(
            solar_to_home_kw=record.solar_to_home_kw,
            solar_to_battery_kw=record.solar_to_battery_kw,
            solar_to_grid_kw=record.solar_to_grid_kw,
            battery_to_home_kw=record.battery_to_home_kw,
            grid_to_home_kw=record.grid_to_home_kw,
            unserved_load_kw=record.unserved_load_kw,
            solar_curtailed_kw=float(extra.get("solar_curtailed_kw") or 0.0),
            grid_to_battery_kw=float(extra.get("grid_to_battery_kw") or 0.0),
        ),
        devices=list(record.devices),
        weather=record.weather,
        location=_location_out(extra),
        energy_balance_status=record.energy_balance_status,
        energy_balance_error_kw=record.energy_balance_error_kw,
        energy_balance_valid=record.energy_balance_valid,
        scenario=str(scenario) if scenario else None,
        warnings=[str(item) for item in warnings],
    )


class EnergyService:
    def __init__(
        self,
        store: EnergyStore,
        timescale: TimescaleTelemetryStore | None = None,
        redis_live: RedisLiveStore | None = None,
    ) -> None:
        self.store = store
        self.timescale = timescale
        self.redis_live = redis_live
        self._tolerance_kw = settings.ENERGY_BALANCE_TOLERANCE_KW

    async def ingest(self, record: TelemetryRecord) -> TelemetryRecord:
        normalized = normalize_record(record, self._tolerance_kw)
        await self.store.put(normalized)
        if self.timescale is not None:
            await self.timescale.upsert(normalized)
        if self.redis_live is not None:
            try:
                await self.redis_live.set_and_publish(normalized)
            except Exception:
                logger.exception("redis live update failed after ingest")
        return normalized

    async def _require_latest(self, household_id: str) -> TelemetryRecord:
        if self.redis_live is not None:
            latest = await self.redis_live.get_latest(household_id)
            if latest is not None:
                return latest
        if self.timescale is not None:
            latest = await self.timescale.get_latest(household_id)
            if latest is not None:
                return latest
        latest = await self.store.get_latest(household_id)
        if latest is None:
            raise EnergyNotFoundError(
                f"No telemetry found for household '{household_id}'."
            )
        return latest

    async def get_live(
        self, household_id: str, onboarding_location: str | None = None
    ) -> LiveEnergyOut:
        record = stamp_onboarding_place(
            await self._require_latest(household_id), onboarding_location
        )
        return to_live(record)

    async def get_battery(self, household_id: str) -> BatteryStatusOut:
        return to_battery(await self._require_latest(household_id))

    async def get_grid(self, household_id: str) -> GridStatusOut:
        return to_grid(await self._require_latest(household_id))

    async def get_devices(self, household_id: str) -> DevicesOut:
        latest = await self._require_latest(household_id)
        return DevicesOut(
            household_id=latest.household_id,
            timestamp=latest.timestamp,
            devices=list(latest.devices),
        )

    async def get_weather(self, household_id: str) -> WeatherOut:
        latest = await self._require_latest(household_id)
        return WeatherOut(
            household_id=latest.household_id,
            timestamp=latest.timestamp,
            weather=latest.weather,
        )

    async def get_history(self, household_id: str, limit: int = 120) -> HistoryOut:
        cap = min(limit, 2000)
        if self.timescale is not None:
            records = await self.timescale.get_history(household_id, limit=cap)
            if records:
                return HistoryOut(
                    household_id=household_id,
                    count=len(records),
                    records=records,
                )
        await self._require_latest(household_id)
        records = await self.store.get_history(household_id, limit=cap)
        return HistoryOut(
            household_id=household_id,
            count=len(records),
            records=records,
        )

    async def get_daily(self, household_id: str, day: date | None = None) -> DailySummaryOut:
        latest = await self._require_latest(household_id)
        target = day or _local_date(latest.timestamp)
        tz = latest.timestamp.tzinfo or timezone.utc
        start = datetime(target.year, target.month, target.day, tzinfo=tz)
        end = start + timedelta(days=1)
        if self.timescale is not None:
            rows = await self.timescale.fetch_range(household_id, start, end)
        else:
            history = await self.store.get_history(household_id)
            rows = [row for row in history if _local_date(row.timestamp) == target]
        totals = _totals(rows)
        return DailySummaryOut(
            household_id=household_id,
            date=target,
            timezone=_timezone_name(latest.timestamp),
            reading_count=len(rows),
            period_start=rows[0].timestamp if rows else None,
            period_end=rows[-1].timestamp if rows else None,
            **totals.model_dump(),
        )

    async def get_hourly(self, household_id: str, day: date | None = None) -> HourlySummaryOut:
        latest = await self._require_latest(household_id)
        target = day or _local_date(latest.timestamp)
        tz = latest.timestamp.tzinfo or timezone.utc
        start = datetime(target.year, target.month, target.day, tzinfo=tz)
        end = start + timedelta(days=1)
        if self.timescale is not None:
            rows = await self.timescale.fetch_range(household_id, start, end)
        else:
            history = await self.store.get_history(household_id)
            rows = [row for row in history if _local_date(row.timestamp) == target]

        buckets: dict[datetime, list[TelemetryRecord]] = {}
        for row in rows:
            local = row.timestamp.astimezone(tz)
            hour_start = local.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(hour_start, []).append(row)

        out: list[HourlyBucketOut] = []
        cursor = datetime(target.year, target.month, target.day, tzinfo=tz)
        for _ in range(24):
            group = buckets.get(cursor, [])
            totals = _totals(group)
            out.append(
                HourlyBucketOut(
                    hour_start=cursor,
                    reading_count=len(group),
                    **totals.model_dump(),
                )
            )
            cursor = cursor + timedelta(hours=1)

        return HourlySummaryOut(
            household_id=household_id,
            date=target,
            timezone=_timezone_name(latest.timestamp),
            buckets=out,
        )
