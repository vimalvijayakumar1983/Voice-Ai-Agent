"""Provider-neutral VAV realtime runtime profiles.

Revision ID: 20260830_014
Revises: 20260829_013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_014"
down_revision: str | None = "20260829_013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runtime_profiles",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("telephony_provider", sa.String(30), nullable=False, server_default="twilio"),
        sa.Column(
            "primary_speech_provider", sa.String(30), nullable=False, server_default="sarvam"
        ),
        sa.Column("fallback_speech_provider", sa.String(30), nullable=True),
        sa.Column("llm_provider", sa.String(30), nullable=False, server_default="openai"),
        sa.Column("llm_model", sa.String(100), nullable=False, server_default="gpt-4o-mini"),
        sa.Column("stt_language", sa.String(30), nullable=False, server_default="auto"),
        sa.Column("max_concurrent_calls", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("daily_call_limit", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("monthly_budget_cents", sa.Integer(), nullable=False, server_default="5000"),
        sa.Column(
            "assigned_numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_agent_runtime_profiles_agent_id"),
    )
    op.create_index(
        "ix_agent_runtime_profiles_tenant_id", "agent_runtime_profiles", ["tenant_id"]
    )
    op.create_index("ix_agent_runtime_profiles_agent_id", "agent_runtime_profiles", ["agent_id"])


def downgrade() -> None:
    op.drop_table("agent_runtime_profiles")
