from uuid import UUID

from pydantic import BaseModel, EmailStr


class TenantCreate(BaseModel):
    name: str
    slug: str


class TenantResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    is_active: bool

    model_config = {"from_attributes": True}


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    tenant_name: str
    tenant_slug: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    tenant_id: UUID
    user_id: UUID
    role: str


class TokenRefresh(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    tenant_id: UUID

    model_config = {"from_attributes": True}


class UserInvite(BaseModel):
    email: EmailStr
    full_name: str
    role: str = "member"


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyResponse(BaseModel):
    id: UUID
    name: str
    key: str  # Only returned on creation
    is_active: bool

    model_config = {"from_attributes": True}
