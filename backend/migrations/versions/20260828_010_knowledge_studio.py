"""Provider-neutral Knowledge Studio domain.

Revision ID: 20260828_010
Revises: 20260827_009
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260828_010"
down_revision: str | None = "20260827_009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("knowledge_bases_agent_id_fkey", "knowledge_bases", type_="foreignkey")
    op.alter_column("knowledge_bases", "agent_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key(
        "knowledge_bases_agent_id_fkey",
        "knowledge_bases",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("knowledge_bases", "content_type", existing_type=sa.String(50), nullable=True)
    op.alter_column("knowledge_bases", "content", existing_type=sa.Text(), nullable=True)
    op.add_column("knowledge_bases", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "knowledge_bases",
        sa.Column("provider", sa.String(50), nullable=False, server_default="smallest"),
    )
    op.add_column(
        "knowledge_bases", sa.Column("provider_knowledge_base_id", sa.String(100), nullable=True)
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("sync_status", sa.String(30), nullable=False, server_default="local_only"),
    )
    op.add_column("knowledge_bases", sa.Column("sync_error", sa.Text(), nullable=True))
    op.add_column(
        "knowledge_bases",
        sa.Column("approval_status", sa.String(30), nullable=False, server_default="draft"),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("scope_type", sa.String(30), nullable=False, server_default="workspace"),
    )
    op.add_column("knowledge_bases", sa.Column("scope_label", sa.String(255), nullable=True))
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "languages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"en\"]'::jsonb"),
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("indexed_source_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_bases", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "knowledge_bases", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        op.f("ix_knowledge_bases_provider_knowledge_base_id"),
        "knowledge_bases",
        ["provider_knowledge_base_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_sources",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("provider_item_id", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_sources_tenant_id"), "knowledge_sources", ["tenant_id"])
    op.create_index(
        op.f("ix_knowledge_sources_knowledge_base_id"),
        "knowledge_sources",
        ["knowledge_base_id"],
    )

    op.create_table(
        "agent_knowledge_bindings",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False, server_default="smallest"),
        sa.Column("sync_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_agent_knowledge_bindings_agent_id"),
    )
    op.create_index(
        op.f("ix_agent_knowledge_bindings_tenant_id"), "agent_knowledge_bindings", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_agent_knowledge_bindings_agent_id"), "agent_knowledge_bindings", ["agent_id"]
    )
    op.create_index(
        op.f("ix_agent_knowledge_bindings_knowledge_base_id"),
        "agent_knowledge_bindings",
        ["knowledge_base_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_knowledge_bindings")
    op.drop_table("knowledge_sources")
    op.drop_index(
        op.f("ix_knowledge_bases_provider_knowledge_base_id"), table_name="knowledge_bases"
    )
    for column in (
        "published_at",
        "last_synced_at",
        "indexed_source_count",
        "source_count",
        "tags",
        "languages",
        "scope_label",
        "scope_type",
        "approval_status",
        "sync_error",
        "sync_status",
        "provider_knowledge_base_id",
        "provider",
        "description",
    ):
        op.drop_column("knowledge_bases", column)
    op.alter_column("knowledge_bases", "content", existing_type=sa.Text(), nullable=False)
    op.alter_column("knowledge_bases", "content_type", existing_type=sa.String(50), nullable=False)
    op.drop_constraint("knowledge_bases_agent_id_fkey", "knowledge_bases", type_="foreignkey")
    op.alter_column("knowledge_bases", "agent_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "knowledge_bases_agent_id_fkey",
        "knowledge_bases",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
