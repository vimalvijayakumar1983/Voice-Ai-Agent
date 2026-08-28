"""Add durable campaign contact dispatch attempts.

Revision ID: 20260827_006
Revises: 20260827_005
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_006"
down_revision: str | None = "20260827_005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Block old API contact inserts for the whole transactional migration. The
    # lock is acquired before duplicate detection/canonicalization, so a
    # formatting variant cannot race in between the data rewrite and UNIQUE.
    op.execute(sa.text("LOCK TABLE campaign_contacts IN SHARE ROW EXCLUSIVE MODE"))

    # Canonicalize legacy formatting with the same rules as normalize_e164.
    # Formatting-equivalent duplicates are rejected before any update so call
    # and outcome history is never silently merged or discarded.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    WITH stripped AS (
                        SELECT
                            campaign_id,
                            CASE
                                WHEN left(
                                    regexp_replace(
                                        btrim(phone_number),
                                        '[[:space:]().-]',
                                        '',
                                        'g'
                                    ),
                                    2
                                ) = '00'
                                THEN '+' || substr(
                                    regexp_replace(
                                        btrim(phone_number),
                                        '[[:space:]().-]',
                                        '',
                                        'g'
                                    ),
                                    3
                                )
                                ELSE regexp_replace(
                                    btrim(phone_number),
                                    '[[:space:]().-]',
                                    '',
                                    'g'
                                )
                            END AS canonical_number
                        FROM campaign_contacts
                    )
                    SELECT 1
                    FROM stripped
                    WHERE canonical_number ~ '^\\+[1-9][0-9]{7,14}$'
                    GROUP BY campaign_id, canonical_number
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'Formatting-equivalent campaign contacts must be reconciled before upgrade';
                END IF;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH normalized AS (
                SELECT
                    id,
                    CASE
                        WHEN left(
                            regexp_replace(
                                btrim(phone_number),
                                '[[:space:]().-]',
                                '',
                                'g'
                            ),
                            2
                        ) = '00'
                        THEN '+' || substr(
                            regexp_replace(
                                btrim(phone_number),
                                '[[:space:]().-]',
                                '',
                                'g'
                            ),
                            3
                        )
                        ELSE regexp_replace(
                            btrim(phone_number),
                            '[[:space:]().-]',
                            '',
                            'g'
                        )
                    END AS canonical_number
                FROM campaign_contacts
            )
            UPDATE campaign_contacts AS contact
            SET phone_number = normalized.canonical_number
            FROM normalized
            WHERE contact.id = normalized.id
              AND normalized.canonical_number ~ '^\\+[1-9][0-9]{7,14}$'
              AND contact.phone_number <> normalized.canonical_number
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM campaign_contacts
                    GROUP BY campaign_id, phone_number
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'Duplicate campaign contacts must be reconciled before upgrade';
                END IF;
            END $$
            """
        )
    )
    op.create_unique_constraint(
        "uq_campaign_contact_phone_number",
        "campaign_contacts",
        ["campaign_id", "phone_number"],
    )
    op.create_table(
        "campaign_contact_attempts",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_call_sid", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["campaign_contacts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["calls.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "contact_id",
            "attempt_number",
            name="uq_campaign_contact_attempt_number",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_campaign_attempt_idempotency_key",
        ),
        sa.UniqueConstraint("call_id", name="uq_campaign_attempt_call_id"),
    )
    op.create_index(
        op.f("ix_campaign_contact_attempts_tenant_id"),
        "campaign_contact_attempts",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_campaign_contact_attempts_campaign_id"),
        "campaign_contact_attempts",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_campaign_contact_attempts_contact_id"),
        "campaign_contact_attempts",
        ["contact_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_campaign_contact_attempts_provider_call_sid"),
        "campaign_contact_attempts",
        ["provider_call_sid"],
        unique=False,
    )
    op.create_index(
        op.f("ix_campaign_contact_attempts_state"),
        "campaign_contact_attempts",
        ["state"],
        unique=False,
    )
    op.create_table(
        "provider_callback_outbox",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_key",
            name="uq_provider_callback_outbox_event_key",
        ),
    )
    op.create_index(
        op.f("ix_provider_callback_outbox_tenant_id"),
        "provider_callback_outbox",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_callback_outbox_call_id"),
        "provider_callback_outbox",
        ["call_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_callback_outbox_campaign_id"),
        "provider_callback_outbox",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_callback_outbox_status"),
        "provider_callback_outbox",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM campaign_contact_attempts
                        WHERE state IN ('claimed', 'dispatching', 'accepted', 'unknown')
                    ) THEN
                        RAISE EXCEPTION
                            'Pause and reconcile active campaign attempts before downgrade';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM provider_callback_outbox
                        WHERE status <> 'dispatched'
                    ) THEN
                        RAISE EXCEPTION
                            'Dispatch pending provider callback outbox rows before downgrade';
                    END IF;
                END $$
                """
            )
        )
    else:
        active_attempts = (
            op.get_bind()
            .execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM campaign_contact_attempts
                    WHERE state IN ('claimed', 'dispatching', 'accepted', 'unknown')
                    """
                )
            )
            .scalar_one()
        )
        if active_attempts:
            raise RuntimeError(
                "Pause and reconcile active campaign attempts before removing the "
                "idempotency ledger"
            )
        pending_outbox = (
            op.get_bind()
            .execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM provider_callback_outbox
                    WHERE status <> 'dispatched'
                    """
                )
            )
            .scalar_one()
        )
        if pending_outbox:
            raise RuntimeError("Dispatch pending provider callback outbox rows before downgrade")

    op.drop_index(
        op.f("ix_provider_callback_outbox_status"),
        table_name="provider_callback_outbox",
    )
    op.drop_index(
        op.f("ix_provider_callback_outbox_campaign_id"),
        table_name="provider_callback_outbox",
    )
    op.drop_index(
        op.f("ix_provider_callback_outbox_call_id"),
        table_name="provider_callback_outbox",
    )
    op.drop_index(
        op.f("ix_provider_callback_outbox_tenant_id"),
        table_name="provider_callback_outbox",
    )
    op.drop_table("provider_callback_outbox")
    op.drop_index(
        op.f("ix_campaign_contact_attempts_state"),
        table_name="campaign_contact_attempts",
    )
    op.drop_index(
        op.f("ix_campaign_contact_attempts_provider_call_sid"),
        table_name="campaign_contact_attempts",
    )
    op.drop_index(
        op.f("ix_campaign_contact_attempts_contact_id"),
        table_name="campaign_contact_attempts",
    )
    op.drop_index(
        op.f("ix_campaign_contact_attempts_campaign_id"),
        table_name="campaign_contact_attempts",
    )
    op.drop_index(
        op.f("ix_campaign_contact_attempts_tenant_id"),
        table_name="campaign_contact_attempts",
    )
    op.drop_table("campaign_contact_attempts")
    op.drop_constraint(
        "uq_campaign_contact_phone_number",
        "campaign_contacts",
        type_="unique",
    )
