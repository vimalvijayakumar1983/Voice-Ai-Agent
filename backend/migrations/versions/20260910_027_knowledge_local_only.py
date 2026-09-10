"""Record that every knowledge base is served by VAV's local pipeline.

Revision ID: 20260910_027
Revises: 20260910_026

Knowledge ingestion and retrieval no longer consult an external provider.
Existing rows still carry the historical ``smallest`` provider label and any
remote item identifiers recorded before the change.  The label is rewritten
so operators and readiness checks describe the live pipeline; remote
identifiers are cleared because nothing reads them any more and a stale value
must never look like a pending remote dependency.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_027"
down_revision: str | None = "20260910_026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE knowledge_bases SET provider = 'vav' WHERE provider <> 'vav'"))
    op.execute(
        sa.text(
            "UPDATE knowledge_bases SET provider_knowledge_base_id = NULL "
            "WHERE provider_knowledge_base_id IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE knowledge_sources SET provider_item_id = NULL "
            "WHERE provider_item_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    # The historical provider label cannot be reconstructed; leave rows as-is.
    pass
