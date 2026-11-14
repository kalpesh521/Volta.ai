"""device command outbox: durable hand-off from a confirmed action to the broker (tracker 4.3, 4.5)

Revision ID: 202611140003
Revises: 202611140002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140003"
down_revision: Union[str, None] = "202611140002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_command_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("command_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_device_command_outbox_household_id", "device_command_outbox", ["household_id"])
    op.create_index("ix_device_command_outbox_due", "device_command_outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_device_command_outbox_due", table_name="device_command_outbox")
    op.drop_index("ix_device_command_outbox_household_id", table_name="device_command_outbox")
    op.drop_table("device_command_outbox")
