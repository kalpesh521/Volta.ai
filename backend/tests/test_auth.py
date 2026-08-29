"""Signup, login, /me, and password-error-path tests."""
from httpx import AsyncClient


async def test_signup_success(client: AsyncClient):
    response = await client.post(
        "/auth/signup",
        json={"name": "Alice", "email": "alice@example.com", "password": "StrongPass123"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Alice"
    assert body["email"] == "alice@example.com"
    assert "id" in body
    assert "password" not in body
    assert "hashed_password" not in body


async def test_signup_requires_name(client: AsyncClient):
    response = await client.post(
        "/auth/signup", json={"email": "noname@example.com", "password": "StrongPass123"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_signup_duplicate_email_returns_409(client: AsyncClient):
    payload = {"name": "Bob", "email": "bob@example.com", "password": "StrongPass123"}
    first = await client.post("/auth/signup", json=payload)
    assert first.status_code == 201

    second = await client.post("/auth/signup", json=payload)
    assert second.status_code == 409
    body = second.json()
    assert body["success"] is False
    assert body["error"]["code"] == "email_already_registered"


async def test_login_success_returns_token_pair(client: AsyncClient):
    await client.post(
        "/auth/signup",
        json={"name": "Carol", "email": "carol@example.com", "password": "StrongPass123"},
    )

    response = await client.post(
        "/auth/login", json={"email": "carol@example.com", "password": "StrongPass123"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


async def test_login_wrong_password_returns_generic_401(client: AsyncClient):
    await client.post(
        "/auth/signup",
        json={"name": "Dave", "email": "dave@example.com", "password": "StrongPass123"},
    )

    response = await client.post(
        "/auth/login", json={"email": "dave@example.com", "password": "wrong-password"}
    )
    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "invalid_credentials"


async def test_login_nonexistent_email_returns_same_generic_401(client: AsyncClient):
    response = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "whatever123"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_me_requires_valid_access_token(client: AsyncClient):
    response = await client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_me_rejects_garbage_token(client: AsyncClient):
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


async def test_me_returns_current_user_with_valid_token(client: AsyncClient):
    await client.post(
        "/auth/signup",
        json={"name": "Erin", "email": "erin@example.com", "password": "StrongPass123"},
    )
    login_response = await client.post(
        "/auth/login", json={"email": "erin@example.com", "password": "StrongPass123"}
    )
    access_token = login_response.json()["access_token"]

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert response.status_code == 200
    assert response.json()["email"] == "erin@example.com"
    assert response.json()["name"] == "Erin"


async def test_me_rejects_expired_token(client: AsyncClient, monkeypatch):
    import jwt

    from app.core.config import settings

    signup_response = await client.post(
        "/auth/signup",
        json={"name": "Frank", "email": "frank@example.com", "password": "StrongPass123"},
    )
    user_id = signup_response.json()["id"]

    from datetime import datetime, timedelta, timezone

    expired_token = jwt.encode(
        {
            "sub": user_id,
            "type": "access",
            "iat": datetime.now(timezone.utc) - timedelta(minutes=30),
            "exp": datetime.now(timezone.utc) - timedelta(minutes=15),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
