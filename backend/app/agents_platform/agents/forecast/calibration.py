"""
Calibrate the array's performance ratio from recent telemetry.

The history has solar power but NOT the cloud cover that was true at the time, so we cannot fit
"actual vs physics-with-weather". Instead we fit the clear-sky envelope: for every daylight sample
we compute actual / clear-sky-physics, then take a HIGH quantile (the best-sky behaviour). Cloud is
applied on top at forecast time, so what must be learned is only "how efficient is this array when
the sky lets it be". A median would bake the recent cloud into the factor and double-count it.

Guards, because this runs on whatever the telemetry happens to contain:
  * sun must be well up (low-sun samples are dominated by horizon/shading and model error);
  * the clear-sky expectation must be a meaningful fraction of the array (no ratios of tiny numbers);
  * samples where the inverter would be clipping are skipped (the ratio saturates);
  * at least `MIN_SAMPLES` usable samples, else the default is used and the caller is told;
  * the factor is clamped to [0.6, 1.1]: a bad meter, a stuck inverter or a cloudy week must not
    be allowed to swing the forecast by more than that.

Known limits: if the whole window was overcast the factor is biased low; one cloudy week and a
clean array look the same until a clear day arrives. The clamp bounds the damage.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timezone, tzinfo

from ._stats import quantile
from .models import HistoryPoint
from .solar import ArrayParams, physics_power_kw, solar_position

FACTOR_MIN = 0.6
FACTOR_MAX = 1.1
MIN_SAMPLES = 12
_MIN_ELEVATION_DEG = 20.0
_MIN_CLEAR_SKY_FRACTION_OF_KWP = 0.10
_MAX_UNCLIPPED_FRACTION_OF_INVERTER = 0.95
_ENVELOPE_QUANTILE = 0.85
_MAX_SAMPLE_RATIO = 1.5


@dataclass(frozen=True, slots=True)
class Calibration:
    factor: float
    samples: int
    days: int
    calibrated: bool
    raw_factor: float | None
    clamped: bool
    note: str


def calibrate_performance_ratio(
    history: Sequence[HistoryPoint],
    array: ArrayParams,
    *,
    tz: tzinfo = timezone.utc,
) -> Calibration:
    """Learn a multiplicative correction to `array.performance_ratio` (1.0 = default is right)."""
    ratios: list[float] = []
    days: set[object] = set()
    for point in history:
        if point.solar_kw is None:
            continue
        if solar_position(point.timestamp, array.latitude, array.longitude).elevation_deg < _MIN_ELEVATION_DEG:
            continue
        clear_kw = physics_power_kw(point.timestamp, array, clip=False)
        if clear_kw < _MIN_CLEAR_SKY_FRACTION_OF_KWP * array.capacity_kwp:
            continue
        if clear_kw > _MAX_UNCLIPPED_FRACTION_OF_INVERTER * array.inverter_kw:
            continue
        ratios.append(min(_MAX_SAMPLE_RATIO, max(0.0, point.solar_kw / clear_kw)))
        days.add(point.timestamp.astimezone(tz).date())
    if len(ratios) < MIN_SAMPLES:
        return Calibration(
            factor=1.0,
            samples=len(ratios),
            days=len(days),
            calibrated=False,
            raw_factor=None,
            clamped=False,
            note=f"only {len(ratios)} usable daylight samples (need {MIN_SAMPLES}); default performance ratio used",
        )
    raw = quantile(ratios, _ENVELOPE_QUANTILE)
    factor = min(FACTOR_MAX, max(FACTOR_MIN, raw))
    return Calibration(
        factor=factor,
        samples=len(ratios),
        days=len(days),
        calibrated=True,
        raw_factor=raw,
        clamped=factor != raw,
        note=f"{len(ratios)} daylight samples over {len(days)} day(s)",
    )
