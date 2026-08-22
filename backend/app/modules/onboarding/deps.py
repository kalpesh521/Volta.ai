"""
FastAPI dependency providers for the onboarding module.
Wires: DB session → OnboardingRepository → OnboardingService
"""
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.onboarding.repository import OnboardingRepository
from app.modules.onboarding.service import OnboardingService


def get_onboarding_repository(db: AsyncSession = Depends(get_db)) -> OnboardingRepository:
    return OnboardingRepository(db)


def get_onboarding_service(
    repo: OnboardingRepository = Depends(get_onboarding_repository),
) -> OnboardingService:
    return OnboardingService(repo)
