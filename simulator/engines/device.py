"""
Virtual household devices with realistic, weather- and habit-driven behaviour.

Definitions live in `config.DEFAULT_DEVICE_CATALOG` (or DEVICE_CATALOG_JSON /
onboarding). To add a device, append a catalog dict. To remove one, delete it.

Known `device_type`s get a physical model; the catalog `schedule` still sets
the habitual time windows and thresholds:

- refrigerator     compressor duty cycle rises with room heat and door
                   openings at meal times; nightly defrost; idle electronics.
- washing_machine  runs on some days (more on Sundays); fill → wash → rinse
                   → spin → drain phases; start time varies around the window.
- water_heater     geyser need follows the season (long winter runs, short or
                   skipped in summer); evening run mostly in winter.
- air_conditioner  inverter AC: thermostat with hysteresis and minimum run
                   time, pull-down at full power then modulates with outdoor
                   heat; night use when it is hot; off when nobody is home.
- water_pump       tank-fill runs in the morning (and sometimes evening);
                   motor inrush is visible at ≤2 s tick intervals.
- ev_charger       plugs in on some evenings, charges until the car needs no
                   more energy (CC then taper), then idles plugged in.

Unknown types fall back to the schedule modes: cyclic, windows,
temperature_or_windows, always_on.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.engines import randomness as rnd
from simulator.models import DeviceReading, WeatherRecord

_IMPORTANT_POWER_KW = 1.5
_IDLE_ELECTRONICS_KW = {
    "refrigerator": 0.004,
    "washing_machine": 0.002,
    "ev_charger": 0.005,
    "air_conditioner": 0.002,
}

_TYPE_ALIASES = {
    "fridge": "refrigerator",
    "ac": "air_conditioner",
    "geyser": "water_heater",
    "ev": "ev_charger",
    "pump": "water_pump",
    "pool_pump": "water_pump",
    "wash": "washing_machine",
}


def _device_priority(critical: bool, rated_power_kw: float) -> str:
    if critical:
        return "critical"
    if rated_power_kw >= _IMPORTANT_POWER_KW:
        return "important"
    return "flexible"


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _minutes(value: str) -> float:
    parsed = _parse_hhmm(value)
    return parsed.hour * 60.0 + parsed.minute


def _in_window(local: datetime, start: str, end: str) -> bool:
    current = local.timetz().replace(tzinfo=None)
    start_t = _parse_hhmm(start)
    end_t = _parse_hhmm(end)
    if start_t <= end_t:
        return start_t <= current < end_t
    return current >= start_t or current < end_t


def _windows(spec: dict[str, Any], default: list[dict[str, str]]) -> list[dict[str, str]]:
    schedule = spec.get("schedule") or {}
    windows = schedule.get("windows") or []
    return windows or default


def _minute_of_day(local: datetime) -> float:
    return local.hour * 60.0 + local.minute + local.second / 60.0


def _day_mean_temperature(weather: WeatherRecord, local: datetime) -> float:
    """Rough daily mean from the current reading (max ~15:00, min ~06:00)."""
    hours = local.hour + local.minute / 60.0
    return weather.temperature_c - 5.0 * math.cos(2.0 * math.pi * (hours - 15.0) / 24.0)


class DeviceEngine:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.catalog: list[dict[str, Any]] = config.device_catalog()
        self._state: dict[str, dict[str, Any]] = {}
        self._plans: dict[tuple[str, date, str], Any] = {}

    # ---- public -----------------------------------------------------------------

    def step(
        self,
        ts: datetime,
        weather: WeatherRecord,
        scenario: str | None = None,
        occupancy: float | None = None,
    ) -> list[DeviceReading]:
        tz = ZoneInfo(self.config.timezone)
        local = ts.astimezone(tz)
        scenario = scenario or self.config.scenario
        occupancy = 0.6 if occupancy is None else occupancy
        self._prune(local.date())
        readings: list[DeviceReading] = []
        for spec in self.catalog:
            device_id = spec["device_id"]
            rated = float(spec["rated_power_kw"])
            power, mode = self._power(spec, local, weather, scenario, occupancy)
            power = max(0.0, power)
            state = self._state_for(device_id, local.date())
            interval_kwh = power * self.config.interval_hours
            on = power >= max(0.02, 0.1 * rated)
            state["energy_today_kwh"] += interval_kwh
            if on:
                state["runtime_today_s"] += self.config.simulation_interval_seconds
            critical = bool(spec.get("critical", False))
            readings.append(
                DeviceReading(
                    device_id=device_id,
                    device_name=spec["device_name"],
                    device_type=spec["device_type"],
                    rated_power_kw=rated,
                    current_state="on" if on else ("standby" if power > 0 else "off"),
                    current_power_kw=round(power, 4),
                    energy_interval_kwh=round(interval_kwh, 6),
                    critical=critical,
                    controllable=bool(spec.get("controllable", True)),
                    device_priority=_device_priority(critical, rated),
                    operating_mode=mode,
                    energy_today_kwh=round(state["energy_today_kwh"], 4),
                    runtime_today_minutes=round(state["runtime_today_s"] / 60.0, 1),
                )
            )
        return readings

    def shed(self, readings: list[DeviceReading], device_ids: set[str], mode: str) -> list[DeviceReading]:
        """Switch devices off for this tick (load manager / power cut)."""
        updated: list[DeviceReading] = []
        for reading in readings:
            if reading.device_id not in device_ids or reading.current_power_kw <= 0.0:
                updated.append(reading)
                continue
            state = self._state.get(reading.device_id)
            energy_today = reading.energy_today_kwh
            runtime_min = reading.runtime_today_minutes
            if state is not None:
                state["energy_today_kwh"] = max(0.0, state["energy_today_kwh"] - reading.energy_interval_kwh)
                if reading.current_state == "on":
                    state["runtime_today_s"] = max(
                        0.0, state["runtime_today_s"] - self.config.simulation_interval_seconds
                    )
                energy_today = round(state["energy_today_kwh"], 4)
                runtime_min = round(state["runtime_today_s"] / 60.0, 1)
            updated.append(
                reading.model_copy(
                    update={
                        "current_state": "off",
                        "current_power_kw": 0.0,
                        "energy_interval_kwh": 0.0,
                        "operating_mode": mode,
                        "energy_today_kwh": energy_today,
                        "runtime_today_minutes": runtime_min,
                    }
                )
            )
        return updated

    def state(self) -> dict[str, Any]:
        return {
            key: {**value, "day": value["day"].isoformat() if value.get("day") else None}
            for key, value in self._state.items()
            if key != "__ac__"
        } | {"__ac__": self._state.get("__ac__", {})}

    def restore(self, data: dict[str, Any]) -> None:
        for key, value in (data or {}).items():
            if key == "__ac__":
                self._state[key] = dict(value)
                continue
            restored = dict(value)
            restored["day"] = date.fromisoformat(restored["day"]) if restored.get("day") else None
            self._state[key] = restored

    # ---- internals ---------------------------------------------------------------

    def _prune(self, today: date) -> None:
        if len(self._plans) < 64:
            return
        cutoff = today - timedelta(days=2)
        self._plans = {key: value for key, value in self._plans.items() if key[1] >= cutoff}

    def _state_for(self, device_id: str, day: date) -> dict[str, Any]:
        state = self._state.get(device_id)
        if state is None or state.get("day") != day:
            state = {"day": day, "energy_today_kwh": 0.0, "runtime_today_s": 0.0}
            self._state[device_id] = state
        return state

    def _ac_state(self, device_id: str) -> dict[str, Any]:
        bucket = self._state.setdefault("__ac__", {})
        return bucket.setdefault(device_id, {"on": False, "changed": None})

    def _seed(self) -> int:
        return self.config.household_seed

    def _power(
        self,
        spec: dict[str, Any],
        local: datetime,
        weather: WeatherRecord,
        scenario: str,
        occupancy: float,
    ) -> tuple[float, str]:
        device_type = _TYPE_ALIASES.get(spec["device_type"], spec["device_type"])
        rated = float(spec["rated_power_kw"])

        if (
            scenario in ("high_evening_load", "guests_party")
            and device_type == "air_conditioner"
            and (17 <= local.hour <= 23 if scenario == "high_evening_load" else 18 <= local.hour <= 23)
            and _day_mean_temperature(weather, local) >= 24.0
        ):
            return rated, "forced_cooling"

        handler = {
            "refrigerator": self._refrigerator,
            "washing_machine": self._washing_machine,
            "water_heater": self._water_heater,
            "air_conditioner": self._air_conditioner,
            "water_pump": self._water_pump,
            "ev_charger": self._ev_charger,
        }.get(device_type)
        if handler is None:
            return self._legacy(spec, local, weather)
        return handler(spec, local, weather, scenario, occupancy)

    def _noise(self, local: datetime, device_id: str, spread: float = 0.08, period: float = 180.0) -> float:
        return 1.0 + spread * (rnd.smooth_noise(local.timestamp(), period, self._seed(), device_id) - 0.5)

    # refrigerator ---------------------------------------------------------------

    def _refrigerator(self, spec, local, weather, scenario, occupancy) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        device_id = spec["device_id"]
        minute = _minute_of_day(local)
        if 160.0 <= minute < 180.0:
            return rated * 1.4, "defrost"
        room = 26.0 + 0.55 * (weather.temperature_c - 26.0)
        duty = 0.28 + 0.013 * (room - 22.0)
        meal = local.hour in (7, 8, 12, 13, 19, 20, 21)
        if meal and occupancy > 0.3 and scenario != "vacation":
            duty += 0.07
        duty = min(0.8, max(0.2, duty))
        period = 2400.0
        phase = rnd.uniform(self._seed(), device_id, "phase")
        position = (local.timestamp() / period + phase) % 1.0
        if position < duty:
            surge = 1.25 if position < 0.015 else 1.0
            return rated * surge * self._noise(local, device_id, 0.06), "compressor_on"
        return _IDLE_ELECTRONICS_KW["refrigerator"], "compressor_off"

    # washing machine ---------------------------------------------------------------

    def _wash_plan(self, spec, day: date, scenario: str) -> list[tuple[float, float]]:
        key = (spec["device_id"], day, scenario)
        if key in self._plans:
            return self._plans[key]
        seed = self._seed()
        device_id = spec["device_id"]
        weekday = day.weekday()
        probability = {6: 0.8, 5: 0.55}.get(weekday, 0.4)
        if scenario == "vacation":
            probability = 0.0
        elif scenario == "guests_party":
            probability = max(probability, 0.6)
        runs: list[tuple[float, float]] = []
        if rnd.chance(probability, seed, device_id, day, "runs"):
            window = _windows(spec, [{"start": "07:00", "end": "08:00"}])[0]
            start = _minutes(window["start"]) + rnd.uniform_between(-20.0, 120.0, seed, device_id, day, "start")
            duration = rnd.uniform_between(50.0, 80.0, seed, device_id, day, "dur")
            runs.append((start, duration))
            if weekday == 6 and rnd.chance(0.5, seed, device_id, day, "second"):
                runs.append((start + duration + rnd.uniform_between(10.0, 60.0, seed, device_id, day, "gap"), duration))
        self._plans[key] = runs
        return runs

    def _washing_machine(self, spec, local, weather, scenario, occupancy) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        minute = _minute_of_day(local)
        for start, duration in self._wash_plan(spec, local.date(), scenario):
            if start <= minute < start + duration:
                progress = (minute - start) / duration
                agitation = rnd.uniform(self._seed(), spec["device_id"], int(local.timestamp() // 30))
                if progress < 0.08:
                    return rated * 0.08, "fill"
                if progress < 0.55:
                    return rated * (0.30 + 0.25 * agitation), "wash"
                if progress < 0.80:
                    return rated * (0.25 + 0.2 * agitation), "rinse"
                if progress < 0.95:
                    return rated * (0.9 + 0.1 * agitation), "spin"
                return rated * 0.15, "drain"
        return _IDLE_ELECTRONICS_KW["washing_machine"], "idle"

    # water heater ---------------------------------------------------------------

    def _heater_plan(self, spec, day: date, need: float, scenario: str) -> list[tuple[float, float, str]]:
        key = (spec["device_id"], day, scenario)
        if key in self._plans:
            return self._plans[key]
        seed = self._seed()
        device_id = spec["device_id"]
        windows = _windows(
            spec,
            [{"start": "06:00", "end": "07:00"}, {"start": "18:00", "end": "19:00"}],
        )
        runs: list[tuple[float, float, str]] = []
        if scenario != "vacation":
            for index, window in enumerate(windows):
                start = _minutes(window["start"])
                morning = start < 12 * 60
                probability = (0.4 + 0.6 * need) if morning else 0.8 * need
                if not rnd.chance(probability, seed, device_id, day, index, "use"):
                    continue
                duration = (12.0 + 45.0 * need) if morning else (10.0 + 25.0 * need)
                if scenario == "guests_party":
                    duration *= 1.4
                begin = start + rnd.uniform_between(-15.0, 25.0, seed, device_id, day, index, "start")
                runs.append((begin, duration, "heating"))
                if need > 0.5:
                    runs.append((begin + duration + 35.0, 4.0, "reheat"))
        self._plans[key] = runs
        return runs

    def _water_heater(self, spec, local, weather, scenario, occupancy) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        plan_key = (spec["device_id"], local.date(), scenario)
        if plan_key not in self._plans:
            mean_t = _day_mean_temperature(weather, local)
            need = min(1.0, max(0.08, (29.0 - mean_t) / 12.0))
            if scenario == "heatwave":
                need = 0.05
            self._heater_plan(spec, local.date(), need, scenario)
        minute = _minute_of_day(local)
        for start, duration, mode in self._plans[plan_key]:
            if start <= minute < start + duration:
                return rated * self._noise(local, spec["device_id"], 0.03), mode
        return 0.0, "thermostat_satisfied"

    # air conditioner ---------------------------------------------------------------

    def _air_conditioner(self, spec, local, weather, scenario, occupancy) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        device_id = spec["device_id"]
        schedule = spec.get("schedule") or {}
        threshold = float(schedule.get("on_above_c", 29.0))
        feels = weather.apparent_temperature_c if weather.apparent_temperature_c is not None else weather.temperature_c
        hour = local.hour
        sleeping = hour >= 22 or hour < 6
        in_window = any(
            _in_window(local, w["start"], w["end"])
            for w in _windows(spec, [])
        )

        # Cost-conscious households tolerate a few degrees above the nominal
        # threshold during the day and use fans first; tolerance varies by day.
        tolerance = rnd.uniform_between(1.5, 5.0, self._seed(), device_id, "ac-tolerance", local.date())
        if scenario == "heatwave":
            tolerance *= 0.5
        if scenario == "vacation":
            want_on, want_keep = False, False
        elif sleeping:
            want_on = feels >= threshold - 1.0
            want_keep = feels >= threshold - 3.5
        elif occupancy >= 0.5 or in_window:
            day_trigger = threshold + (0.0 if in_window else tolerance)
            # Acclimatised to a cool season: a warm winter afternoon gets fans, not AC.
            if _day_mean_temperature(weather, local) < 24.0:
                day_trigger += 4.0
            want_on = feels >= day_trigger
            want_keep = feels >= day_trigger - 3.0
        else:
            want_on, want_keep = False, False

        state = self._ac_state(device_id)
        changed: datetime | None = (
            datetime.fromisoformat(state["changed"]) if state.get("changed") else None
        )
        since_change = (local - changed).total_seconds() / 60.0 if changed else 1e9
        if state["on"]:
            if not want_keep and since_change >= 20.0:
                state["on"] = False
                state["changed"] = local.isoformat()
                since_change = 0.0
        elif want_on and since_change >= 10.0:
            state["on"] = True
            state["changed"] = local.isoformat()
            since_change = 0.0

        if not state["on"]:
            return _IDLE_ELECTRONICS_KW["air_conditioner"], "off"
        if since_change < 12.0:
            return rated * self._noise(local, device_id, 0.04), "pull_down"
        # Inverter compressor modulates after pull-down: ~40 % on a mild
        # night, ~90 % at 38 °C+ outside.
        load = 0.4 + 0.5 * min(1.0, max(0.0, (weather.temperature_c - 24.0) / 14.0))
        if load < 0.45:
            cycle = (local.timestamp() / 900.0 + rnd.uniform(self._seed(), device_id, "cyc")) % 1.0
            if cycle >= 0.7:
                return 0.05, "fan_only"
        return rated * load * self._noise(local, device_id, 0.08), "cooling"

    # water pump ---------------------------------------------------------------

    def _pump_plan(self, spec, day: date, scenario: str) -> list[tuple[float, float]]:
        key = (spec["device_id"], day, scenario)
        if key in self._plans:
            return self._plans[key]
        seed = self._seed()
        device_id = spec["device_id"]
        window = _windows(spec, [{"start": "06:30", "end": "07:00"}])[0]
        runs: list[tuple[float, float]] = []
        morning_probability = 0.3 if scenario == "vacation" else 0.95
        if rnd.chance(morning_probability, seed, device_id, day, "am"):
            start = _minutes(window["start"]) + rnd.uniform_between(-15.0, 20.0, seed, device_id, day, "amstart")
            runs.append((start, rnd.uniform_between(15.0, 35.0, seed, device_id, day, "amdur")))
        if scenario != "vacation" and rnd.chance(0.45, seed, device_id, day, "pm"):
            start = rnd.uniform_between(18.0 * 60, 20.0 * 60, seed, device_id, day, "pmstart")
            runs.append((start, rnd.uniform_between(10.0, 20.0, seed, device_id, day, "pmdur")))
        self._plans[key] = runs
        return runs

    def _water_pump(self, spec, local, weather, scenario, occupancy) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        minute = _minute_of_day(local)
        for start, duration in self._pump_plan(spec, local.date(), scenario):
            if start <= minute < start + duration:
                if self.config.simulation_interval_seconds <= 2 and minute - start < 2.0 / 60.0:
                    return rated * 3.0, "motor_start"
                return rated * self._noise(local, spec["device_id"], 0.06), "pumping"
        return 0.0, "off"

    # EV charger ---------------------------------------------------------------

    def _ev_session(self, spec, day: date, scenario: str) -> tuple[datetime, datetime, float] | None:
        key = (spec["device_id"], day, scenario)
        if key in self._plans:
            return self._plans[key]
        seed = self._seed()
        device_id = spec["device_id"]
        tz = ZoneInfo(self.config.timezone)
        weekday = day.weekday()
        probability = 0.25 if weekday >= 5 else 0.4
        if scenario == "ev_heavy":
            probability = 1.0
        elif scenario == "vacation":
            probability = 0.0
        session = None
        if rnd.chance(probability, seed, device_id, day, "plug"):
            window = _windows(spec, [{"start": "22:00", "end": "06:00"}])[0]
            start_min = _minutes(window["start"]) + rnd.uniform_between(-120.0, 45.0, seed, device_id, day, "start")
            end_min = _minutes(window["end"])
            if end_min <= _minutes(window["start"]):
                end_min += 24 * 60
            midnight = datetime.combine(day, time(0, 0), tzinfo=tz)
            start = midnight + timedelta(minutes=start_min)
            end = midnight + timedelta(minutes=max(end_min, start_min + 60.0))
            low, high = (15.0, 24.0) if scenario == "ev_heavy" else (5.0, 13.0)
            energy = rnd.uniform_between(low, high, seed, device_id, day, "kwh")
            session = (start, end, energy)
        self._plans[key] = session
        return session

    def _ev_charger(self, spec, local, weather, scenario, occupancy) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        for offset in (0, 1):
            day = local.date() - timedelta(days=offset)
            session = self._ev_session(spec, day, scenario)
            if session is None:
                continue
            start, end, energy = session
            if not (start <= local < end):
                continue
            power = rated * 0.97
            taper_energy = 0.12 * energy
            main_hours = (energy - taper_energy) / power
            taper_hours = taper_energy / (0.7 * power)
            elapsed = (local - start).total_seconds() / 3600.0
            if elapsed < main_hours:
                return power * self._noise(local, spec["device_id"], 0.02), "charging"
            if elapsed < main_hours + taper_hours:
                frac = (elapsed - main_hours) / taper_hours
                return power * (1.0 - 0.6 * frac), "charging_taper"
            return _IDLE_ELECTRONICS_KW["ev_charger"], "plugged_full"
        return 0.0, "unplugged"

    # legacy schedule modes -------------------------------------------------------

    def _legacy(self, spec: dict[str, Any], local: datetime, weather: WeatherRecord) -> tuple[float, str]:
        rated = float(spec["rated_power_kw"])
        schedule = spec.get("schedule") or {"mode": "always_on"}
        mode = schedule.get("mode", "always_on")
        on = False
        if mode == "always_on":
            on = True
        elif mode == "cyclic":
            on_minutes = int(schedule.get("on_minutes", 25))
            cycle_minutes = int(schedule.get("cycle_minutes", 45))
            minute_of_day = local.hour * 60 + local.minute
            on = (minute_of_day % cycle_minutes) < on_minutes
        elif mode == "windows":
            on = any(_in_window(local, w["start"], w["end"]) for w in schedule.get("windows", []))
        elif mode == "temperature_or_windows":
            window_on = any(_in_window(local, w["start"], w["end"]) for w in schedule.get("windows", []))
            on = window_on or weather.temperature_c >= float(schedule.get("on_above_c", 29.0))
        return (rated, "on") if on else (0.0, "off")
