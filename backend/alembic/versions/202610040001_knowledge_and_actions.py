"""knowledge documents, pgvector column, and action proposals

Revision ID: 202610040001
Revises: 202609130001
Create Date: 2026-10-04 14:10:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "202610040001"
down_revision: Union[str, None] = "202609130001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("users", "is_admin", server_default=None)

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("doc_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("household_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("brand", sa.String(length=80), nullable=True),
        sa.Column("discom", sa.String(length=80), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("version_group_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(length=80), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_group_id", "version_number", name="uq_knowledge_version"),
    )
    op.create_index("ix_knowledge_documents_scope", "knowledge_documents", ["scope"])
    op.create_index("ix_knowledge_documents_doc_type", "knowledge_documents", ["doc_type"])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
    op.create_index("ix_knowledge_documents_owner_user_id", "knowledge_documents", ["owner_user_id"])
    op.create_index("ix_knowledge_documents_sha256", "knowledge_documents", ["sha256"])
    op.create_index("ix_knowledge_documents_version_group_id", "knowledge_documents", ["version_group_id"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk"),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])

    op.create_table(
        "knowledge_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_reviews_document_id", "knowledge_reviews", ["document_id"])

    op.create_table(
        "bill_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("units_kwh", sa.Numeric(12, 3), nullable=True),
        sa.Column("tariff_category", sa.String(length=80), nullable=True),
        sa.Column("sanctioned_load_kw", sa.Numeric(8, 3), nullable=True),
        sa.Column("amount_inr", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id"),
    )
    op.create_index("ix_bill_extractions_user_id", "bill_extractions", ["user_id"])

    op.create_table(
        "action_proposals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("question", sa.String(length=500), nullable=False),
        sa.Column("device", sa.String(length=64), nullable=True),
        sa.Column("command", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_proposals_user_id", "action_proposals", ["user_id"])
    op.create_index("ix_action_proposals_household_id", "action_proposals", ["household_id"])
    op.create_index("ix_action_proposals_status", "action_proposals", ["status"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_vec vector(256)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_vec "
            "ON knowledge_chunks USING hnsw (embedding_vec vector_cosine_ops)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_vec")
        op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS embedding_vec")
    op.drop_index("ix_action_proposals_status", table_name="action_proposals")
    op.drop_index("ix_action_proposals_household_id", table_name="action_proposals")
    op.drop_index("ix_action_proposals_user_id", table_name="action_proposals")
    op.drop_table("action_proposals")
    op.drop_index("ix_bill_extractions_user_id", table_name="bill_extractions")
    op.drop_table("bill_extractions")
    op.drop_index("ix_knowledge_reviews_document_id", table_name="knowledge_reviews")
    op.drop_table("knowledge_reviews")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_documents_version_group_id", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_sha256", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_owner_user_id", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_status", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_doc_type", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_scope", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_column("users", "is_admin")
