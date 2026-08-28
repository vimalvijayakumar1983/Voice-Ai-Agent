from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, EmailStr, Field, StringConstraints

from app.core.identity import normalize_email

NonBlankText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
TenantSlug = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
CanonicalEmail = Annotated[EmailStr, BeforeValidator(normalize_email)]


class TenantCreate(BaseModel):
    name: NonBlankText
    slug: TenantSlug


class TenantResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    is_active: bool

    model_config = {"from_attributes": True}


class UserRegister(BaseModel):
    email: CanonicalEmail
    password: str = Field(min_length=8, max_length=128)
    full_name: NonBlankText
    tenant_name: NonBlankText
    tenant_slug: TenantSlug


class RegistrationPolicyResponse(BaseModel):
    mode: Literal["bootstrap", "invite_only", "open"]
    registration_available: bool
    message: str


class UserLogin(BaseModel):
    email: CanonicalEmail
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: UUID
    user_id: UUID
    role: str


class LegacySessionMigration(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=4096)


class UserResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    tenant_id: UUID
    tenant_name: str | None = None
    tenant_slug: str | None = None

    model_config = {"from_attributes": True}


class UserInvite(BaseModel):
    email: CanonicalEmail
    full_name: NonBlankText
    role: Literal["admin", "member", "viewer"] = "member"


class InvitationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    invited_by_user_id: UUID | None
    email: str
    full_name: str
    role: Literal["admin", "member", "viewer"]
    status: Literal["pending", "accepted", "expired", "revoked"]
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationCreatedResponse(InvitationResponse):
    token: str  # Only returned once, when the invitation is created.


class InvitationAccept(BaseModel):
    token: str = Field(min_length=32, max_length=512)
    password: str = Field(min_length=8, max_length=128)


class UserUpdate(BaseModel):
    role: Literal["admin", "member", "viewer"] | None = None
    is_active: bool | None = None


class ApiKeyCreate(BaseModel):
    name: NonBlankText
    expires_in_days: int = Field(default=90, ge=1, le=365)


class ApiKeyResponse(BaseModel):
    id: UUID
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class ApiKeyCreatedResponse(ApiKeyResponse):
    key: str  # Only returned once, when the key is created.
