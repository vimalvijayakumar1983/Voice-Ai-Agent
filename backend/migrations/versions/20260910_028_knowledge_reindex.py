"""Record when a knowledge base was last re-indexed with the current pipeline.

Revision ID: 20260910_028
Revises: 20260910_027

After a re-index every source has been through a path that measures coverage,
so approval treats a missing coverage report as an error from then on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_028"
down_revision: str | None = "20260910_027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column("reindex_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_bases", "reindex_requested_at")
