from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CampaignContactCreate(BaseModel):
    phone_number: str
    name: str | None = None
    context_data: dict | None = None


class CampaignContactResponse(BaseModel):
    id: UUID
    phone_number: str
    name: str | None
    status: str
    attempts: int
    last_call_id: UUID | None
    context_data: dict | None

    model_config = {"from_attributes": True}


class CampaignCreate(BaseModel):
    name: str
    description: str | None = None
    agent_id: UUID
    workflow_id: UUID | None = None
    campaign_type: str = "outbound"
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    calling_hours_start: str | None = "09:00"
    calling_hours_end: str | None = "17:00"
    timezone: str = "UTC"
    max_concurrent_calls: int = 5
    retry_attempts: int = 2
    contacts: list[CampaignContactCreate] = []


class CampaignUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    agent_id: UUID | None = None
    workflow_id: UUID | None = None
    status: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    calling_hours_start: str | None = None
    calling_hours_end: str | None = None
    timezone: str | None = None
    max_concurrent_calls: int | None = None
    retry_attempts: int | None = None


class CampaignResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    agent_id: UUID | None
    workflow_id: UUID | None
    name: str
    description: str | None
    status: str
    campaign_type: str
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    calling_hours_start: str | None
    calling_hours_end: str | None
    timezone: str
    max_concurrent_calls: int
    retry_attempts: int
    total_contacts: int
    completed_contacts: int
    successful_contacts: int
    created_at: datetime

    model_config = {"from_attributes": True}
