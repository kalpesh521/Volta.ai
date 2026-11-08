"""audit, virtual device overrides, notifications, and automation rules

Revision ID: 202610040002
Revises: 202610040001
Create Date: 2026-10-04 18:55:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202610040002"
down_revision: Union[str, None] = "202610040001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("action_proposals", sa.Column("duration_minutes", sa.Integer(), nullable=True))
    op.create_table(
        "action_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_audit_events_proposal_id", "action_audit_events", ["proposal_id"])
    op.create_index("ix_action_audit_events_user_id", "action_audit_events", ["user_id"])
    op.create_index("ix_action_audit_events_household_id", "action_audit_events", ["household_id"])
    op.create_index("ix_action_audit_events_command_id", "action_audit_events", ["command_id"])

    op.create_table(
        "device_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("device", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=16), nullable=False),
        sa.Column("power_kw", sa.Float(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_device_overrides_household_id", "device_overrides", ["household_id"])
    op.create_index("ix_device_overrides_command_id", "device_overrides", ["command_id"])
    op.create_index("ix_device_overrides_status", "device_overrides", ["status"])

    op.create_table(
        "home_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("ref_id", sa.String(length=64), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_home_notifications_user_id", "home_notifications", ["user_id"])
    op.create_index("ix_home_notifications_household_id", "home_notifications", ["household_id"])
    op.create_index("ix_home_notifications_kind", "home_notifications", ["kind"])

    op.create_table(
        "automation_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("device", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=16), nullable=False),
        sa.Column("surplus_kw_min", sa.Float(), nullable=True),
        sa.Column("soc_percent_min", sa.Float(), nullable=True),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_rules_user_id", "automation_rules", ["user_id"])
    op.create_index("ix_automation_rules_household_id", "automation_rules", ["household_id"])


def downgrade() -> None:
    op.drop_index("ix_automation_rules_household_id", table_name="automation_rules")
    op.drop_index("ix_automation_rules_user_id", table_name="automation_rules")
    op.drop_table("automation_rules")
    op.drop_index("ix_home_notifications_kind", table_name="home_notifications")
    op.drop_index("ix_home_notifications_household_id", table_name="home_notifications")
    op.drop_index("ix_home_notifications_user_id", table_name="home_notifications")
    op.drop_table("home_notifications")
    op.drop_index("ix_device_overrides_status", table_name="device_overrides")
    op.drop_index("ix_device_overrides_command_id", table_name="device_overrides")
    op.drop_index("ix_device_overrides_household_id", table_name="device_overrides")
    op.drop_table("device_overrides")
    op.drop_index("ix_action_audit_events_command_id", table_name="action_audit_events")
    op.drop_index("ix_action_audit_events_household_id", table_name="action_audit_events")
    op.drop_index("ix_action_audit_events_user_id", table_name="action_audit_events")
    op.drop_index("ix_action_audit_events_proposal_id", table_name="action_audit_events")
    op.drop_table("action_audit_events")
    op.drop_column("action_proposals", "duration_minutes")
