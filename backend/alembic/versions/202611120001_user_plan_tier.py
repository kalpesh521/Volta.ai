"""users.plan_tier: which token budget a user gets (free or plus)

Revision ID: 202611120001
Revises: 202611110001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611120001"
down_revision: Union[str, None] = "202611110001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing users as `free` without a table rewrite on current PostgreSQL.
    op.add_column(
        "users",
        sa.Column("plan_tier", sa.String(length=16), server_default="free", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "plan_tier")
