"""onboarding schema — solar_systems, battery_configs, grid_configs, tracked_appliances

Revision ID: 202608220001
Revises: 202608050002
Create Date: 2026-08-22 18:00:00

Tables created
──────────────
solar_systems       — one per user; stores panel, inverter & onboarding state
battery_configs     — one per system; Off-grid / Hybrid only
grid_configs        — one per system; On-grid  / Hybrid only
tracked_appliances  — many per system; appliance catalog selections
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202608220001"
down_revision: Union[str, None] = "202608050002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── solar_systems ────────────────────────────────────────────────────────
    op.create_table(
        "solar_systems",
        sa.Column("id",                   sa.Uuid(),             nullable=False),
        sa.Column("user_id",              sa.Uuid(),             nullable=False),

        # Step 1: panel info
        sa.Column("panel_type",           sa.String(20),         nullable=False),
        sa.Column("panel_qty",            sa.Integer(),          nullable=False),
        sa.Column("system_type",          sa.String(10),         nullable=False),
        sa.Column("location",             sa.String(200),        nullable=True),
        sa.Column("avg_monthly_bill",     sa.Numeric(10, 2),     nullable=True),

        # Step 2: inverter info
        sa.Column("inverter_brand",       sa.String(30),         nullable=False),
        sa.Column("inverter_capacity_kw", sa.Numeric(6, 2),      nullable=False),

        # Onboarding state
        sa.Column("last_step",            sa.String(20),         nullable=False, server_default="system"),
        sa.Column("appliances_configured",sa.Boolean(),          nullable=False, server_default=sa.false()),
        sa.Column("is_complete",          sa.Boolean(),          nullable=False, server_default=sa.false()),
        sa.Column("completed_at",         sa.DateTime(timezone=True), nullable=True),

        # Timestamps
        sa.Column("created_at",           sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at",           sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_solar_systems_user_id"),
        sa.CheckConstraint("panel_qty >= 1",            name="ck_solar_systems_panel_qty"),
        sa.CheckConstraint("inverter_capacity_kw >= 1", name="ck_solar_systems_inverter_capacity"),
    )
    op.create_index("ix_solar_systems_user_id", "solar_systems", ["user_id"])

    # ── battery_configs ───────────────────────────────────────────────────────
    op.create_table(
        "battery_configs",
        sa.Column("id",                   sa.Uuid(),         nullable=False),
        sa.Column("system_id",            sa.Uuid(),         nullable=False),
        sa.Column("battery_capacity_kwh", sa.Numeric(6, 2),  nullable=False),
        sa.Column("backup_hours",         sa.Integer(),      nullable=False),  # 2 | 4 | 8
        sa.Column("reserve_pct",          sa.Integer(),      nullable=False),  # 5–50
        sa.Column("created_at",           sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at",           sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["system_id"], ["solar_systems.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("system_id", name="uq_battery_configs_system_id"),
        sa.CheckConstraint("battery_capacity_kwh >= 1",        name="ck_battery_capacity"),
        sa.CheckConstraint("backup_hours IN (2, 4, 8)",        name="ck_backup_hours"),
        sa.CheckConstraint("reserve_pct BETWEEN 5 AND 50",     name="ck_reserve_pct"),
    )

    # ── grid_configs ──────────────────────────────────────────────────────────
    op.create_table(
        "grid_configs",
        sa.Column("id",                  sa.Uuid(),         nullable=False),
        sa.Column("system_id",           sa.Uuid(),         nullable=False),
        sa.Column("meter_type",          sa.String(20),     nullable=False),
        sa.Column("sanctioned_load_kw",  sa.Numeric(6, 2),  nullable=False),
        sa.Column("tariff_type",         sa.String(20),     nullable=False),
        sa.Column("discom",              sa.String(100),    nullable=True),
        sa.Column("created_at",          sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at",          sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["system_id"], ["solar_systems.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("system_id", name="uq_grid_configs_system_id"),
        sa.CheckConstraint("sanctioned_load_kw >= 1", name="ck_sanctioned_load"),
    )

    # ── tracked_appliances ────────────────────────────────────────────────────
    op.create_table(
        "tracked_appliances",
        sa.Column("id",            sa.Uuid(),      nullable=False),
        sa.Column("system_id",     sa.Uuid(),      nullable=False),
        sa.Column("appliance_key", sa.String(20),  nullable=False),
        sa.Column("is_critical",   sa.Boolean(),   nullable=False, server_default=sa.false()),
        sa.Column("created_at",    sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["system_id"], ["solar_systems.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("system_id", "appliance_key", name="uq_system_appliance"),
        sa.CheckConstraint(
            "appliance_key IN ('wash','heater','ev','pump','ac','fridge')",
            name="ck_appliance_key",
        ),
    )
    op.create_index("ix_tracked_appliances_system_id", "tracked_appliances", ["system_id"])


def downgrade() -> None:
    op.drop_index("ix_tracked_appliances_system_id", table_name="tracked_appliances")
    op.drop_table("tracked_appliances")
    op.drop_table("grid_configs")
    op.drop_table("battery_configs")
    op.drop_index("ix_solar_systems_user_id", table_name="solar_systems")
    op.drop_table("solar_systems")
