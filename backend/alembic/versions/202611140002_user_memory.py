"""user_memory_items: long-term, user-visible memory per user and home (tracker 3.4)

Revision ID: 202611140002
Revises: 202611140001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140002"
down_revision: Union[str, None] = "202611140001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_memory_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        # Masked text only: personal data is replaced by tokens before an item is stored.
        sa.Column("text", sa.String(length=400), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "household_id", "text_hash", name="uq_user_memory_text"),
    )
    op.create_index(
        "ix_user_memory_scope_updated", "user_memory_items", ["user_id", "household_id", "updated_at"]
    )
    op.create_index("ix_user_memory_items_expires_at", "user_memory_items", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_user_memory_items_expires_at", table_name="user_memory_items")
    op.drop_index("ix_user_memory_scope_updated", table_name="user_memory_items")
    op.drop_table("user_memory_items")
