# Suryaa telemetry simulator

Standalone real-time solar-home data generator for **Suryaa**.

This folder sits next to `frontend/` and `backend/`. It does **not** talk to Redis, Postgres, FastAPI, or any frontend. It prints JSON telemetry and appends the same records to `data/telemetry.jsonl`.

```
volta.ai/
├── backend/
├── frontend/
└── simulator/          ← this package
```

## Installation

Python 3.12+ is required. From the repository root:

```bash
cd /path/to/volta.ai
python3.12 -m venv simulator/.venv
source simulator/.venv/bin/activate
pip install -r simulator/requirements.txt
```

Copy environment defaults if you want to override them:

```bash
cp simulator/.env.example simulator/.env
```

`.env` is optional. If it is missing, the values in `simulator/config.py` are used.

## How to run

Always run from the **repository root** so `python -m simulator.main` can import the package:

```bash
# Fallback weather (no network) — good first run
python -m simulator.main --weather-mode fallback --speed 60 --pretty

# Live Open-Meteo weather for Pimpri-Chinchwad (default lat/lon)
python -m simulator.main --weather-mode live --speed 10 --pretty

# Stop after 12 readings (12 simulated minutes at the default 60s interval)
python -m simulator.main --weather-mode fallback --speed 0 --ticks 12 --pretty

# Backfill a year of 5-minute data with a different realistic situation each day
python -m simulator.main --weather-mode fallback --no-geocode --scenario auto \
  --start-time 2026-01-01T00:00:00 --days 365 --interval-seconds 300 --quiet
```

A year of 5-minute ticks takes about 100 s. Live runs save engine state
(lifetime kWh, battery SOC/SOH, panel soiling, device sessions) to
`data/state/<household_id>.json` every ~30 s and on exit, and resume from it
on the next start. Pass `--fresh-state` to start clean. Backfills (`--start-time`
/ `--days`) never read or write that state.

`--speed 1` waits one wall-clock second per simulated second (real time).  
`--speed 60` prints about one reading per second when the interval is 60s.  
`--speed 0` runs as fast as possible.

## Live dashboard

A single HTML dashboard lives in `frontend/public/suryaa-dashboard.html`. Start the simulator with `--dashboard` so it serves that page and streams telemetry over SSE.

`--dashboard` uses the **current clock** and emits **one reading every second** (`22:04:01`, `22:04:02`, …). Do not pass `--speed 60` for live viewing — that jumped the timestamp by a full minute each tick.

```bash
cd /path/to/volta.ai
source simulator/.venv/bin/activate
python -m simulator.main --dashboard --weather-mode live
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). Refresh the page after starting the command.

Use **Stop** on the dashboard to shut the simulator down (SSE disconnect + process exit) so Open-Meteo weather/geocoding calls stop. That is the same as Ctrl+C when you cannot find the terminal.

```bash
# Offline weather, still one reading per live second
python -m simulator.main --dashboard --weather-mode fallback
```

`--dashboard` also quiets stdout so the terminal is not flooded. Use `--pretty` if you still want JSON printed.

If you pass `--start-time`, the generator switches to simulated time (useful for night/noon tests) instead of the live clock.

### Command-line options

| Flag | Meaning |
|---|---|
| `--interval-seconds` | Simulated seconds between readings (default 60) |
| `--speed` | Wall-clock multiplier |
| `--household-id` | e.g. `home_001` |
| `--solar-capacity-kwp` | PV array size |
| `--battery-capacity-kwh` | Battery nameplate kWh |
| `--start-time` | ISO-8601 clock, e.g. `2026-08-23T12:30:00` |
| `--scenario` | See scenarios below |
| `--output-file` | JSONL path (relative paths are under `simulator/`) |
| `--weather-mode` | `live` \| `fallback` \| `historical-style` |
| `--ticks` | Stop after N readings (0 = until Ctrl+C) |
| `--days` | Backfill N simulated days (sets `--ticks`, speed 0; starts N days ago if no `--start-time`) |
| `--fresh-state` | Ignore saved state from a previous live run |
| `--pretty` | Indent JSON on stdout |
| `--location` | City/place name resolved by Open-Meteo geocoding |
| `--country-code` | ISO country filter (e.g. `IN`) |
| `--no-geocode` | Use `.env` coordinates only |
| `--ingest-url` | FastAPI base URL (ticks are POSTed to `/energy/ingest`, tests/dev only) |
| `--ingest-token` | Shared secret matching backend `INGEST_TOKEN` |
| `--rabbitmq-url` | AMQP URL. Empty / omitted = do not publish. Backend ingest path. |

Production ingest is **RabbitMQ**, not HTTP. The simulator still does not talk to Redis, TimescaleDB, or WebSocket.

```bash
# Slice 2: publish 5 ticks to local Docker RabbitMQ, then exit
python -m simulator.main \
  --rabbitmq-url amqp://volta:volta@localhost:5672/volta \
  --weather-mode fallback --no-geocode \
  --start-time 2026-08-29T12:00:00 --ticks 5 --speed 0 --quiet
```

A failed AMQP publish is logged and skipped — the generator keeps writing JSONL.

With the backend running (`uvicorn main:app --reload` in `backend/`) HTTP ingest still works for tests:

```bash
python -m simulator.main --ingest-url http://127.0.0.1:8000 --ingest-token dev-ingest-token --weather-mode fallback --ticks 5 --speed 0
```

## Open-Meteo weather API

The live weather source is [Open-Meteo](https://open-meteo.com/) ([forecast docs](https://open-meteo.com/en/docs)).

- **No API key** and no sign-up for non-commercial use.
- Data licence is [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — attribution is required.
- Free tier is about **10,000 calls/day**. This simulator fetches once at startup and then every `WEATHER_REFRESH_MINUTES` (default 30), **not** on every telemetry tick.

Default location is Pimpri-Chinchwad / Pune:

- latitude `18.6298`
- longitude `73.7997`
- timezone `Asia/Kolkata`

The request looks like:

```text
https://api.open-meteo.com/v1/forecast
  ?latitude=18.6298
  &longitude=73.7997
  &timezone=Asia/Kolkata
  &hourly=temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,
          precipitation,precipitation_probability,cloud_cover,wind_speed_10m,
          wind_gusts_10m,shortwave_radiation_instant,direct_radiation_instant,
          diffuse_radiation_instant,direct_normal_irradiance_instant,weather_code
  &daily=sunrise,sunset
  &past_days=…&forecast_days=…   # chosen automatically to cover --start-time
  &wind_speed_unit=kmh
```

Radiation uses the `*_instant` variables: the plain ones are averages over the
*preceding* hour, which shifts the PV curve ~30 min late when interpolated.

Hourly keys live in `OPEN_METEO_HOURLY_FIELDS` in `config.py`. Daily keys live in `OPEN_METEO_DAILY_FIELDS`. Mapping from API names to `WeatherRecord` fields is `WEATHER_FIELD_MAP` in `models.py`.

Every weather record is enriched with sun elevation/azimuth, clear-sky GHI,
clearness index, `is_day`, a text `weather_condition` and (when missing)
dew point, apparent temperature, gusts and DNI.

### Weather modes

| Mode | Behaviour |
|---|---|
| `live` | HTTP GET to Open-Meteo. On failure, reuse the last cache (retry after 5 min); if there is no cache, use the synthetic climate and set `data_quality` to `"fallback"`. |
| `fallback` | Never calls the network. Seeded synthetic climate (below). |
| `historical-style` | Same synthetic climate, tagged as a typical-year style series for agent/dashboard work. |

### Synthetic climate (`engines/climate.py`)

Offline weather is not one repeated day. Each day gets a regime — `clear`,
`partly_cloudy`, `overcast`, `rain`, `storm` — from a persistent Markov chain
weighted by the month (Indian monthly normals calibrated on Pune, adjusted for
latitude and elevation; a generic model elsewhere). The regime drives
Tmax/Tmin, dew point, cloud, rain hours, afternoon thunderstorms with gusts,
and north-Indian winter fog. Calibration (10-year mean vs Pune climatology,
kWh/m²/day GHI): Jan 4.8/5.0 · Apr 6.8/6.8 · Jul 3.8/3.9 · Oct 4.8/5.2.
Weather is seeded by *location*, so neighbouring simulated homes share the
same sky; any given day is reproducible regardless of the run's start date.

## How weather affects solar

```text
plane-of-array irradiance (POA)  ← sun position (NOAA) + GHI/DNI/DHI,
                                    tilt ≈ |lat| facing the equator,
                                    Liu-Jordan diffuse, ground albedo, IAM
cell temperature                 ← SAPM model (POA, ambient, wind)
dc_kw = kWp × POA/1000
      × solar_efficiency          # DC derate 0.92 (wiring, mismatch, LID)
      × shading_factor            # 0.97
      × temperature_factor        # −0.37 %/°C above 25 °C cell
      × (1 − soiling)             # builds daily, washed by rain, periodic cleaning
      × panel ageing              # 0.5 %/year
ac_kw = inverter(dc_kw)           # part-load efficiency curve, sleep at dawn,
                                  # clipping at inverter_capacity_kw
```

Rules:

- Power is **zero at night**; the inverter wakes and sleeps at low light.
- Measured Open-Meteo GHI already includes clouds and rain — it is not derated again.
- On partly cloudy skies, deterministic passing-cloud shadows cut the beam for
  a minute or two (`cloud_shadow`), giving the fast ramps real inverters log.
- `sensor_failure`: the irradiance feed drops out, but the panels keep
  producing — power is estimated from clear sky and cloud cover
  (`irradiance_source="estimated"`, `data_quality="degraded"`).
- Typical Pune result: ~1,430 kWh/kWp/year; 5.0 kWh/kWp/day in April,
  2.5–3.3 in the monsoon, peak ≈ 75–80 % of kWp on hot days.

## Energy flow order

1. Solar → home  
2. Solar surplus → battery (CC/CV taper above 90 % SOC, thermal derate)  
3. Remaining surplus → grid (clipped by zero-export and by volt-watt when the feeder voltage is high)  
4. Battery → home, down to a floor set by the goal  
5. Grid → home  
6. Grid → battery, only when the goal asks for it and the battery is not discharging  
7. Unserved load if the grid is down and the battery cannot cover the rest

Goals (`PRIMARY_GOAL`, from onboarding):

| Goal | Battery behaviour |
|---|---|
| `maximize_self_consumption` | Discharge to the minimum SOC; never grid-charge |
| `preserve_backup` | Keep `BATTERY_BACKUP_RESERVE_PERCENT` while the grid is up; grid-charge back to it if allowed |
| `minimize_bill` | With a ToU tariff, hold the battery during off-peak and grid-charge to `GRID_CHARGE_TARGET_SOC_PERCENT`; spend it at peak |

Outages: a grid-tied inverter **without a working battery shuts down**
(anti-islanding), so solar is 0 and the whole load is unserved. With a
battery, the backup port is limited to `INVERTER_CAPACITY_KW`; when demand
exceeds what solar + battery can supply, the largest non-critical appliances
are shed (`operating_mode="load_shed"`).

Grid realism: feeder voltage sags in the evening peak and rises at midday;
frequency drifts around 50 Hz; random feeder faults (Poisson per day, more in
the monsoon and summer evenings, log-normal duration) are seeded by location
so neighbours lose power together. `grid_status` is `available`, `outage`, or
`off_grid` for homes that are not grid-connected.

Battery charge and discharge are **separate non-negative fields**. Do not infer direction from the sign of a single power value.

Each reading is checked on **interval power (kW only)**:

```text
solar + grid_import + battery_discharge
    ≈ home_consumption + grid_export + battery_charge
```

kWh fields are derived afterwards as `power_kw × interval_seconds / 3600`. Import and export are never both positive in the same interval. SOC uses `battery_usable_capacity_kwh` (capped by nameplate).

A failed check sets `energy_balance_status` to `"warning"` and fills `warnings`. Generation never stops.

## JSON output

Stdout prints one JSON object per interval. The same object is appended as one line to `simulator/data/telemetry.jsonl`.

Core fields (plus extra keys such as `schema_version`, `grid_voltage_v`, `warnings` — `TelemetryRecord` allows new keys without breaking older readers):

```json
{
  "timestamp": "2026-08-23T12:30:00+05:30",
  "household_id": "home_001",
  "data_source": "simulator",
  "data_quality": "simulated",
  "solar_power_kw": 4.12,
  "home_load_power_kw": 1.68,
  "battery_soc_percent": 77.4,
  "battery_charge_power_kw": 2.20,
  "battery_discharge_power_kw": 0.0,
  "grid_import_power_kw": 0.0,
  "grid_export_power_kw": 0.24,
  "grid_status": "available",
  "energy_balance_status": "valid",
  "devices": [],
  "weather": {}
}
```

`devices` is a list of refrigerator, washing machine, water heater, AC, and water pump readings (each with `operating_mode`, `energy_today_kwh`, `runtime_today_minutes`). `weather` is the `WeatherRecord` used for that tick.

Extra fields added for analytics and the AI assistant (all optional for older readers):

| Group | Fields |
|---|---|
| Labels | `scenario`, `scenario_description`, `active_conditions[]` (ground truth such as `grid_down:feeder_fault`, `inverter_clipping`, `panels_soiled`, `cloud_shadow`, `peak_tariff`, `night`) |
| Events | `events[]` — `{type, severity, message, …}` on transitions: `grid_outage_started` / `grid_restored`, `battery_full`, `battery_reserve_reached`, `battery_fault`, `battery_derated`, `device_on` / `device_off` / `device_shed`, `inverter_clipping`, `export_curtailed`, `tariff_period_started`, `solar_production_started` / `ended`, `panels_cleaned`, `unserved_load`, `scenario_changed` |
| PV / inverter | `solar_potential_kw`, `solar_dc_power_kw`, `solar_clipped_kw`, `solar_curtailed_kw`, `poa_irradiance_wm2`, `pv_cell_temperature_c`, `pv_temperature_loss_percent`, `pv_soiling_loss_percent`, `pv_shading_loss_percent`, `performance_ratio`, `inverter_status`, `inverter_efficiency_percent`, `cloud_shadow`, `irradiance_source`, `solar_peak_today_kw` |
| Home | `home_load_served_kw`, `load_breakdown_kw{standby, lighting, fans, kitchen, entertainment, misc, tracked_devices}`, `occupancy_level`, `home_load_peak_today_kw`, `shed_device_ids` |
| Battery | `battery_temperature_c`, `battery_fault_code`, `battery_derate_reason`, `battery_cycle_count`, `battery_capacity_effective_kwh`, `battery_charge/discharge_interval_kwh` and `_today_kwh`, `battery_discharge_floor_percent`, `battery_present`, `grid_to_battery_kw` |
| Grid / money | `grid_voltage_v`, `grid_frequency_hz`, `grid_outage_cause`, `grid_outage_minutes_today`, `export_limit_kw`, `grid_import/export_today_kwh`, `tariff_type`, `tariff_period`, `tariff_rate_inr_per_kwh`, `export_credit_inr_per_kwh`, `grid_import_cost_interval_inr`, `grid_export_credit_interval_inr`, `grid_import_cost_today_inr`, `grid_export_credit_today_inr`, `net_energy_cost_today_inr` |
| Daily KPIs | `self_consumption_percent_today`, `self_sufficiency_percent_today`, `unserved_energy_today_kwh`, `solar_curtailed_today_kwh` |
| Context | `primary_goal`, `system{system_type, solar_capacity_kwp, inverter_capacity_kw, battery_capacity_kwh, panel_tilt_deg, panel_azimuth_deg, grid_connected, zero_export_mode}` |

A home without a battery reports `battery_soc_percent=0`, `battery_status="unavailable"` and `battery_present=false` instead of a fake 60 %.

`data/telemetry.jsonl` rotates at `OUTPUT_MAX_MB` (default 200 MB) keeping `OUTPUT_BACKUPS` old files.

## Inspect `telemetry.jsonl`

```bash
# Last reading, pretty
tail -n 1 simulator/data/telemetry.jsonl | python -m json.tool

# Count readings
wc -l simulator/data/telemetry.jsonl

# Solar and SOC only
python -c "
import json
from pathlib import Path
for line in Path('simulator/data/telemetry.jsonl').read_text().splitlines()[-5:]:
    row = json.loads(line)
    print(row['timestamp'], row['solar_power_kw'], row['battery_soc_percent'], row['grid_status'])
"
```

The `data/` directory is created automatically. It is gitignored.

## Scenarios

```bash
python -m simulator.main --scenario cloudy_day --weather-mode fallback --speed 0 --ticks 5 --pretty
```

The full list (with descriptions) lives in `simulator/scenarios.py`:

| Scenario | What it does |
|---|---|
| `normal_day` | Real weather, normal habits, rare random outages |
| `cloudy_day` / `rainy_day` | Weather overlay: thick cloud / steady rain (panels get washed) |
| `monsoon_storm` | Afternoon thunderstorm: gusts, intense rain, high outage risk |
| `heatwave` / `winter_cold` | Air +5.5 °C (heavy AC, hot cells) / −7 °C (long geyser runs) |
| `battery_low` / `battery_full` | Start just above the reserve / at 100 % |
| `battery_degraded` | SOH ~72 %, lower efficiency, runs warmer |
| `battery_overheat` | Sun-exposed enclosure: heats, derates, may trip fault code 2 |
| `grid_outage` | Grid down all day; anti-islanding / backup limits apply |
| `load_shedding` | Scheduled cuts 10:00–12:00 and 19:00–21:00 |
| `voltage_rise` | Midday feeder over-voltage; inverter curtails export (volt-watt) |
| `high_evening_load` | Evening base load ×1.85, AC forced on (warm season) |
| `guests_party` | More cooking, lighting and AC in the evening |
| `vacation` | House empty: fridge and standby only |
| `ev_heavy` | Large EV charging sessions every night |
| `dusty_panels` | ~18 % soiling loss |
| `partial_shading` | Tree/building shades the array early and late |
| `sensor_failure` | Irradiance feed drops out; PV estimated, `data_quality=degraded` |
| `auto` | Season-aware mix: a different realistic scenario each day (≈65 % normal days) |

Use `auto` for training / evaluation data: each tick carries the scenario name
and `active_conditions`, so models get labelled examples of outages, faults,
clipping, curtailment, soiling, heatwaves and unusual household days.

Grid events without a scenario:

```env
GRID_OUTAGE_WINDOW=14:00-16:00
LOAD_SHEDDING_WINDOWS=10:00-12:00,19:00-21:00
FORCE_GRID_OUTAGE=false
RANDOM_GRID_OUTAGES=true
```

## Change solar and battery size

Environment:

```env
SOLAR_CAPACITY_KWP=8.0
INVERTER_CAPACITY_KW=8.0
BATTERY_CAPACITY_KWH=15.0
BATTERY_USABLE_CAPACITY_KWH=12.0
```

Or CLI (usable capacity is set to 80% of nameplate when `--battery-capacity-kwh` is passed):

```bash
python -m simulator.main --solar-capacity-kwp 8 --battery-capacity-kwh 15 --weather-mode fallback
```

## Location

Keep **both** ways to set the home. They solve different jobs:

| Option | When to use |
|---|---|
| **1. City search** (Open-Meteo Geocoding) | Simulate a home in another city, CLI onboarding, or when GPS is denied / VPN / no browser |
| **2. Browser GPS** | Auto “use my location” on the live dashboard |

GPS alone is not enough: permission can be denied, `file://` has no geolocation, and you still want to point the simulator at Pune while you sit in Goa.

### City search (Open-Meteo Geocoding)

Place names are resolved with the [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api). No API key. The first match supplies `latitude`, `longitude`, `timezone`, and `elevation` for weather and solar.

```bash
# Resolve Pune, then stream live weather for that city
python -m simulator.main --dashboard --weather-mode live --location "Pune, India"

# Skip the geocoder and keep LATITUDE/LONGITUDE from .env
python -m simulator.main --dashboard --weather-mode live --no-geocode
```

On the dashboard, use **Search city** to change location while the simulator is running.

`.env`:

```env
LOCATION_NAME=Pimpri-Chinchwad
LOCATION_COUNTRY_CODE=IN
GEOCODE_ON_START=true
```

If geocoding fails, the configured lat/lon/timezone stay in use and `location.data_quality` is `"fallback"`.

### Browser GPS (auto location)

The dashboard asks the browser for GPS on load, and again when you click **My location**. Coordinates are posted to `POST /api/location`. Timezone and elevation come from Open-Meteo forecast (`timezone=auto`). The chip shows `GPS · lat, lon` because Open-Meteo has no reverse city name.

Works only over a secure context: [http://127.0.0.1:8765](http://127.0.0.1:8765) or HTTPS — not `file://`. If the prompt is denied, city search and the CLI `--location` still work.

Weather refreshes for the new coordinates in either case.

## Add or remove fields later

The simulator is built so telemetry, weather, and devices can change without a rewrite.

| What you want | Where to change |
|---|---|
| New / removed env setting | `SimulatorConfig` in `config.py` + `.env.example` |
| New Open-Meteo variable | `OPEN_METEO_HOURLY_FIELDS` in `config.py` + `WEATHER_FIELD_MAP` in `models.py` + `WeatherRecord` |
| New / removed appliance | `DEFAULT_DEVICE_CATALOG` in `config.py`, or `DEVICE_CATALOG_JSON` in `.env` |
| New telemetry key | Add it on `TelemetryRecord` (or just emit it — `extra="allow"`) and set it in `telemetry_generator.py` |

`TelemetryRecord` and `WeatherRecord` use `extra="allow"`, so older JSONL lines remain readable after you add keys.

## Environment variables

See `.env.example` for the full list. Important ones:

| Variable | Default |
|---|---|
| `HOUSEHOLD_ID` | `home_001` |
| `TIMEZONE` | `Asia/Kolkata` |
| `LATITUDE` / `LONGITUDE` | `18.6298` / `73.7997` |
| `SOLAR_CAPACITY_KWP` | `5.0` |
| `INVERTER_CAPACITY_KW` | `5.0` |
| `SOLAR_EFFICIENCY` | `0.92` (DC derate only) |
| `SHADING_FACTOR` | `0.97` |
| `PANEL_TILT_DEG` / `PANEL_AZIMUTH_DEG` | auto (≈ latitude, facing the equator) |
| `PANEL_CLEANING_INTERVAL_DAYS` | `15` |
| `HOUSEHOLD_OCCUPANTS` / `WORK_FROM_HOME` | `4` / `false` |
| `BATTERY_PRESENT` | `true` |
| `BATTERY_CAPACITY_KWH` | `10.0` |
| `INITIAL_BATTERY_SOC_PERCENT` | `60.0` |
| `BATTERY_MINIMUM_SOC_PERCENT` | `20.0` |
| `BATTERY_CAN_CHARGE_FROM_GRID` | `false` (onboarding: `true` for Hybrid) |
| `GRID_AVAILABLE` | `true` (`false` = off-grid home) |
| `ZERO_EXPORT_MODE` | `false` |
| `RANDOM_GRID_OUTAGES` / `GRID_OUTAGE_RATE_PER_DAY` | `true` / `0.08` |
| `PRIMARY_GOAL` | `maximize_self_consumption` |
| `TARIFF_TYPE` / `TARIFF_RATE_INR_PER_KWH` / `EXPORT_CREDIT_INR_PER_KWH` | `Flat rate` / `8.0` / `3.0` |
| `PERSIST_STATE` / `STATE_DIR` | `true` / `data/state` |
| `OUTPUT_MAX_MB` / `OUTPUT_BACKUPS` | `200` / `3` |
| `SIMULATION_INTERVAL_SECONDS` | `60` |
| `RANDOM_SEED` | `42` |
| `WEATHER_REFRESH_MINUTES` | `30` |
| `WEATHER_MODE` | `live` |
| `SCENARIO` | `normal_day` |
| `OUTPUT_FILE` | `data/telemetry.jsonl` |
| `RABBITMQ_URL` | empty (disabled). Local: `amqp://volta:volta@localhost:5672/volta` |
| `RABBITMQ_EXCHANGE` | `telemetry` |
| `RABBITMQ_ROUTING_KEY` | `telemetry.ingest` |
| `OPEN_METEO_BASE_URL` | `https://api.open-meteo.com/v1/forecast` |

## Tests

```bash
cd simulator
source .venv/bin/activate
pytest
```

47 tests. `tests/test_simulator.py` covers the contract: night-time solar = 0, cloud and rain derating, inverter clip, SOC bounds, grid outage / off-grid, zero-export, energy balance, Open-Meteo failure fallback, device contribution to load, seeded repeatability, onboarding and transport.

`tests/test_realism.py` covers the physics and behaviour: sunrise/sunset vs Open-Meteo, clear-sky and tilted-panel gain, day-to-day weather variety and order-independent determinism, monsoon vs winter, battery taper / no-battery reporting / overheating fault / SOH fade, anti-islanding, outage load shedding, preserve-backup reserve, ToU grid charging and peak pricing, volt-watt curtailment, outage events and labels, neighbour-shared random outages, the `auto` scenario mix, whole-day yield and load ranges, state persistence and JSONL rotation.

## Layout

```
simulator/
├── main.py                   # CLI (`python -m simulator.main`)
├── config.py                 # env + device catalog
├── models.py                 # WeatherRecord, TelemetryRecord, flows
├── scenarios.py              # scenario registry + `auto` daily mix
├── telemetry_generator.py    # one reading = all engines + events / labels / KPIs / state
├── engines/                  # sun, climate, randomness, solar, load, device, battery, grid, energy_balance
├── clients/                  # Open-Meteo, optional HTTP ingest, RabbitMQ publisher
├── dashboard/                # HTTP/SSE server, live_bus, location_state
├── tests/
├── requirements.txt
└── .env.example
```
