"""Add auditable structured knowledge compilation fields.

Revision ID: 20260903_019
Revises: 20260902_018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_019"
down_revision: str | None = "20260902_018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_sources", sa.Column("raw_content", sa.Text(), nullable=True))
    op.add_column(
        "knowledge_sources",
        sa.Column("structured_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "knowledge_sources", sa.Column("content_sha256", sa.String(64), nullable=True)
    )
    op.add_column(
        "knowledge_sources",
        sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_knowledge_sources_content_sha256",
        "knowledge_sources",
        ["content_sha256"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_sources_content_sha256", table_name="knowledge_sources")
    op.drop_column("knowledge_sources", "compiled_at")
    op.drop_column("knowledge_sources", "content_sha256")
    op.drop_column("knowledge_sources", "structured_content")
    op.drop_column("knowledge_sources", "raw_content")
