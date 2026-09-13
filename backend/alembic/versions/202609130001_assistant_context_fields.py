"""assistant context fields: primary_goal + numeric tariff on grid_configs

Revision ID: 202609130001
Revises: 202608300001
Create Date: 2026-09-13 20:15:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202609130001"
down_revision: Union[str, None] = "202608300001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "solar_systems",
        sa.Column(
            "primary_goal",
            sa.String(length=40),
            nullable=False,
            server_default="maximize_self_consumption",
        ),
    )
    op.alter_column("solar_systems", "primary_goal", server_default=None)

    op.add_column(
        "grid_configs",
        sa.Column(
            "energy_charge_inr_per_kwh",
            sa.Numeric(8, 2),
            nullable=False,
            server_default="8.50",
        ),
    )
    op.add_column(
        "grid_configs",
        sa.Column(
            "export_credit_inr_per_kwh",
            sa.Numeric(8, 2),
            nullable=False,
            server_default="6.20",
        ),
    )
    op.create_check_constraint(
        "ck_energy_charge_nonneg",
        "grid_configs",
        "energy_charge_inr_per_kwh >= 0",
    )
    op.create_check_constraint(
        "ck_export_credit_nonneg",
        "grid_configs",
        "export_credit_inr_per_kwh >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_export_credit_nonneg", "grid_configs", type_="check")
    op.drop_constraint("ck_energy_charge_nonneg", "grid_configs", type_="check")
    op.drop_column("grid_configs", "export_credit_inr_per_kwh")
    op.drop_column("grid_configs", "energy_charge_inr_per_kwh")
    op.drop_column("solar_systems", "primary_goal")
