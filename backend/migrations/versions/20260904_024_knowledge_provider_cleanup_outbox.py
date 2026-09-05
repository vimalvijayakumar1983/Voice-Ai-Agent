"""Add durable provider-artifact cleanup outbox.

Revision ID: 20260904_024
Revises: 20260904_023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_024"
down_revision: str | None = "20260904_023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "serving_revocation_generation",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_table(
        "knowledge_provider_cleanups",
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repair_run_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_knowledge_base_id", sa.String(length=100), nullable=False),
        sa.Column("provider_item_id", sa.String(length=100), nullable=False),
        sa.Column("provider_artifact_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_source_id"],
            ["knowledge_sources.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_knowledge_base_id",
            "provider_item_id",
            name="uq_knowledge_provider_cleanup_artifact",
        ),
    )
    op.create_index(
        "ix_knowledge_provider_cleanups_tenant_id",
        "knowledge_provider_cleanups",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_provider_cleanups_knowledge_base_id",
        "knowledge_provider_cleanups",
        ["knowledge_base_id"],
    )
    op.create_index(
        "ix_knowledge_provider_cleanups_knowledge_source_id",
        "knowledge_provider_cleanups",
        ["knowledge_source_id"],
    )
    op.create_index(
        "ix_knowledge_provider_cleanups_status",
        "knowledge_provider_cleanups",
        ["status"],
    )
    op.create_index(
        "ix_knowledge_provider_cleanups_ready",
        "knowledge_provider_cleanups",
        ["status", "available_at"],
    )

    # Earlier builds kept superseded item IDs only in source JSON. Promote any
    # such outstanding work into the durable outbox before the new guards begin
    # relying on it.
    op.execute(
        sa.text(
            """
            INSERT INTO knowledge_provider_cleanups (
              id,
              tenant_id,
              knowledge_base_id,
              knowledge_source_id,
              repair_run_id,
              provider,
              provider_knowledge_base_id,
              provider_item_id,
              provider_artifact_name,
              status,
              attempts,
              available_at,
              lease_expires_at,
              last_error
            )
            SELECT DISTINCT ON (
              ks.tenant_id,
              COALESCE(NULLIF(kb.provider, ''), 'smallest'),
              kb.provider_knowledge_base_id,
              pending.provider_item_id
            )
              md5(
                ks.tenant_id::text || ':' ||
                COALESCE(NULLIF(kb.provider, ''), 'smallest') || ':' ||
                kb.provider_knowledge_base_id || ':' ||
                pending.provider_item_id
              )::uuid,
              ks.tenant_id,
              ks.knowledge_base_id,
              ks.id,
              ks.metadata ->> 'repair_run_id',
              COALESCE(NULLIF(kb.provider, ''), 'smallest'),
              kb.provider_knowledge_base_id,
              pending.provider_item_id,
              NULL,
              'pending',
              0,
              now(),
              NULL,
              NULL
            FROM knowledge_sources AS ks
            JOIN knowledge_bases AS kb ON kb.id = ks.knowledge_base_id
            CROSS JOIN LATERAL jsonb_array_elements_text(
              CASE
                WHEN jsonb_typeof(ks.metadata -> 'provider_cleanup_pending_ids') = 'array'
                THEN ks.metadata -> 'provider_cleanup_pending_ids'
                ELSE '[]'::jsonb
              END
            ) AS pending(provider_item_id)
            WHERE kb.provider_knowledge_base_id IS NOT NULL
              AND pending.provider_item_id <> ''
            ORDER BY
              ks.tenant_id,
              COALESCE(NULLIF(kb.provider, ''), 'smallest'),
              kb.provider_knowledge_base_id,
              pending.provider_item_id,
              ks.id
            ON CONFLICT ON CONSTRAINT uq_knowledge_provider_cleanup_artifact DO NOTHING
            """
        )
    )


def downgrade() -> None:
    # The check and destructive drop must share one PostgreSQL transaction and
    # one writer-excluding lock. Without this lock, an API/worker could insert
    # a new cleanup intent after the empty check and immediately before the
    # table is dropped. Operators must still stop producers and drain queues as
    # documented; the lock closes the final database race.
    op.execute(sa.text("LOCK TABLE knowledge_provider_cleanups IN ACCESS EXCLUSIVE MODE"))
    if op.get_context().as_sql:
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM knowledge_provider_cleanups
                        WHERE status <> 'completed'
                    ) THEN
                        RAISE EXCEPTION
                            'Finish pending knowledge provider cleanup work before downgrade';
                    END IF;
                END $$
                """
            )
        )
    else:
        pending = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM knowledge_provider_cleanups WHERE status <> 'completed' LIMIT 1"
                )
            )
            .scalar()
        )
        if pending is not None:
            raise RuntimeError("Finish pending knowledge provider cleanup work before downgrade")
    op.drop_index(
        "ix_knowledge_provider_cleanups_ready",
        table_name="knowledge_provider_cleanups",
    )
    op.drop_index(
        "ix_knowledge_provider_cleanups_status",
        table_name="knowledge_provider_cleanups",
    )
    op.drop_index(
        "ix_knowledge_provider_cleanups_knowledge_source_id",
        table_name="knowledge_provider_cleanups",
    )
    op.drop_index(
        "ix_knowledge_provider_cleanups_knowledge_base_id",
        table_name="knowledge_provider_cleanups",
    )
    op.drop_index(
        "ix_knowledge_provider_cleanups_tenant_id",
        table_name="knowledge_provider_cleanups",
    )
    op.drop_table("knowledge_provider_cleanups")
    op.drop_column("knowledge_bases", "serving_revocation_generation")
