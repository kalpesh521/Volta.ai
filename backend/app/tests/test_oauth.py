"""
Google OAuth tests.

Uses a fake `OAuthProviderClient` (overriding the real Google client via
FastAPI's `dependency_overrides`) so these tests never hit the network -
they exercise our account-linking policy, not Google's infrastructure.
"""
import httpx
from httpx import AsyncClient

from app.core.deps import get_google_oauth_client
from app.services.oauth.base import OAuthProviderClient, OAuthUserInfo
from main import app


class FakeGoogleClient(OAuthProviderClient):
    provider_name = "google"

    def __init__(self, user_info: OAuthUserInfo):
        self._user_info = user_info

    def get_authorization_url(self, state: str) -> str:
        return f"https://accounts.google.com/mock?state={state}"

    async def fetch_user_info(self, code: str) -> OAuthUserInfo:
        return self._user_info


def _override_google_client(user_info: OAuthUserInfo) -> None:
    app.dependency_overrides[get_google_oauth_client] = lambda: FakeGoogleClient(user_info)


async def _call_callback(client: AsyncClient) -> httpx.Response:
    # Simulate the state cookie that /auth/google/login would have set.
    client.cookies.set("oauth_state", "test-state")
    return await client.get(
        "/auth/google/callback", params={"code": "fake-code", "state": "test-state"}
    )


async def test_google_login_redirects_to_google(client: AsyncClient):
    _override_google_client(
        OAuthUserInfo(
            provider="google",
            provider_user_id="irrelevant",
            email="irrelevant@example.com",
            email_verified=True,
        )
    )
    response = await client.get("/auth/google/login", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]
    assert "oauth_state" in response.cookies

    app.dependency_overrides.pop(get_google_oauth_client, None)


async def test_google_callback_rejects_mismatched_state(client: AsyncClient):
    _override_google_client(
        OAuthUserInfo(
            provider="google",
            provider_user_id="123",
            email="x@example.com",
            email_verified=True,
        )
    )
    client.cookies.set("oauth_state", "correct-state")
    response = await client.get(
        "/auth/google/callback", params={"code": "fake-code", "state": "wrong-state"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"

    app.dependency_overrides.pop(get_google_oauth_client, None)


async def test_google_login_creates_new_user(client: AsyncClient):
    _override_google_client(
        OAuthUserInfo(
            provider="google",
            provider_user_id="google-sub-new-1",
            email="newgoogleuser@example.com",
            email_verified=True,
            name="New User",
        )
    )

    response = await _call_callback(client)
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]

    me_response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "newgoogleuser@example.com"

    app.dependency_overrides.pop(get_google_oauth_client, None)


async def test_google_login_links_existing_verified_email_account(client: AsyncClient):
    signup_payload = {"email": "verifiedlink@example.com", "password": "StrongPass123"}
    signup_response = await client.post("/auth/signup", json=signup_payload)
    existing_user_id = signup_response.json()["id"]

    _override_google_client(
        OAuthUserInfo(
            provider="google",
            provider_user_id="google-sub-linked-1",
            email="verifiedlink@example.com",
            email_verified=True,
        )
    )

    response = await _call_callback(client)
    assert response.status_code == 200
    body = response.json()

    me_response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me_response.status_code == 200
    # Linked to the SAME existing account, not a new one.
    assert me_response.json()["id"] == existing_user_id

    app.dependency_overrides.pop(get_google_oauth_client, None)


async def test_google_login_does_not_autolink_unverified_email(client: AsyncClient):
    signup_payload = {"email": "unverifiedlink@example.com", "password": "StrongPass123"}
    await client.post("/auth/signup", json=signup_payload)

    _override_google_client(
        OAuthUserInfo(
            provider="google",
            provider_user_id="google-sub-unverified-1",
            email="unverifiedlink@example.com",
            email_verified=False,
        )
    )

    response = await _call_callback(client)
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "oauth_link_confirmation_required"
    link_token = body["error"]["link_token"]
    assert link_token

    # Confirming with the WRONG password must fail.
    wrong_confirm = await client.post(
        "/auth/google/link-confirm",
        json={"link_token": link_token, "password": "wrong-password"},
    )
    assert wrong_confirm.status_code == 401

    # Confirming with the correct password completes the link.
    confirm = await client.post(
        "/auth/google/link-confirm",
        json={"link_token": link_token, "password": "StrongPass123"},
    )
    assert confirm.status_code == 200
    assert confirm.json()["access_token"]

    app.dependency_overrides.pop(get_google_oauth_client, None)
