"""Energy ingest + live / summary API tests."""
from datetime import datetime
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from app.core.config import settings

TZ = ZoneInfo("Asia/Kolkata")
INGEST_HEADERS = {"X-Ingest-Token": settings.INGEST_TOKEN}
HOUSEHOLD = "home_001"


def _tick(
    ts: datetime,
    *,
    household_id: str = HOUSEHOLD,
    solar_kw: float = 3.0,
    load_kw: float = 1.5,
    charge_kw: float = 1.5,
    discharge_kw: float = 0.0,
    import_kw: float = 0.0,
    export_kw: float = 0.0,
    soc: float = 62.0,
    grid_status: str = "available",
    battery_status: str = "charging",
) -> dict:
    interval_h = 1.0 / 60.0
    return {
        "timestamp": ts.isoformat(),
        "household_id": household_id,
        "data_source": "simulator",
        "data_quality": "simulated",
        "solar_power_kw": solar_kw,
        "solar_energy_interval_kwh": solar_kw * interval_h,
        "solar_energy_today_kwh": 4.2,
        "solar_energy_total_kwh": 120.0,
        "home_load_power_kw": load_kw,
        "home_consumption_interval_kwh": load_kw * interval_h,
        "home_consumption_today_kwh": 3.1,
        "home_consumption_total_kwh": 90.0,
        "battery_soc_percent": soc,
        "battery_soh_percent": 98.0,
        "battery_charge_power_kw": charge_kw,
        "battery_discharge_power_kw": discharge_kw,
        "battery_energy_available_kwh": 5.0,
        "battery_status": battery_status,
        "grid_status": grid_status,
        "grid_import_power_kw": import_kw,
        "grid_export_power_kw": export_kw,
        "total_import_kwh": 10.0,
        "total_export_kwh": 8.0,
        "solar_to_home_kw": min(solar_kw, load_kw),
        "solar_to_battery_kw": charge_kw,
        "solar_to_grid_kw": export_kw,
        "battery_to_home_kw": discharge_kw,
        "grid_to_home_kw": import_kw,
        "unserved_load_kw": 0.0,
        "energy_balance_status": "valid",
        "energy_balance_error_kw": 0.0,
        "energy_balance_valid": True,
        "devices": [
            {
                "device_id": "dev_refrigerator",
                "device_name": "Refrigerator",
                "device_type": "refrigerator",
                "rated_power_kw": 0.15,
                "current_state": "on",
                "current_power_kw": 0.12,
                "energy_interval_kwh": 0.002,
                "critical": True,
                "controllable": False,
            }
        ],
        "weather": {
            "timestamp": ts.isoformat(),
            "temperature_c": 29.0,
            "humidity_percent": 55.0,
            "cloud_cover_percent": 20.0,
            "precipitation_mm": 0.0,
            "precipitation_probability_percent": 10.0,
            "wind_speed_kmh": 8.0,
            "shortwave_radiation_wm2": 700.0,
            "direct_radiation_wm2": 500.0,
            "diffuse_radiation_wm2": 200.0,
            "weather_code": 0,
            "source": "test",
            "data_quality": "simulated",
        },
        "scenario": "normal_day",
        "grid_voltage_v": 230.4,
        "grid_frequency_hz": 50.01,
        "battery_temperature_c": 31.2,
        "warnings": [],
    }


async def _auth_headers(client: AsyncClient, email: str = "energy@example.com") -> dict[str, str]:
    await client.post(
        "/auth/signup",
        json={"name": "Energy Tester", "email": email, "password": "StrongPass123"},
    )
    login = await client.post(
        "/auth/login", json={"email": email, "password": "StrongPass123"}
    )
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_ingest_requires_token(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    response = await client.post("/energy/ingest", json=_tick(ts))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_ingest_token"


async def test_ingest_rejects_wrong_token(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    response = await client.post(
        "/energy/ingest",
        json=_tick(ts),
        headers={"X-Ingest-Token": "not-the-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_ingest_token"


async def test_ingest_rejects_negative_power(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    payload = _tick(ts)
    payload["solar_power_kw"] = -1.0
    response = await client.post("/energy/ingest", json=payload, headers=INGEST_HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_ingest_rejects_simultaneous_import_and_export(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    payload = _tick(ts, import_kw=1.0, export_kw=0.5, charge_kw=0.5)
    response = await client.post("/energy/ingest", json=payload, headers=INGEST_HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_ingest_keeps_unbalanced_tick_as_warning(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    payload = _tick(ts, solar_kw=5.0, load_kw=1.0, charge_kw=0.0)
    response = await client.post("/energy/ingest", json=payload, headers=INGEST_HEADERS)
    assert response.status_code == 202
    body = response.json()
    assert body["energy_balance_valid"] is False
    assert body["energy_balance_status"] == "warning"
    assert body["warnings"]


async def test_live_requires_jwt(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    await client.post("/energy/ingest", json=_tick(ts), headers=INGEST_HEADERS)
    response = await client.get(f"/energy/{HOUSEHOLD}/live")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_live_404_when_household_has_no_data(client: AsyncClient):
    headers = await _auth_headers(client)
    response = await client.get("/energy/home_unknown/live", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "energy_not_found"


async def test_live_and_slice_endpoints_after_ingest(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 30, tzinfo=TZ)
    ingested = await client.post("/energy/ingest", json=_tick(ts), headers=INGEST_HEADERS)
    assert ingested.status_code == 202
    assert ingested.json()["battery_charge_interval_kwh"] > 0
    assert ingested.json()["energy_balance_valid"] is True

    headers = await _auth_headers(client)
    live = await client.get(f"/energy/{HOUSEHOLD}/live", headers=headers)
    assert live.status_code == 200
    body = live.json()
    assert body["solar"]["power_kw"] == 3.0
    assert body["load"]["power_kw"] == 1.5
    assert body["battery"]["status"] == "charging"
    assert body["grid"]["voltage_v"] == 230.4
    assert body["devices"][0]["device_name"] == "Refrigerator"
    assert body["weather"]["temperature_c"] == 29.0
    assert body["scenario"] == "normal_day"

    battery = await client.get(f"/energy/{HOUSEHOLD}/battery", headers=headers)
    assert battery.status_code == 200
    assert battery.json()["soc_percent"] == 62.0

    grid = await client.get(f"/energy/{HOUSEHOLD}/grid", headers=headers)
    assert grid.status_code == 200
    assert grid.json()["status"] == "available"

    devices = await client.get(f"/energy/{HOUSEHOLD}/devices", headers=headers)
    assert devices.status_code == 200
    assert len(devices.json()["devices"]) == 1

    weather = await client.get(f"/energy/{HOUSEHOLD}/weather", headers=headers)
    assert weather.status_code == 200
    assert weather.json()["weather"]["humidity_percent"] == 55.0


async def test_older_tick_does_not_replace_latest(client: AsyncClient):
    newer = datetime(2026, 8, 29, 14, 0, tzinfo=TZ)
    older = datetime(2026, 8, 29, 10, 0, tzinfo=TZ)
    await client.post(
        "/energy/ingest", json=_tick(newer, soc=80.0), headers=INGEST_HEADERS
    )
    await client.post(
        "/energy/ingest", json=_tick(older, soc=40.0), headers=INGEST_HEADERS
    )

    headers = await _auth_headers(client)
    live = await client.get(f"/energy/{HOUSEHOLD}/live", headers=headers)
    assert live.json()["battery"]["soc_percent"] == 80.0
    history = await client.get(f"/energy/{HOUSEHOLD}/history", headers=headers)
    assert history.json()["count"] == 2


async def test_daily_and_hourly_summaries(client: AsyncClient):
    noon = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    evening = datetime(2026, 8, 29, 18, 0, tzinfo=TZ)
    other_day = datetime(2026, 8, 28, 12, 0, tzinfo=TZ)

    await client.post(
        "/energy/ingest",
        json=_tick(noon, solar_kw=3.0, load_kw=1.5, charge_kw=1.5),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(evening, solar_kw=0.6, load_kw=2.0, charge_kw=0.0, discharge_kw=1.4, battery_status="discharging"),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(other_day, solar_kw=4.0, load_kw=1.0, charge_kw=3.0),
        headers=INGEST_HEADERS,
    )

    headers = await _auth_headers(client)
    daily = await client.get(
        f"/energy/{HOUSEHOLD}/daily",
        params={"date": "2026-08-29"},
        headers=headers,
    )
    assert daily.status_code == 200
    body = daily.json()
    assert body["reading_count"] == 2
    assert body["date"] == "2026-08-29"
    expected_solar = (3.0 + 0.6) / 60.0
    assert abs(body["solar_generation_kwh"] - expected_solar) < 1e-6
    assert body["battery_charge_kwh"] > 0
    assert body["battery_discharge_kwh"] > 0

    empty = await client.get(
        f"/energy/{HOUSEHOLD}/daily",
        params={"date": "2026-08-01"},
        headers=headers,
    )
    assert empty.status_code == 200
    assert empty.json()["reading_count"] == 0
    assert empty.json()["solar_generation_kwh"] == 0.0

    hourly = await client.get(
        f"/energy/{HOUSEHOLD}/hourly",
        params={"date": "2026-08-29"},
        headers=headers,
    )
    assert hourly.status_code == 200
    buckets = hourly.json()["buckets"]
    assert len(buckets) == 24
    noon_bucket = buckets[12]
    evening_bucket = buckets[18]
    assert noon_bucket["reading_count"] == 1
    assert evening_bucket["reading_count"] == 1
    assert buckets[0]["reading_count"] == 0


async def test_households_are_isolated(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id="home_001", soc=55.0),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id="home_002", soc=90.0),
        headers=INGEST_HEADERS,
    )
    headers = await _auth_headers(client)
    one = await client.get("/energy/home_001/battery", headers=headers)
    two = await client.get("/energy/home_002/battery", headers=headers)
    assert one.json()["soc_percent"] == 55.0
    assert two.json()["soc_percent"] == 90.0
