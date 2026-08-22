"""
Import all models here so Alembic's autogenerate and SQLAlchemy's mapper
configuration can discover every table via `Base.metadata`.
"""
from app.models.auth_provider import AuthProvider  # noqa: F401
from app.models.password_reset_token import PasswordResetToken  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.user import User  # noqa: F401

# Onboarding models — must be imported so Alembic sees these tables
from app.modules.onboarding.models import BatteryConfig  # noqa: F401
from app.modules.onboarding.models import GridConfig  # noqa: F401
from app.modules.onboarding.models import SolarSystem  # noqa: F401
from app.modules.onboarding.models import TrackedAppliance  # noqa: F401
