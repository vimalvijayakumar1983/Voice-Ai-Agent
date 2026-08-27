"""Tenant context middleware and dependencies."""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User

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
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = uuid.UUID(payload["sub"])
    tenant_id = uuid.UUID(payload["tenant_id"])

    result = await db.execute(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )

    # Set tenant context for the request
    set_current_tenant_id(tenant_id)

    return CurrentUser(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        role=user.role,
        full_name=user.full_name,
    )


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
