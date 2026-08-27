from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str | None = None
    system_prompt: str = Field(min_length=10, max_length=4000)
    model_provider: str = "smallest"
    model_name: str = "electron"
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(500, ge=32, le=8192)
    voice_provider: str = "smallest"
    voice_id: str = ""
    language: str = "en"
    speech_rate: float = Field(1.0, ge=0.5, le=2)
    greeting_message: str | None = Field(None, max_length=500)
    fallback_message: str | None = None
    max_call_duration_seconds: int = 600
    transfer_number: str | None = None
    timezone: str = "Asia/Dubai"


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    voice_provider: str | None = None
    voice_id: str | None = None
    language: str | None = None
    speech_rate: float | None = None
    greeting_message: str | None = None
    fallback_message: str | None = None
    max_call_duration_seconds: int | None = None
    transfer_number: str | None = None
    is_active: bool | None = None
    timezone: str | None = None


class AgentResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    is_active: bool
    system_prompt: str
    model_provider: str
    model_name: str
    temperature: float
    max_tokens: int
    voice_provider: str
    voice_id: str
    language: str
    speech_rate: float
    greeting_message: str | None
    fallback_message: str | None
    max_call_duration_seconds: int
    transfer_number: str | None
    timezone: str
    provider_agent_id: str | None
    provider_branch_id: str | None
    provider_revision_id: str | None
    provider_config: dict | None
    sync_status: str
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SmallestSessionRequest(BaseModel):
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict)


class SmallestSessionResponse(BaseModel):
    access_token: str
    expires_in: int
    sample_rate: int = 24000


class KnowledgeBaseCreate(BaseModel):
    name: str
    content_type: str  # text, url, file
    content: str


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    agent_id: UUID
    name: str
    content_type: str
    content: str
    is_active: bool

    model_config = {"from_attributes": True}
