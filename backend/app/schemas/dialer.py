from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.phone_numbers import normalize_e164

DialingMode = Literal["preview", "progressive", "parallel", "predictive", "scheduled", "event"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, str_strip_whitespace=True)


class CustomerInput(StrictModel):
    company: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=255)
    phone_number: str
    external_id: str | None = Field(None, max_length=160)
    language: str = Field("en", min_length=2, max_length=30)
    timezone: str = "Asia/Dubai"
    notes: str = Field("", max_length=5000)
    contact_allowed: bool = False
    consent_reference: str = Field("", max_length=500)

    @field_validator("phone_number")
    @classmethod
    def phone(cls, value):
        value = normalize_e164(value)
        if not value:
            raise ValueError("Use a valid E.164 phone number including country code")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_name(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Use an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def consent(self):
        if self.contact_allowed and not self.consent_reference:
            raise ValueError("Provide the source of permission to contact this customer")
        return self


class CustomerResponse(CustomerInput):
    id: UUID
    opted_out: bool


class CustomerPermission(StrictModel):
    opted_out: bool
    reason: str = Field(min_length=3, max_length=500)


class CampaignInput(StrictModel):
    name: str = Field(min_length=2, max_length=255)
    company: str = Field(min_length=1, max_length=160)
    agent_id: UUID
    mode: DialingMode = "progressive"
    purpose: Literal["reactivation", "qualification", "reminder", "survey", "collections"] = (
        "survey"
    )
    timezone: str = "Asia/Dubai"
    calling_hours_start: str = "09:00"
    calling_hours_end: str = "18:00"
    max_concurrent_calls: int = Field(1, ge=1, le=20)
    target_live_calls: int = Field(1, ge=1, le=20)
    max_attempts_per_customer: int = Field(1, ge=1, le=3)
    max_attempts_total: int = Field(100, ge=1, le=10000)
    retry_delay_hours: int = Field(24, ge=24, le=168)
    approved_offer: str = Field("", max_length=1000)
    compliance_approved: bool = False

    _timezone = field_validator("timezone")(CustomerInput.timezone_name.__func__)

    @model_validator(mode="after")
    def validate_window(self):
        for value in (self.calling_hours_start, self.calling_hours_end):
            if len(value) != 5:
                raise ValueError("Calling hours must be HH:MM")
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError as exc:
                raise ValueError("Calling hours must be HH:MM") from exc
        if self.calling_hours_start >= self.calling_hours_end:
            raise ValueError(
                "Calling window must end after it starts; overnight windows unsupported"
            )
        if self.target_live_calls > self.max_concurrent_calls:
            raise ValueError("Target live calls cannot exceed reserved channel capacity")
        return self


class CampaignResponse(StrictModel):
    id: UUID
    name: str
    company: str
    agent_id: UUID
    mode: str
    purpose: str
    status: str
    config: dict


class EnqueueInput(StrictModel):
    customer_ids: list[UUID] = Field(min_length=1, max_length=1000)
    available_at: datetime | None = None
    event_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")

    @field_validator("available_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Include the timezone offset in the scheduled time")
        return value

    @field_validator("customer_ids")
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate customers in one request")
        return value


class CampaignAction(StrictModel):
    action: Literal["start", "pause", "cancel"]


class JobResponse(StrictModel):
    id: UUID
    campaign_id: UUID
    customer_id: UUID
    event_key: str
    state: str
    available_at: datetime
    approved: bool
    attempts: int
    call_id: UUID | None
    call_ids: list
    outcome: str | None
    error: str | None
