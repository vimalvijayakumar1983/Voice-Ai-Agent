"""Add immutable, versioned knowledge speech-lexicon artifacts.

Revision ID: 20260904_022
Revises: 20260904_021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_022"
down_revision: str | None = "20260904_021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_speech_lexicons",
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column(
            "entries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_revisions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "coverage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "source_revision_sha256",
            "compiler_version",
            name="uq_knowledge_speech_lexicon_revision",
        ),
    )
    op.create_index(
        "ix_knowledge_speech_lexicons_tenant_id",
        "knowledge_speech_lexicons",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_speech_lexicons_knowledge_base_id",
        "knowledge_speech_lexicons",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_knowledge_speech_lexicons_content_sha256",
        "knowledge_speech_lexicons",
        ["content_sha256"],
    )
    op.create_index(
        "ix_knowledge_speech_lexicons_tenant_kb_created",
        "knowledge_speech_lexicons",
        ["tenant_id", "knowledge_base_id", "created_at"],
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("speech_lexicon_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_knowledge_bases_speech_lexicon_artifact_id",
        "knowledge_bases",
        ["speech_lexicon_artifact_id"],
    )
    # This is intentionally a pointer FK rather than an ownership FK. The
    # artifact owns/cascades with its knowledge base; deleting an individual
    # historical artifact safely clears only the mutable publication pointer.
    # Tenant/knowledge-base ownership is additionally enforced by publication
    # and every runtime lookup.
    op.create_foreign_key(
        "fk_knowledge_bases_speech_lexicon_artifact_id",
        "knowledge_bases",
        "knowledge_speech_lexicons",
        ["speech_lexicon_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_knowledge_bases_speech_lexicon_artifact_id",
        "knowledge_bases",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_knowledge_bases_speech_lexicon_artifact_id",
        table_name="knowledge_bases",
    )
    op.drop_column("knowledge_bases", "speech_lexicon_artifact_id")
    op.drop_index(
        "ix_knowledge_speech_lexicons_tenant_kb_created",
        table_name="knowledge_speech_lexicons",
    )
    op.drop_index(
        "ix_knowledge_speech_lexicons_content_sha256",
        table_name="knowledge_speech_lexicons",
    )
    op.drop_index(
        "ix_knowledge_speech_lexicons_knowledge_base_id",
        table_name="knowledge_speech_lexicons",
    )
    op.drop_index(
        "ix_knowledge_speech_lexicons_tenant_id",
        table_name="knowledge_speech_lexicons",
    )
    op.drop_table("knowledge_speech_lexicons")
