"""Authentication and user management endpoints."""

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_password,
    verify_password,
)
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.tenant import Tenant
from app.models.user import ApiKey, User
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyResponse,
    TokenRefresh,
    TokenResponse,
    UserInvite,
    UserLogin,
    UserRegister,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    """Register a new tenant and owner user."""
    # Check if email already exists
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Check if tenant slug exists
    existing_tenant = await db.execute(select(Tenant).where(Tenant.slug == data.tenant_slug))
    if existing_tenant.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Tenant slug already taken")

    # Create tenant
    tenant = Tenant(name=data.tenant_name, slug=data.tenant_slug)
    db.add(tenant)
    await db.flush()

    # Create owner user
    user = User(
        tenant_id=tenant.id,
        email=data.email,
        hashed_password=hash_password(data.password),
        full_name=data.full_name,
        role="owner",
    )
    db.add(user)
    await db.flush()

    return TokenResponse(
        access_token=create_access_token(user.id, tenant.id, user.role),
        refresh_token=create_refresh_token(user.id, tenant.id),
        tenant_id=tenant.id,
        user_id=user.id,
        role=user.role,
    )


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    """Login with email and password."""
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    # Update last login
    user.last_login = datetime.now(UTC)

    return TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        refresh_token=create_refresh_token(user.id, user.tenant_id),
        tenant_id=user.tenant_id,
        user_id=user.id,
        role=user.role,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(data: TokenRefresh, db: AsyncSession = Depends(get_db)):
    """Refresh access token."""
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    import uuid

    user_id = uuid.UUID(payload["sub"])
    tenant_id = uuid.UUID(payload["tenant_id"])

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(user.id, tenant_id, user.role),
        refresh_token=create_refresh_token(user.id, tenant_id),
        tenant_id=tenant_id,
        user_id=user.id,
        role=user.role,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: CurrentUser = Depends(get_current_user)):
    """Get current user info."""
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        is_active=True,
        tenant_id=current_user.tenant_id,
    )


@router.post("/invite", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def invite_user(
    data: UserInvite,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Invite a new user to the tenant."""
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Generate a temporary password (user should reset)
    temp_password = secrets.token_urlsafe(12)
    user = User(
        tenant_id=current_user.tenant_id,
        email=data.email,
        hashed_password=hash_password(temp_password),
        full_name=data.full_name,
        role=data.role,
    )
    db.add(user)
    await db.flush()

    return UserResponse.model_validate(user)


@router.post("/api-keys", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    data: ApiKeyCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new API key for the tenant."""
    raw_key = generate_api_key()
    api_key = ApiKey(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        key_hash=hash_password(raw_key),
        name=data.name,
    )
    db.add(api_key)
    await db.flush()

    return ApiKeyResponse(id=api_key.id, name=api_key.name, key=raw_key, is_active=True)
