"""Retain original PDFs for reliable reprocessing.

Revision ID: 20260831_016
Revises: 20260830_015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_016"
down_revision: str | None = "20260830_015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_sources", sa.Column("file_content", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_sources", "file_content")
