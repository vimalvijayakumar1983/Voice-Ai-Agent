"""Add immutable blue/green knowledge serving revisions.

Revision ID: 20260904_023
Revises: 20260904_022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_023"
down_revision: str | None = "20260904_022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_serving_revisions",
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("speech_lexicon_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("source_revision_sha256", sa.String(length=64), nullable=False),
        sa.Column("chunk_revision_sha256", sa.String(length=64), nullable=False),
        sa.Column("fact_revision_sha256", sa.String(length=64), nullable=False),
        sa.Column("entity_revision_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("knowledge_name", sa.String(length=255), nullable=False),
        sa.Column("knowledge_description", sa.Text(), nullable=True),
        sa.Column("knowledge_content", sa.Text(), nullable=True),
        sa.Column("scope_type", sa.String(length=30), nullable=False),
        sa.Column("scope_label", sa.String(length=255), nullable=True),
        sa.Column(
            "languages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_knowledge_base_id", sa.String(length=100), nullable=True),
        sa.Column(
            "manifest",
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
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["speech_lexicon_artifact_id"],
            ["knowledge_speech_lexicons.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "content_sha256",
            "compiler_version",
            name="uq_knowledge_serving_revision_content",
        ),
    )
    op.create_index(
        "ix_knowledge_serving_revisions_tenant_id",
        "knowledge_serving_revisions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_serving_revisions_knowledge_base_id",
        "knowledge_serving_revisions",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_knowledge_serving_revisions_speech_lexicon_artifact_id",
        "knowledge_serving_revisions",
        ["speech_lexicon_artifact_id"],
    )
    op.create_index(
        "ix_knowledge_serving_revisions_content_sha256",
        "knowledge_serving_revisions",
        ["content_sha256"],
    )

    op.create_table(
        "knowledge_serving_revision_sources",
        sa.Column("serving_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "structured_content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
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
            ["serving_revision_id"],
            ["knowledge_serving_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "serving_revision_id",
            "original_source_id",
            name="uq_knowledge_serving_revision_source",
        ),
    )
    op.create_index(
        "ix_knowledge_serving_revision_sources_tenant_id",
        "knowledge_serving_revision_sources",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_serving_revision_sources_serving_revision_id",
        "knowledge_serving_revision_sources",
        ["serving_revision_id"],
    )
    op.create_index(
        "ix_knowledge_serving_revision_sources_knowledge_base_id",
        "knowledge_serving_revision_sources",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_knowledge_serving_revision_sources_content_sha256",
        "knowledge_serving_revision_sources",
        ["content_sha256"],
    )
    op.create_index(
        "ix_knowledge_serving_revision_sources_revision_original",
        "knowledge_serving_revision_sources",
        ["serving_revision_id", "original_source_id"],
    )
    # The runtime searches immutable snapshots, not mutable draft sources.
    # Keep relevance selection in PostgreSQL so LIMIT is applied after rank.
    op.execute(
        """
        CREATE INDEX ix_knowledge_serving_revision_sources_search
        ON knowledge_serving_revision_sources
        USING gin (
          to_tsvector(
            'simple'::regconfig,
            coalesce(name, '') || ' ' || coalesce(location, '') || ' ' || coalesce(content, '')
          )
        )
        """
    )

    op.add_column(
        "knowledge_bases",
        sa.Column("serving_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_knowledge_bases_serving_revision_id",
        "knowledge_bases",
        ["serving_revision_id"],
    )
    # See migration 022: ownership flows from the knowledge base to immutable
    # releases, while this reverse FK protects the currently served pointer.
    op.create_foreign_key(
        "fk_knowledge_bases_serving_revision_id",
        "knowledge_bases",
        "knowledge_serving_revisions",
        ["serving_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_knowledge_bases_serving_revision_id",
        "knowledge_bases",
        type_="foreignkey",
    )
    op.drop_index("ix_knowledge_bases_serving_revision_id", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "serving_revision_id")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_serving_revision_sources_search")
    op.drop_index(
        "ix_knowledge_serving_revision_sources_revision_original",
        table_name="knowledge_serving_revision_sources",
    )
    op.drop_index(
        "ix_knowledge_serving_revision_sources_content_sha256",
        table_name="knowledge_serving_revision_sources",
    )
    op.drop_index(
        "ix_knowledge_serving_revision_sources_knowledge_base_id",
        table_name="knowledge_serving_revision_sources",
    )
    op.drop_index(
        "ix_knowledge_serving_revision_sources_serving_revision_id",
        table_name="knowledge_serving_revision_sources",
    )
    op.drop_index(
        "ix_knowledge_serving_revision_sources_tenant_id",
        table_name="knowledge_serving_revision_sources",
    )
    op.drop_table("knowledge_serving_revision_sources")
    op.drop_index(
        "ix_knowledge_serving_revisions_content_sha256",
        table_name="knowledge_serving_revisions",
    )
    op.drop_index(
        "ix_knowledge_serving_revisions_speech_lexicon_artifact_id",
        table_name="knowledge_serving_revisions",
    )
    op.drop_index(
        "ix_knowledge_serving_revisions_knowledge_base_id",
        table_name="knowledge_serving_revisions",
    )
    op.drop_index(
        "ix_knowledge_serving_revisions_tenant_id",
        table_name="knowledge_serving_revisions",
    )
    op.drop_table("knowledge_serving_revisions")
