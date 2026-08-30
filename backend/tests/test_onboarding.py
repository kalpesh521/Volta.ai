"""Onboarding household_id assignment and status."""
from httpx import AsyncClient

from tests.onboarding_helpers import complete_hybrid_onboarding


async def _auth(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/auth/signup",
        json={"name": "Onboard Tester", "email": email, "password": "StrongPass123"},
    )
    login = await client.post(
        "/auth/login", json={"email": email, "password": "StrongPass123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_household_id_assigned_on_system_step(client: AsyncClient):
    headers = await _auth(client, "house-a@example.com")
    empty = await client.get("/onboarding/status", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["household_id"] is None

    household_id = await complete_hybrid_onboarding(client, headers)
    assert household_id.startswith("home_")
    assert len(household_id) == len("home_") + 12

    status = await client.get("/onboarding/status", headers=headers)
    assert status.json()["household_id"] == household_id

    summary = await client.get("/onboarding/summary", headers=headers)
    assert summary.json()["system"]["household_id"] == household_id


async def test_user_can_own_multiple_homes_and_switch_primary(client: AsyncClient):
    headers = await _auth(client, "two-homes@example.com")
    home_a = await complete_hybrid_onboarding(client, headers)
    home_b = await complete_hybrid_onboarding(client, headers, create_new=True)
    assert home_a != home_b

    listed = await client.get("/onboarding/homes", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["primary_household_id"] == home_a
    assert {row["household_id"] for row in body["homes"]} == {home_a, home_b}
    assert sum(1 for row in body["homes"] if row["is_primary"]) == 1

    switched = await client.post(f"/onboarding/homes/{home_b}/primary", headers=headers)
    assert switched.status_code == 200
    assert switched.json()["primary_household_id"] == home_b

    status_b = await client.get(
        "/onboarding/status", params={"household_id": home_b}, headers=headers
    )
    assert status_b.json()["is_primary"] is True
    status_a = await client.get(
        "/onboarding/status", params={"household_id": home_a}, headers=headers
    )
    assert status_a.json()["is_primary"] is False


async def test_household_ids_are_unique_per_user(client: AsyncClient):
    headers_a = await _auth(client, "house-a2@example.com")
    headers_b = await _auth(client, "house-b2@example.com")
    home_a = await complete_hybrid_onboarding(client, headers_a)
    home_b = await complete_hybrid_onboarding(client, headers_b)
    assert home_a != home_b
