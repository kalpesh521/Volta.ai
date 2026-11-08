"""priority and email status on home notifications

Revision ID: 202610040003
Revises: 202610040002
Create Date: 2026-10-04 21:30:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202610040003"
down_revision: Union[str, None] = "202610040002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "home_notifications",
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="info"),
    )
    op.add_column(
        "home_notifications",
        sa.Column("email_status", sa.String(length=16), nullable=False, server_default="skipped"),
    )


def downgrade() -> None:
    op.drop_column("home_notifications", "email_status")
    op.drop_column("home_notifications", "priority")
