import re
from datetime import datetime
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, ValidationInfo, field_validator

from app.services.call_metadata import public_call_metadata
from app.services.provider_variables import validate_provider_variables


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
    # Validate from the private database locator, but serialize only a boolean.
    # The raw provider URL is never part of an API response or browser contract.
    recording_available: bool = Field(
        validation_alias=AliasChoices("recording_available", "provider_recording_url")
    )
    call_metadata: dict | None
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    cost_cents: int | None
    disposition: str | None
    sentiment_score: float | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("call_metadata", mode="before")
    @classmethod
    def redact_private_metadata(cls, value):
        return public_call_metadata(value)

    @field_validator("recording_available", mode="before")
    @classmethod
    def project_recording_availability(cls, value, info: ValidationInfo) -> bool:
        # Twilio reports an explicit recording resource URL. Smallest exposes a
        # fresh server-side download URL by conversation ID, so its provider call
        # ID is sufficient to offer a secure load even when no URL was included
        # in the lifecycle callback.
        provider = info.data.get("provider")
        if provider == "smallest":
            return bool(info.data.get("provider_call_sid"))
        if provider == "twilio":
            return bool(value)
        return False


class CallOutbound(BaseModel):
    agent_id: UUID
    to_number: str
    from_number: str | None = None
    context: dict[str, str | int | float | bool] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @field_validator("to_number")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
            raise ValueError("Phone number must be in E.164 format, for example +971501234567")
        return value

    @field_validator("from_number")
    @classmethod
    def validate_optional_e164(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
            raise ValueError("Phone number must be in E.164 format, for example +971501234567")
        return value

    @field_validator("context")
    @classmethod
    def validate_context(
        cls,
        value: dict[str, str | int | float | bool],
    ) -> dict[str, str | int | float | bool]:
        return validate_provider_variables(value, label="Call context") or {}


class BrowserConversationRegister(BaseModel):
    agent_id: UUID
    provider_call_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    model_config = {"extra": "forbid"}


class ProviderHistorySyncResponse(BaseModel):
    scanned: int
    imported: int
    updated: int
    failed: int


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
