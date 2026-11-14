"""knowledge chunk safety: risk score, quarantine flag and masked-value counts (tracker 3.8)

Revision ID: 202611140001
Revises: 202611120001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140001"
down_revision: Union[str, None] = "202611120001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing chunks were never screened: they read as risk 0, not quarantined, nothing masked. Re-upload a document
    # to screen it. server_defaults backfill without a table rewrite on current PostgreSQL.
    op.add_column("knowledge_chunks", sa.Column("risk_score", sa.Float(), server_default="0", nullable=False))
    op.add_column("knowledge_chunks", sa.Column("quarantined", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("knowledge_chunks", sa.Column("masked_values", sa.Integer(), server_default="0", nullable=False))
    op.add_column("knowledge_documents", sa.Column("quarantined_chunk_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("knowledge_documents", sa.Column("masked_value_count", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("knowledge_documents", "masked_value_count")
    op.drop_column("knowledge_documents", "quarantined_chunk_count")
    op.drop_column("knowledge_chunks", "masked_values")
    op.drop_column("knowledge_chunks", "quarantined")
    op.drop_column("knowledge_chunks", "risk_score")
