"""Widen provider language tags.

Revision ID: 20260827_007
Revises: 20260827_006
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_007"
down_revision: str | None = "20260827_006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "agents",
        "language",
        existing_type=sa.String(length=10),
        type_=sa.String(length=63),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM agents WHERE length(language) > 10) THEN
                    RAISE EXCEPTION
                        'Cannot narrow agents.language while values longer than 10 exist';
                END IF;
            END $$
            """
        )
    )
    op.alter_column(
        "agents",
        "language",
        existing_type=sa.String(length=63),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
