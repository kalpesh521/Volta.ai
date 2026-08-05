"""
Google OAuth2 / OIDC endpoints.

Written against the generic `OAuthProviderClient` interface (injected via
`Depends`), so a second provider can be mounted at `/auth/<provider>/...`
later by adding a new router + client, without touching this file's logic.
"""
import secrets

from fastapi import APIRouter, Cookie, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from app.core.deps import get_google_oauth_client, get_oauth_service
from app.core.exceptions import InvalidTokenError
from app.schemas.auth import OAuthLinkConfirmRequest, TokenResponse
from app.services.oauth.base import OAuthProviderClient
from app.services.oauth_service import OAuthService

router = APIRouter(prefix="/auth/google", tags=["oauth"])

_STATE_COOKIE_NAME = "oauth_state"


@router.get("/login")
async def google_login(
    response: Response, client: OAuthProviderClient = Depends(get_google_oauth_client)
) -> RedirectResponse:
    """Redirects the browser to Google's consent screen. A random `state` value
    is stored in an httponly cookie and echoed back by Google on the callback,
    preventing CSRF on the callback endpoint."""
    state = secrets.token_urlsafe(32)
    redirect = RedirectResponse(url=client.get_authorization_url(state))
    redirect.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        samesite="lax",
        max_age=600,
    )
    return redirect


@router.get("/callback", response_model=TokenResponse)
async def google_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    oauth_state_cookie: str | None = Cookie(default=None, alias=_STATE_COOKIE_NAME),
    client: OAuthProviderClient = Depends(get_google_oauth_client),
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    """
    Exchanges the authorization code for Google's tokens, verifies the ID
    token, and either logs the user in, links to an existing account, or
    creates a new one - see `OAuthService.authenticate` for the linking policy.
    """
    if oauth_state_cookie is None or not secrets.compare_digest(oauth_state_cookie, state):
        raise InvalidTokenError("Invalid OAuth state - possible CSRF attempt")

    response.delete_cookie(_STATE_COOKIE_NAME)
    return await oauth_service.authenticate(client, code)


@router.post("/link-confirm", response_model=TokenResponse)
async def confirm_google_link(
    data: OAuthLinkConfirmRequest, oauth_service: OAuthService = Depends(get_oauth_service)
) -> TokenResponse:
    """
    Completes account linking when `/callback` responded with 403
    `oauth_link_confirmation_required` (Google didn't verify the email). The
    caller proves ownership of the existing account with its password.
    """
    return await oauth_service.confirm_link(data.link_token, data.password)
