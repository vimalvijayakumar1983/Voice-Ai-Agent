"""Add multi-language configuration to voice agents.

Revision ID: 20260827_002
Revises: 20260827_001
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260827_002"
down_revision: str | None = "20260827_001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "supported_languages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute("UPDATE agents SET supported_languages = jsonb_build_array(language)")
    op.alter_column(
        "agents",
        "supported_languages",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[\"en\"]'::jsonb"),
    )


def downgrade() -> None:
    op.drop_column("agents", "supported_languages")
