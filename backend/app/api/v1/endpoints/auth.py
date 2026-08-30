"""Authentication and user management endpoints."""

import asyncio
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.identity import normalize_email
from app.core.security import (
    create_access_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.tenant import Tenant
from app.models.user import ApiKey, User, UserInvitation
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    InvitationAccept,
    InvitationCreatedResponse,
    InvitationResponse,
    LegacySessionMigration,
    RegistrationPolicyResponse,
    TokenResponse,
    UserInvite,
    UserLogin,
    UserRegister,
    UserResponse,
    UserUpdate,
)
from app.services.audit import record_audit_event
from app.services.auth_sessions import (
    RefreshSessionRejectedError,
    issue_refresh_session,
    revoke_refresh_token,
    revoke_user_sessions,
    rotate_refresh_session,
)
from app.services.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/auth", tags=["Authentication"])

INVITATION_LIFETIME = timedelta(days=7)
DUMMY_PASSWORD_HASH = hash_password("constant-time-invalid-account-password")
_SQLITE_INVITATION_LOCK = asyncio.Lock()
_SQLITE_REGISTRATION_LOCK = asyncio.Lock()
_REGISTRATION_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"voice-ai-agent:owner-bootstrap").digest()[:8],
    "big",
    signed=True,
)
REGISTRATION_UNAVAILABLE_DETAIL = (
    "Registration is unavailable. Ask a workspace owner for an invitation."
)
REFRESH_COOKIE_PATH = "/api/v1/auth"
INVALID_AUTH_ORIGIN_DETAIL = "Request origin is not allowed"


def _validate_auth_origin(request: Request, *, required: bool = False) -> None:
    """Reject cross-site cookie mutations before credentials are processed.

    Browser refresh and logout are deliberately stricter than login: they use
    an ambient HttpOnly credential, so an exact configured Origin is required.
    Login, registration, and invitation acceptance still support trusted
    non-browser clients without an Origin header, but reject any supplied
    Origin that is not an exact console allowlist entry.
    """

    origin = request.headers.get("origin")
    if origin is None:
        if required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=INVALID_AUTH_ORIGIN_DETAIL,
            )
        return
    if origin == "null" or not any(
        hmac.compare_digest(origin, allowed_origin) for allowed_origin in settings.cors_origin_list
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INVALID_AUTH_ORIGIN_DETAIL,
        )


def _refresh_cookie_policy() -> tuple[bool, str]:
    if settings.is_production:
        return True, "none"
    return False, "lax"


def _set_refresh_cookie(response: Response, token: str) -> None:
    secure, same_site = _refresh_cookie_policy()
    claims = decode_token(token)
    expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC) if claims else None
    max_age = None
    if expires_at is not None:
        max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=max_age,
        expires=expires_at,
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=same_site,
    )


def _clear_refresh_cookie(response: Response) -> None:
    secure, same_site = _refresh_cookie_policy()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=same_site,
    )


def _refresh_rejection(
    detail: str,
    *,
    status_code: int,
    preserve_cookie_on_conflict: bool = True,
) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    # A 409 means a concurrent tab already rotated the shared browser cookie.
    # Clearing here could race with and destroy the winning response.
    if not preserve_cookie_on_conflict or status_code != status.HTTP_409_CONFLICT:
        _clear_refresh_cookie(response)
    return response


def _hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _invitation_status(invitation: UserInvitation) -> str:
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.resolution_reason == "expired":
        return "expired"
    if invitation.revoked_at is not None:
        return "revoked"
    if _as_utc(invitation.expires_at) <= datetime.now(UTC):
        return "expired"
    return "pending"


def _invitation_response(
    invitation: UserInvitation, *, token: str | None = None
) -> InvitationResponse | InvitationCreatedResponse:
    values = {
        "id": invitation.id,
        "tenant_id": invitation.tenant_id,
        "invited_by_user_id": invitation.invited_by_user_id,
        "email": invitation.email,
        "full_name": invitation.full_name,
        "role": invitation.role,
        "status": _invitation_status(invitation),
        "expires_at": invitation.expires_at,
        "accepted_at": invitation.accepted_at,
        "revoked_at": invitation.revoked_at,
        "created_at": invitation.created_at,
    }
    if token is not None:
        return InvitationCreatedResponse(**values, token=token)
    return InvitationResponse(**values)


async def _create_invitation(
    *,
    data: UserInvite,
    current_user: CurrentUser,
    email: str,
    db: AsyncSession,
) -> InvitationCreatedResponse | InvitationResponse:
    existing = await db.execute(select(User).where(func.lower(func.trim(User.email)) == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    now = datetime.now(UTC)
    # An expired unresolved invite is explicitly superseded before the partial
    # UNIQUE invariant reserves this canonical identity for a new invitation.
    await db.execute(
        update(UserInvitation)
        .where(
            func.lower(func.trim(UserInvitation.email)) == email,
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
            UserInvitation.expires_at <= now,
        )
        .values(revoked_at=now, resolution_reason="expired")
    )
    existing_invitation = await db.execute(
        select(UserInvitation)
        .where(
            func.lower(func.trim(UserInvitation.email)) == email,
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
            UserInvitation.expires_at > now,
        )
        .limit(1)
    )
    if existing_invitation.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A pending invitation already exists for this email",
        )

    raw_token = secrets.token_urlsafe(32)
    invitation = UserInvitation(
        tenant_id=current_user.tenant_id,
        invited_by_user_id=current_user.id,
        email=email,
        full_name=data.full_name,
        role=data.role,
        token_hash=_hash_invitation_token(raw_token),
        expires_at=now + INVITATION_LIFETIME,
    )
    db.add(invitation)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A pending invitation already exists for this email",
        ) from exc

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="invitation.created",
        resource_type="invitation",
        resource_id=str(invitation.id),
        details={
            "email": invitation.email,
            "role": invitation.role,
            "expires_at": invitation.expires_at.isoformat(),
        },
    )
    await db.commit()
    return _invitation_response(invitation, token=raw_token)


def _effective_registration_mode() -> str:
    mode = settings.registration_mode
    if settings.app_env.strip().lower() == "production" and mode == "open":
        # The startup validator should make this unreachable, but the request
        # path remains fail-closed if settings are replaced at runtime.
        return "invite_only"
    return mode


async def _any_user_exists(db: AsyncSession) -> bool:
    return (await db.execute(select(User.id).limit(1))).scalar_one_or_none() is not None


async def _create_registered_owner(
    *,
    data: UserRegister,
    email: str,
    db: AsyncSession,
) -> tuple[TokenResponse, str]:
    existing = await db.execute(select(User).where(func.lower(func.trim(User.email)) == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    existing_tenant = await db.execute(select(Tenant).where(Tenant.slug == data.tenant_slug))
    if existing_tenant.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Tenant slug already taken")

    tenant = Tenant(name=data.tenant_name, slug=data.tenant_slug)
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Tenant slug already taken") from exc

    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password(data.password),
        full_name=data.full_name,
        role="owner",
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Email already registered") from exc

    refresh_token = await issue_refresh_session(
        db,
        user_id=user.id,
        tenant_id=tenant.id,
    )
    await db.commit()
    return (
        TokenResponse(
            access_token=create_access_token(user.id, tenant.id, user.role),
            tenant_id=tenant.id,
            user_id=user.id,
            role=user.role,
        ),
        refresh_token,
    )


async def _bootstrap_registered_owner(
    *,
    data: UserRegister,
    email: str,
    db: AsyncSession,
) -> tuple[TokenResponse, str]:
    configured_email = str(settings.bootstrap_owner_email or "")
    if not configured_email or not hmac.compare_digest(email, configured_email):
        raise HTTPException(status_code=403, detail=REGISTRATION_UNAVAILABLE_DETAIL)
    if await _any_user_exists(db):
        raise HTTPException(status_code=403, detail=REGISTRATION_UNAVAILABLE_DETAIL)
    return await _create_registered_owner(data=data, email=email, db=db)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    response: Response,
    data: UserRegister,
    db: AsyncSession = Depends(get_db),
):
    """Register a new tenant and owner user."""
    _validate_auth_origin(request)
    email = normalize_email(data.email)
    await enforce_rate_limit(
        request,
        scope="register-ip",
        limit=5,
        window_seconds=60 * 60,
    )
    await enforce_rate_limit(
        request,
        scope="register-account",
        subject=email,
        bind_to_client=False,
        limit=3,
        window_seconds=60 * 60,
    )
    mode = _effective_registration_mode()
    if mode == "invite_only":
        raise HTTPException(status_code=403, detail=REGISTRATION_UNAVAILABLE_DETAIL)
    if mode == "open":
        token_response, refresh_token = await _create_registered_owner(
            data=data,
            email=email,
            db=db,
        )
    elif db.get_bind().dialect.name == "sqlite":
        async with _SQLITE_REGISTRATION_LOCK:
            token_response, refresh_token = await _bootstrap_registered_owner(
                data=data,
                email=email,
                db=db,
            )
    else:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _REGISTRATION_LOCK_ID},
            )
        token_response, refresh_token = await _bootstrap_registered_owner(
            data=data,
            email=email,
            db=db,
        )
    _set_refresh_cookie(response, refresh_token)
    return token_response


@router.get("/registration-policy", response_model=RegistrationPolicyResponse)
async def registration_policy(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return public signup availability without exposing the bootstrap identity."""
    await enforce_rate_limit(
        request,
        scope="registration-policy-ip",
        limit=120,
        window_seconds=60,
    )
    mode = _effective_registration_mode()
    if mode == "open":
        return RegistrationPolicyResponse(
            mode="open",
            registration_available=True,
            message="Workspace account creation is available.",
        )
    if (
        mode == "bootstrap"
        and settings.bootstrap_owner_email is not None
        and not await _any_user_exists(db)
    ):
        return RegistrationPolicyResponse(
            mode="bootstrap",
            registration_available=True,
            message="Initial workspace setup is restricted to the designated owner.",
        )
    return RegistrationPolicyResponse(
        mode="invite_only",
        registration_available=False,
        message="New accounts require an invitation from a workspace owner.",
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    response: Response,
    data: UserLogin,
    db: AsyncSession = Depends(get_db),
):
    """Login with email and password."""
    _validate_auth_origin(request)
    email = normalize_email(data.email)
    await enforce_rate_limit(
        request,
        scope="login-ip",
        limit=50,
        window_seconds=5 * 60,
    )
    await enforce_rate_limit(
        request,
        scope="login-account",
        subject=email,
        bind_to_client=False,
        limit=10,
        window_seconds=5 * 60,
    )
    result = await db.execute(select(User).where(func.lower(func.trim(User.email)) == email))
    user = result.scalar_one_or_none()

    password_matches = verify_password(
        data.password,
        user.hashed_password if user else DUMMY_PASSWORD_HASH,
    )
    if not user or not password_matches:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    # Update last login
    user.last_login = datetime.now(UTC)
    refresh_token = await issue_refresh_session(
        db,
        user_id=user.id,
        tenant_id=user.tenant_id,
    )

    token_response = TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        tenant_id=user.tenant_id,
        user_id=user.id,
        role=user.role,
    )
    _set_refresh_cookie(response, refresh_token)
    return token_response


@router.post("/migrate-session", response_model=TokenResponse)
async def migrate_legacy_session(
    request: Request,
    response: Response,
    data: LegacySessionMigration,
    db: AsyncSession = Depends(get_db),
):
    """One-release bridge from a legacy JSON refresh token to the cookie."""

    if not settings.legacy_session_migration_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    _validate_auth_origin(request, required=True)
    await enforce_rate_limit(
        request,
        scope="refresh-ip",
        limit=60,
        window_seconds=60,
    )
    await enforce_rate_limit(
        request,
        scope="refresh-token",
        subject=hashlib.sha256(data.refresh_token.encode("utf-8")).hexdigest(),
        bind_to_client=False,
        limit=20,
        window_seconds=60,
    )
    try:
        user, rotated_token = await rotate_refresh_session(db, data.refresh_token)
    except RefreshSessionRejectedError as exc:
        if exc.persist_changes:
            await db.commit()
        return _refresh_rejection(
            exc.detail,
            status_code=exc.status_code,
            preserve_cookie_on_conflict=False,
        )

    _set_refresh_cookie(response, rotated_token)
    return TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        tenant_id=user.tenant_id,
        user_id=user.id,
        role=user.role,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Rotate the HttpOnly refresh cookie and return a new access token."""
    _validate_auth_origin(request, required=True)
    await enforce_rate_limit(
        request,
        scope="refresh-ip",
        limit=60,
        window_seconds=60,
    )
    refresh_credential = request.cookies.get(settings.refresh_cookie_name)
    if refresh_credential is None or not 20 <= len(refresh_credential) <= 4096:
        return _refresh_rejection("Invalid refresh token", status_code=401)
    await enforce_rate_limit(
        request,
        scope="refresh-token",
        subject=hashlib.sha256(refresh_credential.encode("utf-8")).hexdigest(),
        bind_to_client=False,
        limit=20,
        window_seconds=60,
    )
    try:
        user, rotated_token = await rotate_refresh_session(db, refresh_credential)
    except RefreshSessionRejectedError as exc:
        if exc.persist_changes:
            # Security mutations must survive the 401 response; the request
            # dependency otherwise rolls back exceptions by design.
            await db.commit()
        return _refresh_rejection(exc.detail, status_code=exc.status_code)

    _set_refresh_cookie(response, rotated_token)
    return TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        tenant_id=user.tenant_id,
        user_id=user.id,
        role=user.role,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Revoke a refresh-token family, even when its access token has expired."""
    _validate_auth_origin(request, required=True)
    await enforce_rate_limit(
        request,
        scope="logout-ip",
        limit=120,
        window_seconds=60,
    )
    refresh_credential = request.cookies.get(settings.refresh_cookie_name)
    if refresh_credential is not None and 20 <= len(refresh_credential) <= 4096:
        await revoke_refresh_token(db, refresh_credential)
    # Always return the same result so callers cannot probe session validity.
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user info."""
    result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = result.scalar_one()
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        is_active=True,
        tenant_id=current_user.tenant_id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
    )


@router.post(
    "/invite",
    response_model=InvitationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_user(
    data: UserInvite,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create an expiring, single-use workspace invitation."""
    if current_user.role == "admin" and data.role == "admin":
        raise HTTPException(status_code=403, detail="Only owners can invite administrators")

    email = normalize_email(data.email)
    if db.get_bind().dialect.name == "sqlite":
        async with _SQLITE_INVITATION_LOCK:
            return await _create_invitation(
                data=data,
                current_user=current_user,
                email=email,
                db=db,
            )

    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:email, 0))"),
            {"email": email},
        )
    return await _create_invitation(
        data=data,
        current_user=current_user,
        email=email,
        db=db,
    )


@router.post("/invitations/accept", response_model=TokenResponse)
async def accept_invitation(
    request: Request,
    response: Response,
    data: InvitationAccept,
    db: AsyncSession = Depends(get_db),
):
    """Accept a valid invitation, set a password, and start a session."""
    _validate_auth_origin(request)
    await enforce_rate_limit(
        request,
        scope="invitation-accept-ip",
        limit=20,
        window_seconds=10 * 60,
    )
    await enforce_rate_limit(
        request,
        scope="invitation-accept-token",
        subject=_hash_invitation_token(data.token),
        bind_to_client=False,
        limit=5,
        window_seconds=10 * 60,
    )
    token_hash = _hash_invitation_token(data.token)
    result = await db.execute(
        select(UserInvitation).where(UserInvitation.token_hash == token_hash).with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found or invalid")

    invitation_state = _invitation_status(invitation)
    if invitation_state == "accepted":
        raise HTTPException(status_code=409, detail="Invitation has already been accepted")
    if invitation_state == "revoked":
        raise HTTPException(status_code=410, detail="Invitation has been revoked")
    if invitation_state == "expired":
        raise HTTPException(status_code=410, detail="Invitation has expired")

    email = normalize_email(invitation.email)
    existing = await db.execute(select(User).where(func.lower(func.trim(User.email)) == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        tenant_id=invitation.tenant_id,
        email=email,
        hashed_password=hash_password(data.password),
        full_name=invitation.full_name,
        role=invitation.role,
    )
    db.add(user)
    invitation.accepted_at = datetime.now(UTC)
    invitation.email = email
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from exc

    await record_audit_event(
        db,
        tenant_id=invitation.tenant_id,
        actor_user_id=user.id,
        action="invitation.accepted",
        resource_type="invitation",
        resource_id=str(invitation.id),
        details={"email": invitation.email, "role": invitation.role},
    )

    refresh_token = await issue_refresh_session(
        db,
        user_id=user.id,
        tenant_id=invitation.tenant_id,
    )
    token_response = TokenResponse(
        access_token=create_access_token(user.id, invitation.tenant_id, user.role),
        tenant_id=invitation.tenant_id,
        user_id=user.id,
        role=user.role,
    )
    _set_refresh_cookie(response, refresh_token)
    return token_response


@router.get("/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List invitations for the current workspace without their secret tokens."""
    result = await db.execute(
        select(UserInvitation)
        .where(UserInvitation.tenant_id == current_user.tenant_id)
        .order_by(UserInvitation.created_at.desc())
    )
    return [_invitation_response(invitation) for invitation in result.scalars().all()]


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: str,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a pending invitation in the current workspace."""
    try:
        parsed_id = uuid.UUID(invitation_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid invitation ID") from exc

    result = await db.execute(
        select(UserInvitation).where(
            UserInvitation.id == parsed_id,
            UserInvitation.tenant_id == current_user.tenant_id,
        )
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    invitation_state = _invitation_status(invitation)
    if invitation_state == "accepted":
        raise HTTPException(status_code=409, detail="Accepted invitations cannot be revoked")
    if invitation_state == "revoked":
        raise HTTPException(status_code=409, detail="Invitation has already been revoked")
    if invitation_state == "expired":
        raise HTTPException(status_code=410, detail="Expired invitations cannot be revoked")

    invitation.revoked_at = datetime.now(UTC)
    invitation.resolution_reason = "revoked"
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="invitation.revoked",
        resource_type="invitation",
        resource_id=str(invitation.id),
        details={"email": invitation.email, "role": invitation.role},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List members of the current workspace."""
    result = await db.execute(
        select(User).where(User.tenant_id == current_user.tenant_id).order_by(User.created_at.asc())
    )
    return [UserResponse.model_validate(user) for user in result.scalars().all()]


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    data: UserUpdate,
    current_user: CurrentUser = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Update a non-owner workspace member's role or access."""
    try:
        target_id = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid user ID") from exc

    result = await db.execute(
        select(User).where(
            User.id == target_id,
            User.tenant_id == current_user.tenant_id,
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id or user.role == "owner":
        raise HTTPException(status_code=400, detail="The workspace owner cannot be modified")

    updates = data.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No changes supplied")
    was_active = user.is_active
    for field, value in updates.items():
        setattr(user, field, value)
    await db.flush()
    revoked_sessions = 0
    if was_active and not user.is_active:
        revoked_sessions = await revoke_user_sessions(
            db,
            user_id=user.id,
            tenant_id=current_user.tenant_id,
            reason="account_disabled",
        )
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="user.updated",
        resource_type="user",
        resource_id=str(user.id),
        details={
            "changed_fields": sorted(updates),
            "revoked_sessions": revoked_sessions,
        },
    )
    return UserResponse.model_validate(user)


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    data: ApiKeyCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new API key for the tenant."""
    api_key_id = uuid.uuid4()
    raw_key = generate_api_key(api_key_id)
    api_key = ApiKey(
        id=api_key_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        key_hash=hash_api_key(raw_key),
        name=data.name,
        expires_at=datetime.now(UTC) + timedelta(days=data.expires_in_days),
    )
    db.add(api_key)
    await db.flush()

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="api_key.created",
        resource_type="api_key",
        resource_id=str(api_key.id),
        details={"name": api_key.name, "expires_at": api_key.expires_at.isoformat()},
    )

    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        key=raw_key,
        is_active=True,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
    )


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List workspace API keys without exposing their secret values."""
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.tenant_id == current_user.tenant_id)
        .order_by(ApiKey.created_at.desc())
    )
    return [ApiKeyResponse.model_validate(api_key) for api_key in result.scalars().all()]


@router.delete("/api-keys/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    api_key_id: str,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key immediately while preserving its audit record."""
    try:
        key_id = uuid.UUID(api_key_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid API key ID") from exc

    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.tenant_id == current_user.tenant_id,
        )
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    api_key.is_active = False
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="api_key.revoked",
        resource_type="api_key",
        resource_id=str(api_key.id),
        details={"name": api_key.name},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
