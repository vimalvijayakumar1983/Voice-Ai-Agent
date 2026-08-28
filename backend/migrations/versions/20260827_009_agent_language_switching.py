"""Persist governed language-switching configuration.

Revision ID: 20260827_009
Revises: 20260827_008
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_009"
down_revision: str | None = "20260827_008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "language_switching_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "language_switching_mode",
            sa.String(length=20),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.create_check_constraint(
        "ck_agents_language_switching_mode",
        "agents",
        "language_switching_mode IN ('disabled', 'automatic')",
    )
    op.create_check_constraint(
        "ck_agents_language_switching_consistency",
        "agents",
        "language_switching_enabled = (language_switching_mode = 'automatic')",
    )
    op.create_check_constraint(
        "ck_agents_language_switching_requires_languages",
        "agents",
        "NOT language_switching_enabled OR jsonb_array_length(supported_languages) > 1",
    )
    op.create_check_constraint(
        "ck_agents_primary_language_supported",
        "agents",
        "supported_languages ? language",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agents_primary_language_supported", "agents", type_="check")
    op.drop_constraint(
        "ck_agents_language_switching_requires_languages",
        "agents",
        type_="check",
    )
    op.drop_constraint(
        "ck_agents_language_switching_consistency",
        "agents",
        type_="check",
    )
    op.drop_constraint("ck_agents_language_switching_mode", "agents", type_="check")
    op.drop_column("agents", "language_switching_mode")
    op.drop_column("agents", "language_switching_enabled")
