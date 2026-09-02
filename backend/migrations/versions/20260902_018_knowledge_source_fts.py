"""Index knowledge-source text for bounded realtime retrieval.

Revision ID: 20260902_018
Revises: 20260831_017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_018"
down_revision: str | None = "20260831_017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_knowledge_sources_name_content_fts"
_SEARCH_EXPRESSION = sa.text(
    "to_tsvector('simple'::regconfig, coalesce(name::text, '') || ' ' || coalesce(content, ''))"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        # If a prior concurrent build was interrupted, PostgreSQL can retain an
        # invalid index. Drop it before retrying so Alembic recovery is deterministic.
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {_INDEX_NAME} ON knowledge_sources "
                f"USING gin ({_SEARCH_EXPRESSION.text})"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
