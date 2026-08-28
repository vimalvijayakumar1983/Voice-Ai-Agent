"""Add authenticated encryption envelopes for integration configuration.

Revision ID: 20260827_005
Revises: 20260827_004
Create Date: 2026-08-27

The migration deliberately does not transform existing JSONB values because
Alembic must not receive application encryption keys. Existing rows remain
readable through a bounded legacy path and must be processed immediately after
deployment by the idempotent application backfill (they also self-migrate on
their first control-plane mutation).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_005"
down_revision: str | None = "20260827_004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column("encrypted_config", sa.Text(), nullable=True),
    )
    op.add_column(
        "integrations",
        sa.Column("config_encryption_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        # Preserve the same fail-safe in generated PostgreSQL migration plans;
        # an offline Python result proxy cannot inspect the row count.
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM integrations WHERE encrypted_config IS NOT NULL
                    ) THEN
                        RAISE EXCEPTION
                            'Refusing to discard encrypted integration configs';
                    ELSE
                        ALTER TABLE integrations DROP COLUMN config_encryption_version;
                        ALTER TABLE integrations DROP COLUMN encrypted_config;
                    END IF;
                END $$
                """
            )
        )
        return

    encrypted_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM integrations WHERE encrypted_config IS NOT NULL"))
        .scalar_one()
    )
    if encrypted_rows:
        raise RuntimeError(
            "Refusing to drop encrypted integration configs; rewrap them into a "
            "downgrade-safe export before removing this column"
        )
    op.drop_column("integrations", "config_encryption_version")
    op.drop_column("integrations", "encrypted_config")
