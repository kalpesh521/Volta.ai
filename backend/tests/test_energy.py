"""Energy ingest + live / summary API tests."""
from datetime import datetime
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from app.core.config import settings
from tests.onboarding_helpers import HYBRID_SYSTEM, complete_hybrid_onboarding

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
        "location": {
            "name": "Pune",
            "admin1": "Maharashtra",
            "country": "India",
            "latitude": 18.5203,
            "longitude": 73.8567,
            "timezone": "Asia/Kolkata",
            "query": "Pune, India",
            "source": "open-meteo-geocoding",
        },
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


async def test_live_403_until_onboarding_complete(client: AsyncClient):
    headers = await _auth_headers(client)
    created = await client.put(
        "/onboarding/system",
        json={
            "panel_type": "Monocrystalline",
            "panel_qty": 8,
            "system_type": "On-grid",
            "inverter_brand": "Growatt",
            "inverter_capacity_kw": 5,
        },
        headers=headers,
    )
    assert created.status_code == 200
    household_id = created.json()["household_id"]
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=household_id),
        headers=INGEST_HEADERS,
    )
    response = await client.get(f"/energy/{household_id}/live", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    me = await client.get("/energy/me/live", headers=headers)
    assert me.status_code == 403


async def test_live_and_slice_endpoints_after_ingest(client: AsyncClient):
    headers = await _auth_headers(client)
    household_id = await complete_hybrid_onboarding(client, headers)
    ts = datetime(2026, 8, 29, 12, 30, tzinfo=TZ)
    ingested = await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=household_id),
        headers=INGEST_HEADERS,
    )
    assert ingested.status_code == 202
    assert ingested.json()["battery_charge_interval_kwh"] > 0
    assert ingested.json()["energy_balance_valid"] is True

    live = await client.get(f"/energy/{household_id}/live", headers=headers)
    assert live.status_code == 200
    body = live.json()
    assert body["household_id"] == household_id
    assert body["solar"]["power_kw"] == 3.0
    assert body["load"]["power_kw"] == 1.5
    assert body["battery"]["status"] == "charging"
    assert body["grid"]["voltage_v"] == 230.4
    assert body["devices"][0]["device_name"] == "Refrigerator"
    assert body["weather"]["temperature_c"] == 29.0
    assert body["scenario"] == "normal_day"
    assert body["location"]["name"] == "Pune"
    assert "Maharashtra" in (body["location"]["label"] or "")

    me_live = await client.get("/energy/me/live", headers=headers)
    assert me_live.status_code == 200
    assert me_live.json()["household_id"] == household_id

    battery = await client.get(f"/energy/{household_id}/battery", headers=headers)
    assert battery.status_code == 200
    assert battery.json()["soc_percent"] == 62.0

    grid = await client.get(f"/energy/{household_id}/grid", headers=headers)
    assert grid.status_code == 200
    assert grid.json()["status"] == "available"

    devices = await client.get(f"/energy/{household_id}/devices", headers=headers)
    assert devices.status_code == 200
    assert len(devices.json()["devices"]) == 1

    weather = await client.get(f"/energy/{household_id}/weather", headers=headers)
    assert weather.status_code == 200
    assert weather.json()["weather"]["humidity_percent"] == 55.0


async def test_older_tick_does_not_replace_latest(client: AsyncClient):
    headers = await _auth_headers(client)
    household_id = await complete_hybrid_onboarding(client, headers)
    newer = datetime(2026, 8, 29, 14, 0, tzinfo=TZ)
    older = datetime(2026, 8, 29, 10, 0, tzinfo=TZ)
    await client.post(
        "/energy/ingest",
        json=_tick(newer, household_id=household_id, soc=80.0),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(older, household_id=household_id, soc=40.0),
        headers=INGEST_HEADERS,
    )

    live = await client.get(f"/energy/{household_id}/live", headers=headers)
    assert live.json()["battery"]["soc_percent"] == 80.0
    history = await client.get(f"/energy/{household_id}/history", headers=headers)
    assert history.json()["count"] == 2


async def test_daily_and_hourly_summaries(client: AsyncClient):
    headers = await _auth_headers(client)
    household_id = await complete_hybrid_onboarding(client, headers)
    noon = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    evening = datetime(2026, 8, 29, 18, 0, tzinfo=TZ)
    other_day = datetime(2026, 8, 28, 12, 0, tzinfo=TZ)

    await client.post(
        "/energy/ingest",
        json=_tick(noon, household_id=household_id, solar_kw=3.0, load_kw=1.5, charge_kw=1.5),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(
            evening,
            household_id=household_id,
            solar_kw=0.6,
            load_kw=2.0,
            charge_kw=0.0,
            discharge_kw=1.4,
            battery_status="discharging",
        ),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(other_day, household_id=household_id, solar_kw=4.0, load_kw=1.0, charge_kw=3.0),
        headers=INGEST_HEADERS,
    )

    daily = await client.get(
        f"/energy/{household_id}/daily",
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

    me_daily = await client.get(
        "/energy/me/daily",
        params={"date": "2026-08-29"},
        headers=headers,
    )
    assert me_daily.status_code == 200
    assert me_daily.json()["household_id"] == household_id
    assert me_daily.json()["reading_count"] == 2

    empty = await client.get(
        f"/energy/{household_id}/daily",
        params={"date": "2026-08-01"},
        headers=headers,
    )
    assert empty.status_code == 200
    assert empty.json()["reading_count"] == 0
    assert empty.json()["solar_generation_kwh"] == 0.0

    hourly = await client.get(
        f"/energy/{household_id}/hourly",
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


async def test_households_are_isolated_per_user(client: AsyncClient):
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    headers_a = await _auth_headers(client, email="owner-a@example.com")
    headers_b = await _auth_headers(client, email="owner-b@example.com")
    home_a = await complete_hybrid_onboarding(client, headers_a)
    home_b = await complete_hybrid_onboarding(client, headers_b)

    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=home_a, soc=55.0),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=home_b, soc=90.0),
        headers=INGEST_HEADERS,
    )

    own = await client.get(f"/energy/{home_a}/battery", headers=headers_a)
    assert own.status_code == 200
    assert own.json()["soc_percent"] == 55.0

    leaked = await client.get(f"/energy/{home_b}/battery", headers=headers_a)
    assert leaked.status_code == 404

    other = await client.get(f"/energy/{home_b}/battery", headers=headers_b)
    assert other.status_code == 200
    assert other.json()["soc_percent"] == 90.0


async def test_simulator_profile_from_onboarding(client: AsyncClient):
    headers = await _auth_headers(client)
    household_id = await complete_hybrid_onboarding(client, headers)

    owner = await client.get(f"/energy/{household_id}/profile", headers=headers)
    assert owner.status_code == 200
    body = owner.json()
    assert body["household_id"] == household_id
    assert body["solar_capacity_kwp"] == 4.0  # 10 × 400 Wp
    assert body["inverter_capacity_kw"] == 5.0
    assert body["battery_present"] is True
    assert body["battery_capacity_kwh"] == 10.0
    assert body["battery_usable_capacity_kwh"] == 8.0
    assert body["battery_minimum_soc_percent"] == 20.0
    assert body["grid_available"] is True
    assert body["zero_export_mode"] is False
    keys = {d["device_type"] for d in body["devices"]}
    assert keys == {"refrigerator", "air_conditioner"}
    fridge = next(d for d in body["devices"] if d["device_type"] == "refrigerator")
    assert fridge["critical"] is True
    assert body["location"] == "Pune, India"

    ingest = await client.get(
        f"/energy/{household_id}/profile",
        headers=INGEST_HEADERS,
    )
    assert ingest.status_code == 200
    assert ingest.json()["household_id"] == household_id

    me = await client.get("/energy/me/profile", headers=headers)
    assert me.status_code == 200
    assert me.json()["household_id"] == household_id

    stranger = await _auth_headers(client, email="stranger@example.com")
    denied = await client.get(f"/energy/{household_id}/profile", headers=stranger)
    assert denied.status_code == 404


async def test_token_routes_use_primary_home_and_support_second_home(client: AsyncClient):
    headers = await _auth_headers(client, email="multi-home@example.com")
    home_a = await complete_hybrid_onboarding(client, headers)
    home_b = await complete_hybrid_onboarding(client, headers, create_new=True)
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)

    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=home_a, soc=40.0),
        headers=INGEST_HEADERS,
    )
    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=home_b, soc=88.0),
        headers=INGEST_HEADERS,
    )

    homes = await client.get("/energy/me/homes", headers=headers)
    assert homes.status_code == 200
    assert homes.json()["primary_household_id"] == home_a

    primary_live = await client.get("/energy/me/live", headers=headers)
    assert primary_live.status_code == 200
    assert primary_live.json()["household_id"] == home_a
    assert primary_live.json()["battery"]["soc_percent"] == 40.0

    other_live = await client.get(
        "/energy/me/live", params={"household_id": home_b}, headers=headers
    )
    assert other_live.status_code == 200
    assert other_live.json()["household_id"] == home_b
    assert other_live.json()["battery"]["soc_percent"] == 88.0

    await client.post(f"/onboarding/homes/{home_b}/primary", headers=headers)
    switched = await client.get("/energy/me/live", headers=headers)
    assert switched.json()["household_id"] == home_b
    assert switched.json()["battery"]["soc_percent"] == 88.0

    catalog = await client.get("/energy/onboarding-profiles", headers=INGEST_HEADERS)
    assert catalog.status_code == 200
    ids = {row["household_id"] for row in catalog.json()["profiles"]}
    assert home_a in ids
    assert home_b in ids


async def test_me_live_does_not_fall_back_to_previous_home(client: AsyncClient):
    headers = await _auth_headers(client, email="primary-switch@example.com")
    home_a = await complete_hybrid_onboarding(client, headers)
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    await client.post(
        "/energy/ingest",
        json=_tick(ts, household_id=home_a, soc=40.0),
        headers=INGEST_HEADERS,
    )
    created = await client.post("/onboarding/homes", json=HYBRID_SYSTEM, headers=headers)
    assert created.status_code == 201
    home_b = created.json()["household_id"]
    switched = await client.post(f"/onboarding/homes/{home_b}/primary", headers=headers)
    assert switched.status_code == 200
    assert switched.json()["primary_household_id"] == home_b
    me = await client.get("/energy/me/live", headers=headers)
    assert me.status_code == 403
    assert me.json()["error"]["code"] == "forbidden"


async def test_live_replaces_gps_coords_with_onboarding_city(client: AsyncClient):
    headers = await _auth_headers(client, email="gps-place@example.com")
    household_id = await complete_hybrid_onboarding(client, headers)
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    tick = _tick(ts, household_id=household_id)
    tick["location"] = {
        "name": "Current location",
        "source": "browser-gps",
        "latitude": 18.64,
        "longitude": 73.76,
        "timezone": "Asia/Kolkata",
    }
    ingested = await client.post("/energy/ingest", json=tick, headers=INGEST_HEADERS)
    assert ingested.status_code == 202
    live = await client.get("/energy/me/live", headers=headers)
    assert live.status_code == 200
    assert live.json()["location"]["name"] == "Pune, India"
