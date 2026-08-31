"""Contracts for the provider-neutral VAV realtime runtime."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RuntimeProfileUpdate(BaseModel):
    telephony_provider: Literal["twilio", "livekit_sip"] = "twilio"
    primary_speech_provider: Literal["sarvam", "elevenlabs"] = "sarvam"
    fallback_speech_provider: Literal["smallest", "sarvam", "elevenlabs"] | None = None
    llm_provider: Literal["openai"] = "openai"
    llm_model: Literal["gpt-4o-mini", "gpt-4o"] = "gpt-4o-mini"
    stt_language: str = Field("auto", min_length=2, max_length=30)
    max_concurrent_calls: int = Field(1, ge=1, le=100)
    daily_call_limit: int = Field(100, ge=1, le=100_000)
    monthly_budget_cents: int = Field(5000, ge=100, le=100_000_000)
    assigned_numbers: list[str] = Field(default_factory=list, max_length=100)

    model_config = {"extra": "forbid"}

    @field_validator("assigned_numbers")
    @classmethod
    def validate_numbers(cls, value: list[str]) -> list[str]:
        result = list(dict.fromkeys(number.strip() for number in value))
        if any(not re.fullmatch(r"\+[1-9]\d{7,14}", number) for number in result):
            raise ValueError("Assigned numbers must use E.164 format")
        return result

    @field_validator("stt_language")
    @classmethod
    def validate_stt_language(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "auto" or re.fullmatch(r"[a-z]{2}-IN", normalized):
            return normalized
        raise ValueError("STT language must be auto or an Indian locale such as en-IN")


class RuntimeProfileResponse(BaseModel):
    id: UUID | None = None
    agent_id: UUID
    enabled: bool
    telephony_provider: str
    primary_speech_provider: str
    fallback_speech_provider: str | None
    llm_provider: str
    llm_model: str
    stt_language: str
    max_concurrent_calls: int
    daily_call_limit: int
    monthly_budget_cents: int
    assigned_numbers: list[str]
    status: str
    ready: bool
    blockers: list[str]
    last_tested_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RuntimeReadinessResponse(BaseModel):
    agent_id: UUID
    ready: bool
    status: Literal["ready", "blocked"]
    blockers: list[str]
    checks: dict[str, bool]
    tested_at: datetime


class SipCredentialRequest(BaseModel):
    sip_uri: str = Field(min_length=4, max_length=500)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=512)
    inbound_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    livekit_url: str = Field(min_length=8, max_length=500)
    livekit_api_key: str = Field(min_length=8, max_length=255)
    livekit_api_secret: str = Field(min_length=16, max_length=512)

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class SipCredentialStatus(BaseModel):
    configured: bool
    updated_at: datetime | None = None


class ApiKeyCredentialRequest(BaseModel):
    api_key: str = Field(min_length=20, max_length=512)

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("API key must not contain whitespace")
        return value


class TwilioCredentialRequest(BaseModel):
    account_sid: str = Field(pattern=r"^AC[a-fA-F0-9]{32}$")
    auth_token: str = Field(min_length=20, max_length=512)
    default_from_number: str | None = Field(
        default=None,
        pattern=r"^\+[1-9]\d{7,14}$",
    )

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    @field_validator("auth_token")
    @classmethod
    def validate_auth_token(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Twilio auth token must not contain whitespace")
        return value


class WorkspaceCredentialStatus(BaseModel):
    provider: str
    configured: bool
    source: str
    updated_at: datetime | None = None
    account_sid_hint: str | None = None
    default_from_number: str | None = None


class WorkspaceCredentialStatuses(BaseModel):
    providers: dict[str, WorkspaceCredentialStatus]
