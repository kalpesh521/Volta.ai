"""
Authorize energy reads against the household bound to the current user.

Reads require:
  1. a solar_systems row whose household_id matches the path
  2. that row belongs to the current user
  3. onboarding is complete (dashboard unlock)

Unknown or other users' households return 404 (no existence leak).
The owner's incomplete onboarding returns 403.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Path, Query

from app.core.deps import get_current_user, get_current_user_optional
from app.core.exceptions import EnergyNotFoundError, ForbiddenError, InvalidTokenError
from app.models.user import User
from app.modules.energy.deps import verify_ingest_token
from app.modules.energy.schemas import _HOUSEHOLD_ID_PATTERN
from app.modules.onboarding.deps import get_onboarding_repository
from app.modules.onboarding.models import SolarSystem
from app.modules.onboarding.repository import OnboardingRepository

HouseholdIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=_HOUSEHOLD_ID_PATTERN,
        description="Household id assigned at onboarding, e.g. home_a1b2c3d4e5f6",
    ),
]


def _deny_unknown() -> None:
    raise EnergyNotFoundError("No telemetry found for this household.")


async def require_my_completed_system(
    household_id: str | None = Query(
        default=None,
        description="Optional home. Omit to use the primary home from the login token.",
    ),
    current_user: User = Depends(get_current_user),
    repo: OnboardingRepository = Depends(get_onboarding_repository),
) -> SolarSystem:
    """Resolve a home the JWT owns. Token-only URLs use the primary home."""
    requested = (household_id or "").strip()
    if requested and requested not in {"home_001"}:
        system = await repo.get_system_by_household(requested)
        if system is None or system.user_id != current_user.id:
            _deny_unknown()
        assert system is not None
        if not system.is_complete:
            raise ForbiddenError("Complete onboarding to view energy data.")
        return system

    system = await repo.get_primary_system(current_user.id)
    if system is None:
        raise ForbiddenError("Complete onboarding to view energy data.")
    if not system.is_complete:
        raise ForbiddenError("Complete onboarding to view energy data.")
    return system


async def require_owned_household(
    household_id: HouseholdIdPath,
    current_user: User = Depends(get_current_user),
    repo: OnboardingRepository = Depends(get_onboarding_repository),
) -> SolarSystem:
    """Path household_id must belong to the authenticated user."""
    system = await repo.get_system_by_household(household_id)
    if system is None or system.user_id != current_user.id:
        _deny_unknown()
    assert system is not None
    if not system.is_complete:
        raise ForbiddenError("Complete onboarding to view energy data.")
    return system


async def require_profile_access(
    household_id: HouseholdIdPath,
    x_ingest_token: str | None = Header(
        default=None,
        alias="X-Ingest-Token",
        description="Simulator shared secret. Alternative to a user JWT.",
    ),
    current_user: User | None = Depends(get_current_user_optional),
    repo: OnboardingRepository = Depends(get_onboarding_repository),
) -> SolarSystem:
    """
    Simulator uses X-Ingest-Token. Dashboard uses the user JWT.
    Incomplete onboarding is 404 so ticks are not generated for a half-configured home.
    """
    if (x_ingest_token or "").strip():
        verify_ingest_token(x_ingest_token)
        system = await repo.get_system_by_household(household_id)
        if system is None or not system.is_complete:
            _deny_unknown()
        assert system is not None
        return system

    if current_user is None:
        raise InvalidTokenError()

    system = await repo.get_system_by_household(household_id)
    if system is None or system.user_id != current_user.id:
        _deny_unknown()
    assert system is not None
    if not system.is_complete:
        raise ForbiddenError("Complete onboarding to view energy data.")
    return system
