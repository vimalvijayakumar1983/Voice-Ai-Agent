"""Add structured, auditable call disposition details.

Revision ID: 20260903_020
Revises: 20260903_019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_020"
down_revision: str | None = "20260903_019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("disposition_profile", sa.String(length=30), nullable=True),
    )
    op.execute("UPDATE agents SET disposition_profile = 'general' WHERE disposition_profile IS NULL")
    op.alter_column("agents", "disposition_profile", nullable=False)
    op.add_column(
        "call_summaries",
        sa.Column(
            "disposition_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("call_summaries", "disposition_details")
    op.drop_column("agents", "disposition_profile")
