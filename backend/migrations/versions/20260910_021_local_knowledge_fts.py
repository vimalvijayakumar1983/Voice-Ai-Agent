"""Local-only knowledge: align the full-text index with the retrieval query.

Revision ID: 20260910_021
Revises: 20260903_020

The realtime candidate query searches ``name || content || structured_content``
but the previous index covered only ``name || content``. PostgreSQL uses an
expression index only when the expression matches exactly, so every caller
turn recomputed ``to_tsvector`` over each source's full text. This migration
also records that knowledge bases are served by VAV itself rather than an
external provider.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_021"
down_revision: str | None = "20260903_020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_INDEX_NAME = "ix_knowledge_sources_name_content_fts"
_INDEX_NAME = "ix_knowledge_sources_search_fts"
# Must stay structurally identical to ``_postgres_candidate_source_ids`` in
# ``app/services/knowledge_retrieval.py``. Rendered by SQLAlchemy as:
#   to_tsvector('simple'::regconfig, coalesce(CAST(name AS TEXT), '') || ' '
#     || coalesce(content, '') || ' ' || coalesce(CAST(structured_content AS TEXT), ''))
_SEARCH_EXPRESSION = (
    "to_tsvector('simple'::regconfig, "
    "coalesce(name::text, '') || ' ' || coalesce(content, '') || ' ' || "
    "coalesce(structured_content::text, ''))"
)
_OLD_SEARCH_EXPRESSION = (
    "to_tsvector('simple'::regconfig, coalesce(name::text, '') || ' ' || coalesce(content, ''))"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text("UPDATE knowledge_bases SET provider = 'vav' WHERE provider <> 'vav'"))
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_OLD_INDEX_NAME}"))
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {_INDEX_NAME} ON knowledge_sources "
                f"USING gin ({_SEARCH_EXPRESSION})"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_OLD_INDEX_NAME}"))
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {_OLD_INDEX_NAME} ON knowledge_sources "
                f"USING gin ({_OLD_SEARCH_EXPRESSION})"
            )
        )
