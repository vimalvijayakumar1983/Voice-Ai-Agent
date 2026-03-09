from uuid import UUID

from pydantic import BaseModel


class IntegrationCreate(BaseModel):
    name: str
    integration_type: str
    config: dict


class IntegrationUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    is_active: bool | None = None


class IntegrationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    integration_type: str
    config: dict
    is_active: bool

    model_config = {"from_attributes": True}
