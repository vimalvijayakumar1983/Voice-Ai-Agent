from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CallResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    agent_id: UUID | None
    campaign_id: UUID | None
    direction: str
    status: str
    from_number: str
    to_number: str
    provider: str
    provider_call_sid: str | None
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    cost_cents: int | None
    disposition: str | None
    sentiment_score: float | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CallOutbound(BaseModel):
    agent_id: UUID
    to_number: str
    from_number: str | None = None
    context: dict[str, str | int | float | bool] = Field(default_factory=dict)
    from_product_id: str | None = None
    version_id: str | None = None

    @field_validator("to_number")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        digits = value[1:] if value.startswith("+") else ""
        if not digits.isdigit() or not 8 <= len(digits) <= 15:
            raise ValueError("Phone number must be in E.164 format, for example +971501234567")
        return value


class CallTranscriptResponse(BaseModel):
    id: UUID
    call_id: UUID
    turns: list[dict]
    full_text: str | None

    model_config = {"from_attributes": True}


class CallSummaryResponse(BaseModel):
    id: UUID
    call_id: UUID
    summary: str
    key_topics: list[str] | None
    action_items: list[str] | None
    sentiment: str | None

    model_config = {"from_attributes": True}


class CallListParams(BaseModel):
    agent_id: UUID | None = None
    campaign_id: UUID | None = None
    direction: str | None = None
    status: str | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    page: int = 1
    page_size: int = 50
