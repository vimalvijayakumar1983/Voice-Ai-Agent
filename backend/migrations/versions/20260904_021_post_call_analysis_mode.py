"""Add provider-aware post-call analysis policy.

Revision ID: 20260904_021
Revises: 20260903_020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_021"
down_revision: str | None = "20260903_020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("post_call_analysis_mode", sa.String(length=30), nullable=True),
    )
    op.execute(
        "UPDATE agents SET post_call_analysis_mode = 'provider_first' "
        "WHERE post_call_analysis_mode IS NULL"
    )
    op.alter_column("agents", "post_call_analysis_mode", nullable=False)


def downgrade() -> None:
    op.drop_column("agents", "post_call_analysis_mode")
