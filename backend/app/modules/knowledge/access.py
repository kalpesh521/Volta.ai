"""Who counts as a Suryaa admin: the user flag, or an email listed in the environment."""
from __future__ import annotations

from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.models.user import User


def is_suryaa_admin(user: User) -> bool:
    if user.is_admin:
        return True
    allowed = {
        email.strip().lower()
        for email in settings.SURYAA_ADMIN_EMAILS.split(",")
        if email.strip()
    }
    return user.email.lower() in allowed


def require_suryaa_admin(user: User) -> User:
    if not is_suryaa_admin(user):
        raise ForbiddenError("Suryaa admin access is required.")
    return user
