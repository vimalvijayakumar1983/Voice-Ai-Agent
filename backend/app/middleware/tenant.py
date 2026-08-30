"""Tenant context middleware and dependencies."""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token, parse_api_key_id, verify_api_key
from app.models.user import ApiKey, User

# Context var for current tenant - accessible anywhere in the request
_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("current_tenant_id", default=None)
_current_user_id: ContextVar[uuid.UUID | None] = ContextVar("current_user_id", default=None)

security = HTTPBearer()


@dataclass
class CurrentUser:
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    full_name: str


def get_current_tenant_id() -> uuid.UUID | None:
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: uuid.UUID) -> None:
    _current_tenant_id.set(tenant_id)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    token = credentials.credentials

    if token.startswith("vai_"):
        return await _get_api_key_user(token, db)

    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        user_id = uuid.UUID(payload["sub"])
        tenant_id = uuid.UUID(payload["tenant_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    result = await db.execute(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )

    return _as_current_user(user)


def _as_current_user(user: User, *, role: str | None = None) -> CurrentUser:
    """Apply request context and expose the authenticated user's current role."""
    set_current_tenant_id(user.tenant_id)
    _current_user_id.set(user.id)
    return CurrentUser(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        role=role or user.role,
        full_name=user.full_name,
    )


async def _get_api_key_user(api_key_value: str, db: AsyncSession) -> CurrentUser:
    """Authenticate an API key by its embedded primary key and stored digest."""
    api_key_id = parse_api_key_id(api_key_value)
    if api_key_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    result = await db.execute(
        select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.id == api_key_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    api_key, user = row
    now = datetime.now(UTC)
    expires_at = api_key.expires_at
    if expires_at is None:
        # Legacy keys receive the same bounded lifetime as newly issued keys.
        # This prevents a pre-migration null from becoming an indefinite bearer
        # credential while allowing a controlled rotate/revoke window.
        expires_at = api_key.created_at + timedelta(days=90)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    digest_matches = verify_api_key(api_key_value, api_key.key_hash)

    if (
        not api_key.is_active
        or not user.is_active
        or api_key.tenant_id != user.tenant_id
        or expires_at <= now
        or not digest_matches
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    api_key.last_used_at = now
    # Scoped service accounts are staged roadmap work. Until then API keys are
    # deliberately read-only, regardless of the creator's live human role.
    return _as_current_user(user, role="viewer")


def require_role(*roles: str):
    """Dependency factory for role-based access control."""

    async def _check_role(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' not authorized. Required: {roles}",
            )
        return current_user

    return _check_role
