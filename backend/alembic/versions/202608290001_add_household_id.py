"""add solar_systems.household_id — bind telemetry keys to a user

Revision ID: 202608290001
Revises: 202608220001
Create Date: 2026-08-29 23:20:00

Existing rows are backfilled from the system UUID so the column can be
NOT NULL + UNIQUE without a two-step app deploy.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202608290001"
down_revision: Union[str, None] = "202608220001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "solar_systems",
        sa.Column("household_id", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE solar_systems
        SET household_id = 'home_' || replace(id::text, '-', '')
        WHERE household_id IS NULL
        """
    )
    op.alter_column("solar_systems", "household_id", nullable=False)
    op.create_unique_constraint(
        "uq_solar_systems_household_id",
        "solar_systems",
        ["household_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_solar_systems_household_id", "solar_systems", type_="unique")
    op.drop_column("solar_systems", "household_id")
