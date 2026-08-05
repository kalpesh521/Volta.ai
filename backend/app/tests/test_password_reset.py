"""Password reset request/confirm flow tests."""
from httpx import AsyncClient

from app.core.security import generate_opaque_token, hash_token
from app.repositories.password_reset_repository import PasswordResetRepository
from app.repositories.user_repository import UserRepository


async def test_password_reset_request_always_returns_204(client: AsyncClient):
    """Same response whether or not the email exists - no user enumeration."""
    payload = {"email": "known@example.com", "password": "StrongPass123"}
    await client.post("/auth/signup", json=payload)

    known_response = await client.post(
        "/auth/password-reset/request", json={"email": "known@example.com"}
    )
    unknown_response = await client.post(
        "/auth/password-reset/request", json={"email": "unknown@example.com"}
    )

    assert known_response.status_code == 204
    assert unknown_response.status_code == 204


async def test_password_reset_confirm_with_invalid_token_fails(client: AsyncClient):
    response = await client.post(
        "/auth/password-reset/confirm",
        json={"token": "not-a-real-token", "new_password": "NewStrongPass123"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_password_reset_confirm_allows_login_with_new_password(
    client: AsyncClient, db_session
):
    payload = {"email": "kelly@example.com", "password": "OldPassword123"}
    await client.post("/auth/signup", json=payload)

    # Simulate the token the "emailed" link would contain, using the same
    # repository the request-reset endpoint uses under the hood.
    raw_token = generate_opaque_token()
    from datetime import datetime, timedelta, timezone

    user = await UserRepository(db_session).get_by_email("kelly@example.com")
    reset_repo = PasswordResetRepository(db_session)
    await reset_repo.create(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    await db_session.commit()

    confirm_response = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "NewStrongPass123"},
    )
    assert confirm_response.status_code == 204

    old_login = await client.post("/auth/login", json=payload)
    assert old_login.status_code == 401

    new_login = await client.post(
        "/auth/login", json={"email": "kelly@example.com", "password": "NewStrongPass123"}
    )
    assert new_login.status_code == 200


async def test_password_reset_token_is_single_use(client: AsyncClient, db_session):
    payload = {"email": "liam@example.com", "password": "OldPassword123"}
    await client.post("/auth/signup", json=payload)

    from datetime import datetime, timedelta, timezone

    raw_token = generate_opaque_token()
    user = await UserRepository(db_session).get_by_email("liam@example.com")
    reset_repo = PasswordResetRepository(db_session)
    await reset_repo.create(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    await db_session.commit()

    first = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "NewStrongPass123"},
    )
    second = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "AnotherPass456"},
    )
    assert first.status_code == 204
    assert second.status_code == 401
