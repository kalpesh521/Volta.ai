"""
Import all models here so Alembic's autogenerate and SQLAlchemy's mapper
configuration can discover every table via `Base.metadata`.
"""
from app.models.auth_provider import AuthProvider  # noqa: F401
from app.models.password_reset_token import PasswordResetToken  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.user import User  # noqa: F401
