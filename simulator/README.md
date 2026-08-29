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
```

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
  &hourly=temperature_2m,relative_humidity_2m,precipitation,
          precipitation_probability,cloud_cover,wind_speed_10m,
          shortwave_radiation,direct_radiation,diffuse_radiation,
          weather_code
  &daily=sunrise,sunset
  &wind_speed_unit=kmh
```

Hourly keys live in `OPEN_METEO_HOURLY_FIELDS` in `config.py`. Daily keys live in `OPEN_METEO_DAILY_FIELDS`. Mapping from API names to `WeatherRecord` fields is `WEATHER_FIELD_MAP` in `models.py`.

### Weather modes

| Mode | Behaviour |
|---|---|
| `live` | HTTP GET to Open-Meteo. On failure, reuse the last cache; if there is no cache, use the deterministic fallback profile and set `data_quality` to `"fallback"`. |
| `fallback` | Never calls the network. Uses a seeded local-climate profile. |
| `historical-style` | Never calls the network. Same generator as fallback, tagged as a typical-year style series for agent/dashboard work. |

## How weather affects solar

```text
solar_power_kw =
    solar_capacity_kwp
  × irradiance_factor          # shortwave_radiation_wm2 / 1000, clipped to 0–1.2
  × solar_efficiency           # default 0.82
  × shading_factor             # default 0.95
  × temperature_factor         # derate above 25 °C
  × cloud_factor / rain_factor # only when GHI is synthesized (clear-sky sine)
  × small seeded noise
```

Rules:

- Power is **zero at night** (before sunrise / after sunset).
- Measured Open-Meteo GHI already includes clouds and rain — it is not derated again.
- If `shortwave_radiation` is missing or zero during daylight, a sine daylight curve is used, then cloud/rain factors apply.
- `sensor_failure` (degraded + GHI 0) stays at 0 — no sine fallback.
- Inverter clipping: power never exceeds `inverter_capacity_kw`.

## Energy flow order

1. Solar → home  
2. Solar surplus → battery  
3. Remaining surplus → grid (clipped to 0 when `zero_export_mode` is true)  
4. Battery → home  
5. Grid → home  
6. Unserved load if the grid is down and the battery is at minimum SOC  

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

`devices` is a list of refrigerator, washing machine, water heater, AC, and water pump readings. `weather` is the `WeatherRecord` used for that tick.

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

| Scenario | What it does |
|---|---|
| `normal_day` | Default household + weather |
| `cloudy_day` | High cloud cover, reduced GHI |
| `rainy_day` | Rain + heavy cloud, lower PV |
| `battery_low` | Starts just above minimum SOC |
| `battery_full` | Starts at 100% SOC |
| `grid_outage` | `grid_available=false`, import is 0, unserved load possible |
| `high_evening_load` | Stronger evening base load, AC forced on 17:00–23:00 |
| `sensor_failure` | Radiation sensors drop out (`data_quality=degraded`) |

Optional scheduled outage without the full `grid_outage` scenario:

```env
GRID_OUTAGE_WINDOW=14:00-16:00
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
| `SOLAR_EFFICIENCY` | `0.82` |
| `SHADING_FACTOR` | `0.95` |
| `BATTERY_PRESENT` | `true` |
| `BATTERY_CAPACITY_KWH` | `10.0` |
| `INITIAL_BATTERY_SOC_PERCENT` | `60.0` |
| `BATTERY_MINIMUM_SOC_PERCENT` | `20.0` |
| `GRID_AVAILABLE` | `true` |
| `ZERO_EXPORT_MODE` | `false` |
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

Coverage includes: night-time solar = 0, cloud and rain derating, inverter clip, SOC bounds, grid outage, zero-export, energy balance, Open-Meteo failure fallback, device contribution to load, and seeded repeatability.

## Layout

```
simulator/
├── main.py                   # CLI (`python -m simulator.main`)
├── config.py                 # env + device catalog
├── models.py                 # WeatherRecord, TelemetryRecord, flows
├── telemetry_generator.py    # one reading = all engines
├── engines/                  # solar, load, battery, grid, device, energy_balance
├── clients/                  # Open-Meteo, optional HTTP ingest, RabbitMQ publisher
├── dashboard/                # HTTP/SSE server, live_bus, location_state
├── tests/
├── requirements.txt
└── .env.example
```
