"""assistant conversations: owner and age of each chat thread

Revision ID: 202611110001
Revises: 202611090002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611110001"
down_revision: Union[str, None] = "202611090002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_conversations_user_id", "assistant_conversations", ["user_id"])
    op.create_index(
        "ix_assistant_conversations_last_active_at", "assistant_conversations", ["last_active_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_conversations_last_active_at", table_name="assistant_conversations")
    op.drop_index("ix_assistant_conversations_user_id", table_name="assistant_conversations")
    op.drop_table("assistant_conversations")
