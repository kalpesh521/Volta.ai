"""Shared onboarding payloads for energy / household tests."""
from httpx import AsyncClient

HYBRID_SYSTEM = {
    "panel_type": "Monocrystalline",
    "panel_qty": 10,
    "system_type": "Hybrid",
    "location": "Pune, India",
    "avg_monthly_bill": 3500,
    "inverter_brand": "Growatt",
    "inverter_capacity_kw": 5,
}


async def complete_hybrid_onboarding(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    appliances: list[dict] | None = None,
    household_id: str | None = None,
    create_new: bool = False,
) -> str:
    params = {"household_id": household_id} if household_id else None
    if create_new:
        created = await client.post("/onboarding/homes", json=HYBRID_SYSTEM, headers=headers)
        assert created.status_code == 201, created.text
    else:
        created = await client.put(
            "/onboarding/system", json=HYBRID_SYSTEM, headers=headers, params=params
        )
        assert created.status_code == 200, created.text
    hid = created.json()["household_id"]
    assert hid.startswith("home_")
    step_params = {"household_id": hid} if create_new or household_id else None

    battery = await client.put(
        "/onboarding/battery",
        json={"battery_capacity_kwh": 10, "backup_hours": 4, "reserve_pct": 20},
        headers=headers,
        params=step_params,
    )
    assert battery.status_code == 200, battery.text

    grid = await client.put(
        "/onboarding/grid",
        json={
            "meter_type": "Net metering",
            "sanctioned_load_kw": 5,
            "tariff_type": "Flat rate",
            "discom": "MSEDCL",
        },
        headers=headers,
        params=step_params,
    )
    assert grid.status_code == 200, grid.text

    selected = appliances or [
        {"appliance_key": "fridge", "is_critical": True},
        {"appliance_key": "ac", "is_critical": False},
    ]
    apps = await client.put(
        "/onboarding/appliances",
        json={"appliances": selected},
        headers=headers,
        params=step_params,
    )
    assert apps.status_code == 200, apps.text

    done = await client.post("/onboarding/complete", headers=headers, params=step_params)
    assert done.status_code == 200, done.text
    assert done.json()["is_complete"] is True
    assert done.json()["household_id"] == hid
    return hid
