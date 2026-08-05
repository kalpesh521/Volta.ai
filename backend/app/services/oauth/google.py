"""
Google OAuth2 / OpenID Connect client.

Verifies the ID token's signature (against Google's published JWKS),
audience (must equal our `GOOGLE_CLIENT_ID`), and issuer server-side -
we never trust the profile fields until the signature checks out.
"""
import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import settings
from app.core.exceptions import InvalidTokenError
from app.services.oauth.base import OAuthProviderClient, OAuthUserInfo

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_VALID_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

# Module-level so the JWKS (Google's public signing keys) is cached across requests
# instead of being re-fetched on every single login.
_jwks_client = PyJWKClient(GOOGLE_JWKS_URL)


class GoogleOAuthClient(OAuthProviderClient):
    provider_name = "google"

    def get_authorization_url(self, state: str) -> str:
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,  # CSRF protection - verified again on callback
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{GOOGLE_AUTH_URL}?{httpx.QueryParams(params)}"

    async def fetch_user_info(self, code: str) -> OAuthUserInfo:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )

        if token_response.status_code != 200:
            raise InvalidTokenError("Failed to exchange authorization code with Google")

        id_token = token_response.json().get("id_token")
        if not id_token:
            raise InvalidTokenError("Google did not return an ID token")

        return self._verify_id_token(id_token)

    def _verify_id_token(self, id_token: str) -> OAuthUserInfo:
        try:
            signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=settings.GOOGLE_CLIENT_ID,
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(f"Invalid Google ID token: {exc}") from exc

        if claims.get("iss") not in GOOGLE_VALID_ISSUERS:
            raise InvalidTokenError("Invalid Google ID token issuer")

        email = claims.get("email")
        if not email:
            raise InvalidTokenError("Google ID token did not include an email address")

        return OAuthUserInfo(
            provider=self.provider_name,
            provider_user_id=claims["sub"],
            email=email,
            email_verified=bool(claims.get("email_verified", False)),
            name=claims.get("name"),
        )
