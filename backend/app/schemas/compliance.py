from uuid import UUID

from pydantic import BaseModel


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
    consent_type: str
    status: str
    evidence: dict | None = None


class ConsentRecordResponse(BaseModel):
    id: UUID
    phone_number: str
    consent_type: str
    status: str
    evidence: dict | None

    model_config = {"from_attributes": True}


class DncCheckResponse(BaseModel):
    phone_number: str
    is_on_dnc: bool
