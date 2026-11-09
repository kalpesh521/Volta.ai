"""knowledge chunks: full-text index for lexical retrieval

Fault codes ("E04"), model numbers and tariff clause ids are matched by exact words, which
embeddings handle poorly. A generated tsvector column with a GIN index lets Postgres find
them directly. The 'simple' configuration keeps tokens unstemmed so codes survive and
non-English text is not mangled.

Revision ID: 202611090002
Revises: 202611090001
"""
from typing import Sequence, Union

from alembic import op

revision: str = "202611090002"
down_revision: Union[str, None] = "202611090001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_content_tsv "
        "ON knowledge_chunks USING gin (content_tsv)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_content_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS content_tsv")
