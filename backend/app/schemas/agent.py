from uuid import UUID

from pydantic import BaseModel


class AgentCreate(BaseModel):
    name: str
    description: str | None = None
    system_prompt: str
    model_provider: str = "openai"
    model_name: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 500
    voice_provider: str = "twilio"
    voice_id: str = "en-US-Standard-A"
    language: str = "en-US"
    speech_rate: float = 1.0
    greeting_message: str | None = None
    fallback_message: str | None = None
    max_call_duration_seconds: int = 600
    transfer_number: str | None = None


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

    model_config = {"from_attributes": True}


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
