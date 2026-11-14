"""
Forecast engine: hourly solar + load for the next N hours, with honest prediction intervals.

    clean history -> calibrate PR -> weather (or persistence fallback) -> per-hour solar band
                  -> per-hour load estimate -> totals, method labels, confidence, warnings

It is a pure async function of (inputs, weather port, now): no DB, no clock, no globals. The only
I/O is the weather port, whose failure is caught and degraded.

Slots are whole local hours starting at the next hour boundary (or `now` if it is on one), so
"today remaining" never double-counts hours that have already happened. Weather is read at each
slot's midpoint.

Prediction interval (nominal p10 / p50 / p90, NOT empirically validated):
  solar  p50 = physics at the forecast cloud; p10/p90 re-run the physics at cloud +/- sigma(lead,
         source) and widen by a relative model error (smaller when calibrated). Sigma grows with
         lead and is larger for persistence/climatology than for a weather model.
  load   empirical day-to-day spread of that hour (or a flat +/-40 %), widened by 1 + lead/96.

Known limits: the weather model's own spread is not queried (a scenario band stands in), solar and
load errors are not combined into a net interval, soiling/shading drift is only seen via
calibration, and with no observation the fallback weather is a climatological constant.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .calibration import Calibration, calibrate_performance_ratio
from .load import LoadModel, build_load_model
from .models import (
    MAX_HORIZON_HOURS,
    ForecastInputs,
    ForecastWeatherError,
    HistoryPoint,
    WeatherForecastPort,
    WeatherSeries,
)
from .result import (
    Confidence,
    DataPointsUsed,
    ForecastMethods,
    ForecastResult,
    ForecastSlot,
    ForecastTotals,
)
from .solar import ArrayParams, slot_power_kw
from .weather import PersistenceWeather

HISTORY_WINDOW = timedelta(days=14)
_FUTURE_TOLERANCE = timedelta(minutes=5)

# Cloud-cover scenario band (percentage points) by weather source kind: base + per_hour * lead, capped.
_SIGMA = {
    "model": (8.0, 0.5, 30.0),
    "persistence": (20.0, 1.0, 45.0),
    "climatology": (40.0, 0.0, 40.0),
}
_MODEL_ERROR = (0.08, 0.12, 0.15)  # calibrated with >= 40 samples / calibrated with fewer / default PR
_WELL_CALIBRATED_SAMPLES = 40


def cloud_sigma_pct(kind: str, lead_hours: float) -> float:
    base, per_hour, cap = _SIGMA[kind]
    return min(cap, base + per_hour * max(0.0, lead_hours))


def _clean_history(
    history: tuple[HistoryPoint, ...] | list[HistoryPoint], now: datetime, warnings: list[str]
) -> list[HistoryPoint]:
    cleaned: list[HistoryPoint] = []
    future = bad = 0
    for p in history:
        ts = p.timestamp if p.timestamp.tzinfo else p.timestamp.replace(tzinfo=timezone.utc)
        if ts > now + _FUTURE_TOLERANCE:
            future += 1
            continue
        if now - ts > HISTORY_WINDOW:
            continue

        def ok(v: float | None) -> float | None:
            if v is None:
                return None
            return float(v) if math.isfinite(v) and v >= 0 else None

        solar, load = ok(p.solar_kw), ok(p.load_kw)
        if (p.solar_kw is not None and solar is None) or (p.load_kw is not None and load is None):
            bad += 1
        if solar is None and load is None:
            continue
        cleaned.append(HistoryPoint(ts, solar, load))
    if future:
        warnings.append(f"ignored {future} history record(s) timestamped in the future")
    if bad:
        warnings.append(f"ignored {bad} invalid (negative or non-finite) history value(s)")
    cleaned.sort(key=lambda p: p.timestamp)
    return cleaned


def _slot_starts(now: datetime, tz: ZoneInfo, horizon: int) -> list[datetime]:
    """UTC instants of each hour start; arithmetic in UTC so DST can never repeat/skip a slot."""
    floor = now.astimezone(tz).replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    first = floor if floor >= now else floor + timedelta(hours=1)
    return [first + timedelta(hours=i) for i in range(horizon)]


async def _weather(
    inputs: ForecastInputs, port: WeatherForecastPort, times: list[datetime], now: datetime, warnings: list[str]
) -> tuple[WeatherSeries, bool]:
    """(series, fell_back). A broken weather source must cost accuracy, never the whole answer."""
    try:
        series = await port.hourly(latitude=inputs.latitude, longitude=inputs.longitude, times=times)
        if len(series.hours) != len(times):
            raise ForecastWeatherError(f"weather source returned {len(series.hours)} of {len(times)} hours")
        return series, False
    except (ForecastWeatherError, TimeoutError) as exc:
        fallback = PersistenceWeather(
            observed_cloud_pct=inputs.observed_cloud_cover_pct,
            observed_at=inputs.observed_at or now,
        )
        warnings.append(f"weather forecast unavailable ({exc}); using {'persistence' if inputs.observed_cloud_cover_pct is not None else 'climatological'} cloud cover")
        return await fallback.hourly(latitude=inputs.latitude, longitude=inputs.longitude, times=times), True


def _model_error(calibration: Calibration) -> float:
    if not calibration.calibrated:
        return _MODEL_ERROR[2]
    return _MODEL_ERROR[0] if calibration.samples >= _WELL_CALIBRATED_SAMPLES else _MODEL_ERROR[1]


def _assess_confidence(
    *, calibration: Calibration, series: WeatherSeries, load: LoadModel, horizon: int, fell_back: bool
) -> Confidence:
    score = 0
    reasons: list[str] = []
    if calibration.calibrated:
        score += 1
        reasons.append(f"solar calibrated from {calibration.samples} recent daylight samples")
    else:
        reasons.append("solar uses the default performance ratio (not enough recent solar history)")
    if series.kind == "model":
        score += 2
        reasons.append(f"cloud cover from the {series.source} forecast model")
    elif series.kind == "persistence":
        score += 1
        reasons.append("cloud cover is persistence of the latest observation" + (" (weather forecast unavailable)" if fell_back else ""))
    else:
        reasons.append("cloud cover is a climatological average (no forecast or observation)")
    if load.method == "profile":
        score += 1
        reasons.append(f"load from an hourly profile ({load.days} day(s) of history)")
    elif load.method == "flat":
        reasons.append("load is the latest reading held flat")
    else:
        reasons.append("load forecast unavailable")
    if horizon > 24:
        score -= 1
        reasons.append("horizon beyond 24 h is less reliable")
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    return Confidence(level=level, reasons=reasons)


async def forecast(inputs: ForecastInputs, weather: WeatherForecastPort, now: datetime) -> ForecastResult:
    """Forecast the next `inputs.horizon_hours` (1..48) hours for one home. `now` must be tz-aware."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    now = now.astimezone(timezone.utc)
    tz = ZoneInfo(inputs.timezone)
    warnings: list[str] = []

    horizon = inputs.horizon_hours
    if horizon < 1:
        raise ValueError("horizon_hours must be >= 1")
    if horizon > MAX_HORIZON_HOURS:
        warnings.append(f"horizon clamped from {horizon} to {MAX_HORIZON_HOURS} hours")
        horizon = MAX_HORIZON_HOURS

    history = _clean_history(tuple(inputs.history), now, warnings)
    default_array = ArrayParams(
        latitude=inputs.latitude,
        longitude=inputs.longitude,
        capacity_kwp=inputs.solar_capacity_kwp,
        inverter_kw=inputs.inverter_capacity_kw,
        performance_ratio=inputs.performance_ratio,
        tilt_deg=inputs.tilt_deg,
        azimuth_deg=inputs.azimuth_deg,
    )
    calibration = calibrate_performance_ratio(history, default_array, tz=tz)
    if calibration.calibrated:
        if calibration.clamped:
            warnings.append(
                f"calibration factor {calibration.raw_factor:.2f} outside the allowed range; clamped to {calibration.factor:.2f}"
            )
        if calibration.days < 2:
            warnings.append("solar calibrated from a single day of data; a cloudy day would understate the array")
    else:
        warnings.append(calibration.note)
    array = default_array.with_performance_ratio(inputs.performance_ratio * calibration.factor)

    starts = _slot_starts(now, tz, horizon)
    mids = [s + timedelta(minutes=30) for s in starts]
    series, fell_back = await _weather(inputs, weather, mids, now, warnings)

    load_model = build_load_model(history, now=now, tz=tz)
    if load_model.method == "unknown":
        warnings.append(f"load forecast unavailable: {load_model.note}")
    elif load_model.method == "flat":
        warnings.append(load_model.note)
    elif load_model.missing_hours:
        warnings.append(f"load profile has no data for {load_model.missing_hours} hour(s) of the day; profile mean used there")

    rel_err = _model_error(calibration)
    slots: list[ForecastSlot] = []
    for start, mid, wx in zip(starts, mids, series.hours):
        lead = (mid - now).total_seconds() / 3600.0
        cloud = max(0.0, min(100.0, wx.cloud_cover_pct))
        precip = wx.precipitation_mm or 0.0
        sigma = cloud_sigma_pct(series.kind, lead)

        def power(c: float, p: float = precip) -> float:
            return slot_power_kw(start, array, cloud_cover_percent=c, precipitation_mm=p, ambient_c=wx.temperature_c)

        p50 = power(cloud)
        p10 = min(p50, power(min(100.0, cloud + sigma)) * (1.0 - rel_err))
        p90 = max(p50, min(array.inverter_kw, power(max(0.0, cloud - sigma)) * (1.0 + rel_err)))
        local_start = start.astimezone(tz)
        load = load_model.predict(local_start, lead)
        slots.append(
            ForecastSlot(
                start=local_start,
                lead_hours=round(lead, 2),
                cloud_cover_pct=cloud,
                precipitation_mm=wx.precipitation_mm,
                temperature_c=wx.temperature_c,
                solar_kw_p10=p10,
                solar_kw_p50=p50,
                solar_kw_p90=p90,
                load_kw_p10=None if load is None else load.p10_kw,
                load_kw_p50=None if load is None else load.p50_kw,
                load_kw_p90=None if load is None else load.p90_kw,
            )
        )

    solar_kwh = sum(s.solar_kw_p50 for s in slots)
    load_kwh = None if load_model.method == "unknown" else sum(s.load_kw_p50 or 0.0 for s in slots)
    totals = ForecastTotals(
        solar_kwh=solar_kwh,
        solar_kwh_p10=sum(s.solar_kw_p10 for s in slots),
        solar_kwh_p90=sum(s.solar_kw_p90 for s in slots),
        load_kwh=load_kwh,
        net_kwh=None if load_kwh is None else solar_kwh - load_kwh,
    )
    return ForecastResult(
        generated_at=now.astimezone(tz),
        timezone=inputs.timezone,
        horizon_hours=horizon,
        slots=slots,
        totals=totals,
        methods=ForecastMethods(
            solar="physics+calibrated" if calibration.calibrated else "physics_default",
            load=load_model.method,
            weather=series.source,
            weather_kind=series.kind,
        ),
        confidence=_assess_confidence(
            calibration=calibration, series=series, load=load_model, horizon=horizon, fell_back=fell_back
        ),
        data_points_used=DataPointsUsed(
            solar_calibration=calibration.samples,
            load_profile=load_model.points_used,
            history_total=len(history),
        ),
        performance_ratio=array.performance_ratio,
        calibration_factor=calibration.factor,
        interval_basis={
            "solar": (
                f"p10/p90 re-run the physics at cloud cover +/- {cloud_sigma_pct(series.kind, 0):.0f} to "
                f"{cloud_sigma_pct(series.kind, horizon):.0f} percentage points (grows with lead; {series.kind}-based) "
                f"and +/-{rel_err * 100:.0f}% model error; nominal 80% band, not empirically validated"
            ),
            "load": (
                "day-to-day spread of that hour in recent history, widened 1 + lead/96"
                if load_model.method == "profile"
                else "flat +/-40% around the latest reading, widened 1 + lead/96"
                if load_model.method == "flat"
                else "not applicable (no load forecast)"
            ),
        },
        warnings=warnings,
    )
