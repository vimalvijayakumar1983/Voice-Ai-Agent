"""Durable, rotating refresh-token session lifecycle."""

import asyncio
import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_refresh_token, decode_token
from app.models.user import RefreshSession, User
from app.services.audit import record_audit_event

REFRESH_REUSE_GRACE = timedelta(seconds=5)
_SQLITE_REFRESH_LOCK = asyncio.Lock()


class RefreshSessionRejectedError(Exception):
    """A refresh token was rejected, optionally after durable security work."""

    def __init__(
        self,
        detail: str,
        *,
        persist_changes: bool = False,
        status_code: int = 401,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.persist_changes = persist_changes
        self.status_code = status_code


def hash_refresh_token(token: str) -> str:
    """Return the non-reversible digest stored for a refresh token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def issue_refresh_session(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    family_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    rotated_from_id: uuid.UUID | None = None,
) -> str:
    """Persist a refresh session and return its one-time bearer token."""

    session_id = uuid.uuid4()
    resolved_family_id = family_id or uuid.uuid4()
    resolved_expiry = expires_at or (
        datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    )
    token = create_refresh_token(
        user_id,
        tenant_id,
        jti=session_id,
        family_id=resolved_family_id,
        expires_at=resolved_expiry,
    )
    db.add(
        RefreshSession(
            id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            family_id=resolved_family_id,
            token_hash=hash_refresh_token(token),
            rotated_from_id=rotated_from_id,
            expires_at=resolved_expiry,
        )
    )
    await db.flush()
    return token


def _parse_refresh_claims(payload: dict | None) -> tuple[uuid.UUID, ...] | None:
    if not payload or payload.get("type") != "refresh":
        return None
    try:
        return (
            uuid.UUID(str(payload["jti"])),
            uuid.UUID(str(payload["family_id"])),
            uuid.UUID(str(payload["sub"])),
            uuid.UUID(str(payload["tenant_id"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _revoke_family(
    db: AsyncSession,
    *,
    session: RefreshSession,
    reason: str,
    now: datetime,
) -> int:
    result = await db.execute(
        update(RefreshSession)
        .where(
            RefreshSession.tenant_id == session.tenant_id,
            RefreshSession.user_id == session.user_id,
            RefreshSession.family_id == session.family_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoke_reason=reason)
    )
    return int(result.rowcount or 0)


async def _rotate_refresh_session(
    db: AsyncSession,
    token: str,
) -> tuple[User, str]:
    """Consume one refresh token and return its user plus rotated successor.

    Reusing a token that was already rotated is treated as a credential theft
    signal and revokes every still-active token in that login family.
    """

    claims = _parse_refresh_claims(decode_token(token))
    if claims is None:
        raise RefreshSessionRejectedError("Invalid refresh token")
    session_id, family_id, user_id, tenant_id = claims

    result = await db.execute(
        select(RefreshSession).where(RefreshSession.id == session_id).with_for_update()
    )
    session = result.scalar_one_or_none()
    if (
        session is None
        or session.family_id != family_id
        or session.user_id != user_id
        or session.tenant_id != tenant_id
        or not hmac.compare_digest(session.token_hash, hash_refresh_token(token))
    ):
        raise RefreshSessionRejectedError("Invalid refresh token")

    now = datetime.now(UTC)
    if session.revoked_at is not None:
        if session.revoke_reason == "rotated":
            # A second tab can arrive immediately after a legitimate rotation.
            # The successor is never stored in plaintext, so it cannot be
            # replayed to that request. Give the browser's cross-tab mutex time
            # to observe the replacement without revoking the successful tab.
            # A later replay remains a theft signal and revokes the family.
            if now - _as_utc(session.revoked_at) <= REFRESH_REUSE_GRACE:
                raise RefreshSessionRejectedError(
                    "Refresh token was already rotated; retry with the current session",
                    status_code=409,
                )
            revoked_count = await _revoke_family(
                db,
                session=session,
                reason="reuse_detected",
                now=now,
            )
            await record_audit_event(
                db,
                tenant_id=session.tenant_id,
                actor_user_id=session.user_id,
                action="session.reuse_detected",
                resource_type="refresh_session",
                resource_id=str(session.id),
                details={"revoked_sessions": revoked_count},
            )
            await db.commit()
            raise RefreshSessionRejectedError(
                "Refresh token reuse detected; this session has been revoked",
                persist_changes=True,
            )
        raise RefreshSessionRejectedError("Invalid refresh token")

    if _as_utc(session.expires_at) <= now:
        session.revoked_at = now
        session.revoke_reason = "expired"
        await db.commit()
        raise RefreshSessionRejectedError("Invalid refresh token", persist_changes=True)

    user_result = await db.execute(
        select(User).where(User.id == session.user_id, User.tenant_id == session.tenant_id)
    )
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        revoked_count = await _revoke_family(
            db,
            session=session,
            reason="account_inactive",
            now=now,
        )
        if user is not None:
            await record_audit_event(
                db,
                tenant_id=session.tenant_id,
                actor_user_id=session.user_id,
                action="session.revoked",
                resource_type="refresh_session",
                resource_id=str(session.id),
                details={"reason": "account_inactive", "revoked_sessions": revoked_count},
            )
        await db.commit()
        raise RefreshSessionRejectedError("User not found", persist_changes=True)

    replacement = await issue_refresh_session(
        db,
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        family_id=session.family_id,
        expires_at=_as_utc(session.expires_at),
        rotated_from_id=session.id,
    )
    session.last_used_at = now
    session.revoked_at = now
    session.revoke_reason = "rotated"
    await db.flush()
    # Commit the consumed token and its successor before the bearer response can
    # leave the server. This also releases the PostgreSQL row lock atomically.
    await db.commit()
    return user, replacement


async def rotate_refresh_session(
    db: AsyncSession,
    token: str,
) -> tuple[User, str]:
    """Serialize SQLite's lock-free test/dev path; PostgreSQL uses FOR UPDATE."""

    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        async with _SQLITE_REFRESH_LOCK:
            return await _rotate_refresh_session(db, token)
    return await _rotate_refresh_session(db, token)


async def revoke_refresh_token(db: AsyncSession, token: str) -> int:
    """Revoke the login family for a refresh token without disclosing validity."""

    result = await db.execute(
        select(RefreshSession)
        .where(RefreshSession.token_hash == hash_refresh_token(token))
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if session is None:
        return 0

    now = datetime.now(UTC)
    revoked_count = await _revoke_family(
        db,
        session=session,
        reason="logout",
        now=now,
    )
    if revoked_count:
        await record_audit_event(
            db,
            tenant_id=session.tenant_id,
            actor_user_id=session.user_id,
            action="session.logged_out",
            resource_type="refresh_session",
            resource_id=str(session.id),
            details={"revoked_sessions": revoked_count},
        )
    return revoked_count


async def revoke_user_sessions(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    reason: str,
) -> int:
    """Revoke every active refresh session for one tenant-scoped user."""

    result = await db.execute(
        update(RefreshSession)
        .where(
            RefreshSession.user_id == user_id,
            RefreshSession.tenant_id == tenant_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC), revoke_reason=reason)
    )
    return int(result.rowcount or 0)
