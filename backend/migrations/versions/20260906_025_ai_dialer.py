"""Add tenant-scoped customer workspace and durable AI dialing queue.

Revision ID: 20260906_025
Revises: 20260904_024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "20260906_025"
down_revision = "20260904_024"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
    ]


def upgrade():
    op.create_table(
        "dialer_customers",
        *common(),
        sa.Column("company", sa.String(160), nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(160)),
        sa.Column("language", sa.String(30), nullable=False),
        sa.Column("timezone", sa.String(60), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("contact_allowed", sa.Boolean(), nullable=False),
        sa.Column("consent_reference", sa.String(500), nullable=False),
        sa.Column("opted_out", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "company", "phone_number"),
    )
    op.create_table(
        "dialer_campaigns",
        *common(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("company", sa.String(160), nullable=False),
        sa.Column("agent_id", pg.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("owner_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=False),
    )
    op.create_table(
        "dialer_jobs",
        *common(),
        sa.Column(
            "campaign_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dialer_campaigns.id"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dialer_customers.id"),
            nullable=False,
        ),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("call_id", pg.UUID(as_uuid=True)),
        sa.Column("call_ids", pg.JSONB(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(80)),
        sa.Column("error", sa.String(250)),
        sa.UniqueConstraint("campaign_id", "event_key"),
    )
    for table, columns in {
        "dialer_customers": ["tenant_id"],
        "dialer_campaigns": ["tenant_id", "status"],
        "dialer_jobs": [
            "tenant_id",
            "campaign_id",
            "customer_id",
            "state",
            "available_at",
            "call_id",
        ],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.drop_table("dialer_jobs")
    op.drop_table("dialer_campaigns")
    op.drop_table("dialer_customers")
