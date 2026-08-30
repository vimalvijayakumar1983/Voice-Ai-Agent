"""Mark approved Sarvam knowledge bindings as live VAV retrieval.

Revision ID: 20260830_015
Revises: 20260830_014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_015"
down_revision: str | None = "20260830_014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agent_knowledge_bindings AS binding
            SET provider = 'sarvam',
                sync_status = 'synced',
                last_synced_at = now()
            FROM agents AS agent, knowledge_bases AS knowledge
            WHERE binding.agent_id = agent.id
              AND binding.knowledge_base_id = knowledge.id
              AND binding.tenant_id = agent.tenant_id
              AND binding.tenant_id = knowledge.tenant_id
              AND agent.voice_provider = 'sarvam'
              AND knowledge.is_active IS TRUE
              AND knowledge.approval_status = 'approved'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agent_knowledge_bindings AS binding
            SET sync_status = 'pending',
                last_synced_at = NULL
            FROM agents AS agent
            WHERE binding.agent_id = agent.id
              AND binding.tenant_id = agent.tenant_id
              AND agent.voice_provider = 'sarvam'
              AND binding.provider = 'sarvam'
            """
        )
    )
