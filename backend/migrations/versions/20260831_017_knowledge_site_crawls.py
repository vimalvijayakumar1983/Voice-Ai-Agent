"""Durable whole-site knowledge crawling.

Revision ID: 20260831_017
Revises: 20260831_016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831_017"
down_revision: str | None = "20260831_016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_crawls",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("knowledge_base_id", sa.UUID(), nullable=False),
        sa.Column("root_url", sa.Text(), nullable=False),
        sa.Column("allowed_host", sa.String(255), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued"),
        sa.Column("max_pages", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("max_depth", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("include_subdomains", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_knowledge_crawls_tenant_id", "knowledge_crawls", ["tenant_id"])
    op.create_index(
        "ix_knowledge_crawls_knowledge_base_id", "knowledge_crawls", ["knowledge_base_id"]
    )
    op.create_index("ix_knowledge_crawls_status", "knowledge_crawls", ["status"])

    op.create_table(
        "knowledge_crawl_pages",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("crawl_id", sa.UUID(), nullable=False),
        sa.Column("knowledge_source_id", sa.UUID(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovered_via", sa.String(30), nullable=False, server_default="link"),
        sa.Column("status", sa.String(30), nullable=False, server_default="discovered"),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["crawl_id"], ["knowledge_crawls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["knowledge_source_id"], ["knowledge_sources.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("crawl_id", "canonical_url", name="uq_knowledge_crawl_page_url"),
    )
    op.create_index("ix_knowledge_crawl_pages_tenant_id", "knowledge_crawl_pages", ["tenant_id"])
    op.create_index("ix_knowledge_crawl_pages_crawl_id", "knowledge_crawl_pages", ["crawl_id"])
    op.create_index(
        "ix_knowledge_crawl_pages_knowledge_source_id",
        "knowledge_crawl_pages",
        ["knowledge_source_id"],
    )
    op.create_index("ix_knowledge_crawl_pages_status", "knowledge_crawl_pages", ["status"])


def downgrade() -> None:
    op.drop_table("knowledge_crawl_pages")
    op.drop_table("knowledge_crawls")
