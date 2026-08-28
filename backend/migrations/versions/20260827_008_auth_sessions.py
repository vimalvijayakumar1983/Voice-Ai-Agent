"""Canonicalize user emails and add rotating refresh sessions.

Revision ID: 20260827_008
Revises: 20260827_007
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_008"
down_revision: str | None = "20260827_007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _preflight_email_normalization() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Block old application writes until normalization and the functional
        # UNIQUE index are committed, closing the preflight/update race.
        op.execute(sa.text("LOCK TABLE users, user_invitations IN SHARE ROW EXCLUSIVE MODE"))
        op.execute(
            sa.text(
                """
                DO $$
                DECLARE
                    conflicting_emails text;
                BEGIN
                    SELECT string_agg(canonical_email, ', ' ORDER BY canonical_email)
                    INTO conflicting_emails
                    FROM (
                        SELECT lower(btrim(email)) AS canonical_email
                        FROM users
                        GROUP BY lower(btrim(email))
                        HAVING count(*) > 1
                        ORDER BY lower(btrim(email))
                        LIMIT 10
                    ) AS conflicts;

                    IF conflicting_emails IS NOT NULL THEN
                        RAISE EXCEPTION USING MESSAGE =
                            'Canonical email duplicates must be reconciled before '
                            || 'upgrade. Conflicts (up to 10): '
                            || conflicting_emails;
                    END IF;

                    IF EXISTS (SELECT 1 FROM users WHERE btrim(email) = '') THEN
                        RAISE EXCEPTION
                            'Blank user emails must be corrected before upgrade';
                    END IF;

                    SELECT string_agg(canonical_email, ', ' ORDER BY canonical_email)
                    INTO conflicting_emails
                    FROM (
                        SELECT lower(btrim(email)) AS canonical_email
                        FROM user_invitations
                        WHERE accepted_at IS NULL
                          AND revoked_at IS NULL
                          AND expires_at > now()
                        GROUP BY lower(btrim(email))
                        HAVING count(*) > 1
                        ORDER BY lower(btrim(email))
                        LIMIT 10
                    ) AS invitation_conflicts;

                    IF conflicting_emails IS NOT NULL THEN
                        RAISE EXCEPTION USING MESSAGE =
                            'Concurrent active invitations for the same canonical '
                            || 'email must be reconciled before upgrade. Conflicts '
                            || '(up to 10): '
                            || conflicting_emails;
                    END IF;
                END $$
                """
            )
        )
        return

    users = sa.table("users", sa.column("email", sa.String(length=255)))
    canonical = sa.func.lower(sa.func.trim(users.c.email))
    duplicates = (
        bind.execute(
            sa.select(canonical.label("canonical_email"))
            .group_by(canonical)
            .having(sa.func.count() > 1)
            .order_by(canonical)
            .limit(10)
        )
        .scalars()
        .all()
    )
    if duplicates:
        conflict_list = ", ".join(str(value) for value in duplicates)
        raise RuntimeError(
            "Canonical email duplicates must be reconciled before upgrade. "
            f"Conflicts (up to 10): {conflict_list}"
        )
    if bind.execute(sa.select(users.c.email).where(sa.func.trim(users.c.email) == "")).first():
        raise RuntimeError("Blank user emails must be corrected before upgrade")

    invitations = sa.table(
        "user_invitations",
        sa.column("email", sa.String(length=255)),
        sa.column("accepted_at", sa.DateTime(timezone=True)),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
        sa.column("expires_at", sa.DateTime(timezone=True)),
    )
    invitation_email = sa.func.lower(sa.func.trim(invitations.c.email))
    invitation_duplicates = (
        bind.execute(
            sa.select(invitation_email.label("canonical_email"))
            .where(
                invitations.c.accepted_at.is_(None),
                invitations.c.revoked_at.is_(None),
                invitations.c.expires_at > sa.func.current_timestamp(),
            )
            .group_by(invitation_email)
            .having(sa.func.count() > 1)
            .order_by(invitation_email)
            .limit(10)
        )
        .scalars()
        .all()
    )
    if invitation_duplicates:
        conflict_list = ", ".join(str(value) for value in invitation_duplicates)
        raise RuntimeError(
            "Concurrent active invitations for the same canonical email must be "
            "reconciled before upgrade. "
            f"Conflicts (up to 10): {conflict_list}"
        )


def upgrade() -> None:
    _preflight_email_normalization()
    op.add_column(
        "user_invitations",
        sa.Column("resolution_reason", sa.String(length=32), nullable=True),
    )
    op.execute(sa.text("UPDATE users SET email = lower(trim(email))"))
    op.execute(
        sa.text(
            "UPDATE user_invitations SET revoked_at = expires_at, "
            "resolution_reason = 'expired' "
            "WHERE accepted_at IS NULL AND revoked_at IS NULL "
            "AND expires_at <= CURRENT_TIMESTAMP"
        )
    )
    op.execute(sa.text("UPDATE user_invitations SET email = lower(trim(email))"))
    op.create_index(
        "uq_users_email_canonical",
        "users",
        [sa.text("lower(trim(email))")],
        unique=True,
    )
    op.create_index(
        "uq_user_invitations_pending_email",
        "user_invitations",
        [sa.text("lower(trim(email))")],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
        sqlite_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )

    op.create_table(
        "refresh_sessions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("rotated_from_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["rotated_from_id"],
            ["refresh_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refresh_sessions_tenant_id", "refresh_sessions", ["tenant_id"])
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_index("ix_refresh_sessions_family_id", "refresh_sessions", ["family_id"])
    op.create_index(
        "ix_refresh_sessions_token_hash",
        "refresh_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_refresh_sessions_rotated_from_id",
        "refresh_sessions",
        ["rotated_from_id"],
        unique=True,
    )
    op.create_index("ix_refresh_sessions_expires_at", "refresh_sessions", ["expires_at"])
    op.create_index("ix_refresh_sessions_revoked_at", "refresh_sessions", ["revoked_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM refresh_sessions) THEN
                        RAISE EXCEPTION USING MESSAGE =
                            'Cannot downgrade while refresh_sessions contains rows. '
                            || 'Stop auth traffic and globally invalidate the signed JWTs '
                            || '(rotate SECRET_KEY or wait through maximum token expiry) '
                            || 'before explicitly clearing sessions and retrying.';
                    END IF;
                END $$
                """
            )
        )
    else:
        sessions = sa.table("refresh_sessions", sa.column("id", sa.UUID()))
        if bind.execute(sa.select(sessions.c.id).limit(1)).first():
            raise RuntimeError(
                "Cannot downgrade while refresh_sessions contains rows. Stop auth "
                "traffic and globally invalidate the signed JWTs (rotate SECRET_KEY "
                "or wait through maximum token expiry) before explicitly clearing "
                "sessions and retrying."
            )
    op.drop_table("refresh_sessions")
    op.drop_index("uq_user_invitations_pending_email", table_name="user_invitations")
    op.drop_index("uq_users_email_canonical", table_name="users")
    op.execute(
        sa.text("UPDATE user_invitations SET revoked_at = NULL WHERE resolution_reason = 'expired'")
    )
    op.drop_column("user_invitations", "resolution_reason")
    # Lowercasing is intentionally retained: reversing it is impossible without
    # inventing original identity data, and the legacy exact UNIQUE index still
    # protects the downgraded schema.
