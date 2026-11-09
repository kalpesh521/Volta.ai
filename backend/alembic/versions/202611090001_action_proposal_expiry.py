"""action proposals: confirmation expiry

Revision ID: 202611090001
Revises: 202610040003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611090001"
down_revision: Union[str, None] = "202610040003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("action_proposals", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    # Rows still waiting when this runs get a deadline from their creation time so
    # they cannot be confirmed indefinitely.
    op.execute(
        "UPDATE action_proposals SET expires_at = created_at + INTERVAL '2 minutes' "
        "WHERE status = 'awaiting_confirmation' AND expires_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("action_proposals", "expires_at")
