"""multi-home: drop unique user_id, add is_primary

Revision ID: 202608300001
Revises: 202608290001
Create Date: 2026-08-30 00:10:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202608300001"
down_revision: Union[str, None] = "202608290001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_solar_systems_user_id", "solar_systems", type_="unique")
    op.add_column(
        "solar_systems",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE solar_systems SET is_primary = true")
    op.alter_column("solar_systems", "is_primary", server_default=None)
    op.create_index(
        "uq_solar_systems_one_primary",
        "solar_systems",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("uq_solar_systems_one_primary", table_name="solar_systems")
    op.drop_column("solar_systems", "is_primary")
    op.create_unique_constraint("uq_solar_systems_user_id", "solar_systems", ["user_id"])
