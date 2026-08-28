from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.services.phone_numbers import normalize_e164


class DncEntryCreate(BaseModel):
    phone_number: str
    reason: str | None = None
    source: str = "manual"


class DncEntryResponse(BaseModel):
    id: UUID
    phone_number: str
    reason: str | None
    source: str | None

    model_config = {"from_attributes": True}


class ConsentRecordCreate(BaseModel):
    phone_number: str
    consent_type: Literal["outbound_call", "marketing_call", "recording", "data_processing"]
    status: Literal["granted", "revoked"]
    evidence: dict | None = Field(default=None, max_length=50)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone_number(cls, value: str) -> str:
        normalized = normalize_e164(value)
        if normalized is None:
            raise ValueError("Phone number must be a valid E.164 number")
        return normalized


class ConsentRecordResponse(BaseModel):
    id: UUID
    phone_number: str
    consent_type: str
    status: str
    evidence: dict | None
    granted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DncCheckResponse(BaseModel):
    phone_number: str
    is_on_dnc: bool
