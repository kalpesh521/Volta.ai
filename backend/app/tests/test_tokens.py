"""Refresh token rotation and logout revocation tests."""
from httpx import AsyncClient


async def _signup_and_login(client: AsyncClient, email: str) -> dict:
    payload = {"email": email, "password": "StrongPass123"}
    await client.post("/auth/signup", json=payload)
    response = await client.post("/auth/login", json=payload)
    return response.json()


async def test_refresh_rotates_token_and_invalidates_old_one(client: AsyncClient):
    tokens = await _signup_and_login(client, "grace@example.com")
    old_refresh_token = tokens["refresh_token"]

    refresh_response = await client.post(
        "/auth/refresh", json={"refresh_token": old_refresh_token}
    )
    assert refresh_response.status_code == 200
    new_tokens = refresh_response.json()
    assert new_tokens["refresh_token"] != old_refresh_token
    assert new_tokens["access_token"] != tokens["access_token"]

    # The old (now-rotated) refresh token must no longer work.
    reuse_response = await client.post(
        "/auth/refresh", json={"refresh_token": old_refresh_token}
    )
    assert reuse_response.status_code == 401
    assert reuse_response.json()["error"]["code"] == "invalid_token"


async def test_refresh_reuse_revokes_all_sessions(client: AsyncClient):
    tokens = await _signup_and_login(client, "heidi@example.com")
    old_refresh_token = tokens["refresh_token"]

    rotated = await client.post("/auth/refresh", json={"refresh_token": old_refresh_token})
    new_refresh_token = rotated.json()["refresh_token"]

    # Reusing the already-rotated token is treated as a compromise signal.
    await client.post("/auth/refresh", json={"refresh_token": old_refresh_token})

    # As a result, even the legitimately-rotated token should now be revoked too.
    response = await client.post("/auth/refresh", json={"refresh_token": new_refresh_token})
    assert response.status_code == 401


async def test_refresh_with_garbage_token_returns_401(client: AsyncClient):
    response = await client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient):
    tokens = await _signup_and_login(client, "ivan@example.com")
    refresh_token = tokens["refresh_token"]

    logout_response = await client.post("/auth/logout", json={"refresh_token": refresh_token})
    assert logout_response.status_code == 204

    reuse_response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse_response.status_code == 401


async def test_logout_is_idempotent(client: AsyncClient):
    tokens = await _signup_and_login(client, "judy@example.com")
    refresh_token = tokens["refresh_token"]

    first = await client.post("/auth/logout", json={"refresh_token": refresh_token})
    second = await client.post("/auth/logout", json={"refresh_token": refresh_token})
    assert first.status_code == 204
    assert second.status_code == 204
