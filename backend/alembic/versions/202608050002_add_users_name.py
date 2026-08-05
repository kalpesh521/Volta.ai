"""add users.name column for signup / oauth display name

Revision ID: 202608050002
Revises: 202608050001
Create Date: 2026-08-05 20:40:00

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202608050002"
down_revision: Union[str, None] = "202608050001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing Neon rows; removed afterward so new
    # inserts must supply an explicit name from the app layer.
    op.add_column(
        "users",
        sa.Column("name", sa.String(length=100), nullable=False, server_default=""),
    )
    op.alter_column("users", "name", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "name")
