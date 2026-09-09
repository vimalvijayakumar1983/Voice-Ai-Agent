"""Explicit single-company ownership for knowledge sources.

Revision ID: 20260909_025
Revises: 20260904_024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260909_025"
down_revision = "20260904_024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_bases", sa.Column("owner_company", sa.String(160), nullable=True))
    op.add_column("knowledge_bases", sa.Column("readiness_report", JSONB(), nullable=True))


def downgrade():
    op.drop_column("knowledge_bases", "readiness_report")
    op.drop_column("knowledge_bases", "owner_company")
