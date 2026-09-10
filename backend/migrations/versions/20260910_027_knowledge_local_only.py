"""Record that every knowledge base is served by VAV's local pipeline.

Revision ID: 20260910_027
Revises: 20260910_026

Knowledge ingestion and retrieval no longer consult an external provider.
This revision brings persisted rows in line with that contract:

* every knowledge base is labelled ``vav``;
* a knowledge base left in the retired ``provisioning`` state gets the local
  status its sources justify, so the API keeps serving it;
* a binding to an agent that does not run on a VAV-native voice runtime is
  moved to ``pending`` because such an agent never reads the local index;
* unfinished remote-cleanup intents are cancelled with a recorded reason,
  because no worker processes them any more.

Remote identifiers (``provider_knowledge_base_id`` and ``provider_item_id``)
are deliberately retained: nothing reads them, but they are the only handle an
operator has for decommissioning the remote collections explicitly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_027"
down_revision: str | None = "20260910_026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLEANUP_RETIRED_REASON = (
    "Cancelled by migration 20260910_027: VAV serves knowledge locally and no longer "
    "runs remote artifact cleanup. Decommission the remote collection manually."
)


def upgrade_statements() -> list[sa.sql.elements.TextClause]:
    """The data statements, in order; shared with the migration's tests."""
    return [
        sa.text("UPDATE knowledge_bases SET provider = 'vav' WHERE provider <> 'vav'"),
        # ``provisioning`` was only ever a remote-provider state. Map it to the
        # local status the knowledge base's sources justify.
        sa.text(
            "UPDATE knowledge_bases SET sync_status = 'error' "
            "WHERE sync_status = 'provisioning' AND EXISTS ("
            "SELECT 1 FROM knowledge_sources s "
            "WHERE s.knowledge_base_id = knowledge_bases.id AND s.status = 'failed')"
        ),
        sa.text(
            "UPDATE knowledge_bases SET sync_status = 'processing' "
            "WHERE sync_status = 'provisioning' AND EXISTS ("
            "SELECT 1 FROM knowledge_sources s "
            "WHERE s.knowledge_base_id = knowledge_bases.id "
            "AND s.status IN ('pending', 'processing'))"
        ),
        sa.text(
            "UPDATE knowledge_bases SET sync_status = 'ready' "
            "WHERE sync_status = 'provisioning' AND EXISTS ("
            "SELECT 1 FROM knowledge_sources s WHERE s.knowledge_base_id = knowledge_bases.id)"
        ),
        sa.text(
            "UPDATE knowledge_bases SET sync_status = 'local_only' "
            "WHERE sync_status = 'provisioning'"
        ),
        # A binding to an agent on another voice runtime can never be live
        # again: that runtime does not read VAV's local index.
        sa.text(
            "UPDATE agent_knowledge_bindings SET sync_status = 'pending', last_synced_at = NULL "
            "WHERE agent_id IN (SELECT id FROM agents WHERE voice_provider NOT IN "
            "('sarvam', 'elevenlabs', 'inworld'))"
        ),
        sa.text(
            "UPDATE knowledge_provider_cleanups SET status = 'cancelled', last_error = :reason "
            "WHERE status NOT IN ('completed', 'cancelled')"
        ).bindparams(reason=_CLEANUP_RETIRED_REASON),
    ]


def upgrade() -> None:
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade() -> None:
    # The historical provider label, statuses and cleanup intents cannot be
    # reconstructed; leave rows as they are.
    pass
