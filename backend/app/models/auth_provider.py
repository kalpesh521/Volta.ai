"""
Auth provider identity linked to a user.

Kept as a separate table (not columns on `users`) so a single user can link
multiple providers (google, github, ...) later without any schema change -
`provider` is just a string, never hardcoded logic per-provider at the DB layer.
"""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from app.core.types import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class AuthProvider(Base):
    __tablename__ = "auth_providers"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_provider_provider_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # e.g. "google", "github" - free-form string, new providers need no migration.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # The stable subject/id the provider issues for this user (Google's `sub` claim, etc.)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="auth_providers")
