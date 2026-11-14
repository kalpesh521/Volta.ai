"""automation_rule_defs: stored DSL rules with fingerprint, approval and state (tracker 4.1, 4.2)

Revision ID: 202611140004
Revises: 202611140003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140004"
down_revision: Union[str, None] = "202611140003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automation_rule_defs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("rule_json", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending_approval", nullable=False),
        sa.Column("source", sa.String(length=12), server_default="api", nullable=False),
        sa.Column("dry_run_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("state_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("pause_reason", sa.String(length=200), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fire_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("household_id", "rule_id", name="uq_automation_rule_per_home"),
    )
    op.create_index("ix_automation_rule_defs_user_id", "automation_rule_defs", ["user_id"])
    op.create_index("ix_automation_rule_defs_active", "automation_rule_defs", ["household_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_automation_rule_defs_active", table_name="automation_rule_defs")
    op.drop_index("ix_automation_rule_defs_user_id", table_name="automation_rule_defs")
    op.drop_table("automation_rule_defs")
