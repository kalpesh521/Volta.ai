"""
Provider-agnostic OAuth contract.

Any new social login provider (GitHub, Microsoft, ...) just implements this
interface and gets registered in `app.modules.auth.deps` - nothing in the
router, service, or account-linking logic needs to change or hardcode
"google" anywhere.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthUserInfo:
    provider: str
    provider_user_id: str
    email: str
    email_verified: bool
    name: str | None = None


class OAuthProviderClient(ABC):
    provider_name: str

    @abstractmethod
    def get_authorization_url(self, state: str) -> str:
        """Builds the URL to redirect the browser to for the consent screen."""

    @abstractmethod
    async def fetch_user_info(self, code: str) -> OAuthUserInfo:
        """Exchanges the authorization `code` for tokens and returns verified identity."""
