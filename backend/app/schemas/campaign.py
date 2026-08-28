from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.phone_numbers import normalize_e164
from app.services.provider_variables import validate_provider_variables


def _validate_calling_hour(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("Calling hours must use 24-hour HH:MM format") from exc
    return parsed.strftime("%H:%M")


def _validate_scheduled_datetime(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("Scheduled timestamps must include a timezone offset")
    return value


class CampaignContactCreate(BaseModel):
    phone_number: str
    name: str | None = None
    context_data: dict[str, str | int | float | bool] | None = None

    @field_validator("phone_number")
    @classmethod
    def normalize_phone_number(cls, value: str) -> str:
        normalized = normalize_e164(value)
        if not normalized:
            raise ValueError("Phone number must be a valid E.164 number")
        return normalized

    @field_validator("context_data")
    @classmethod
    def validate_provider_variables(
        cls,
        value: dict[str, str | int | float | bool] | None,
    ) -> dict[str, str | int | float | bool] | None:
        return validate_provider_variables(value, label="Contact context")


class CampaignContactResponse(BaseModel):
    id: UUID
    phone_number: str
    name: str | None
    status: str
    attempts: int
    last_call_id: UUID | None
    context_data: dict | None

    model_config = {"from_attributes": True}


class CampaignAttemptResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    contact_id: UUID
    call_id: UUID | None
    attempt_number: int
    provider: str
    provider_call_sid: str | None
    state: str
    dispatch_started_at: datetime | None
    accepted_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CampaignAttemptReconcile(BaseModel):
    action: Literal["release_for_retry"]
    reason: str = Field(min_length=3, max_length=500)

    # Provider call identities in the shared provider account are not
    # tenant-authenticating evidence. Only signed callbacks may bind them.
    model_config = {"extra": "forbid"}


class CampaignCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str | None = None
    agent_id: UUID
    workflow_id: UUID | None = None
    campaign_type: Literal["outbound", "survey"] = "outbound"
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    calling_hours_start: str | None = "09:00"
    calling_hours_end: str | None = "17:00"
    timezone: str = "UTC"
    max_concurrent_calls: int = Field(5, ge=1, le=100)
    retry_attempts: int = Field(2, ge=0, le=10)
    contacts: list[CampaignContactCreate] = Field(default_factory=list, max_length=10_000)

    @field_validator("calling_hours_start", "calling_hours_end")
    @classmethod
    def validate_calling_hours(cls, value: str | None) -> str | None:
        return _validate_calling_hour(value)

    @field_validator("scheduled_start", "scheduled_end")
    @classmethod
    def validate_scheduled_timestamps(cls, value: datetime | None) -> datetime | None:
        return _validate_scheduled_datetime(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_schedule(self):
        if (self.calling_hours_start is None) != (self.calling_hours_end is None):
            raise ValueError("Calling-hours start and end must both be set or both be omitted")
        if (
            self.calling_hours_start is not None
            and self.calling_hours_start == self.calling_hours_end
        ):
            raise ValueError("Calling-hours start and end cannot be the same")
        if self.scheduled_start and self.scheduled_end:
            if self.scheduled_end <= self.scheduled_start:
                raise ValueError("Scheduled end must be after scheduled start")
        phone_numbers = [contact.phone_number for contact in self.contacts]
        if len(phone_numbers) != len(set(phone_numbers)):
            raise ValueError("A phone number can appear only once in a campaign")
        return self


class CampaignUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=255)
    description: str | None = None
    agent_id: UUID | None = None
    workflow_id: UUID | None = None
    status: Literal["draft", "paused", "cancelled"] | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    calling_hours_start: str | None = None
    calling_hours_end: str | None = None
    timezone: str | None = None
    max_concurrent_calls: int | None = Field(None, ge=1, le=100)
    retry_attempts: int | None = Field(None, ge=0, le=10)

    @field_validator("agent_id")
    @classmethod
    def require_agent_id(cls, value: UUID | None) -> UUID:
        if value is None:
            raise ValueError("Campaign agent cannot be removed")
        return value

    @field_validator("name", "status")
    @classmethod
    def require_text_fields(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @field_validator("max_concurrent_calls", "retry_attempts")
    @classmethod
    def require_numeric_fields(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @field_validator("calling_hours_start", "calling_hours_end")
    @classmethod
    def validate_calling_hours(cls, value: str | None) -> str | None:
        return _validate_calling_hour(value)

    @field_validator("scheduled_start", "scheduled_end")
    @classmethod
    def validate_scheduled_timestamps(cls, value: datetime | None) -> datetime | None:
        return _validate_scheduled_datetime(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Timezone cannot be null")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value


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
