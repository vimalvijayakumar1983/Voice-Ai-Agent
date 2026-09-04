"""Contracts for the provider-neutral VAV realtime runtime."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

PRODUCTION_LLM_MODELS = {
    "openai": ("gpt-4o-mini", "gpt-4o"),
    "inworld": ("auto", "openai/gpt-4o-mini", "openai/gpt-4o"),
}

InworldSTTModel = Literal[
    "auto",
    "assemblyai/u3-rt-pro",
    "soniox/stt-rt-v4",
    "inworld/inworld-stt-1",
]

DiagnosticRecordingMode = Literal["off", "livekit_egress_explicit_consent"]
KnowledgeTurnMode = Literal["tool_loop", "single_pass_experimental"]


class RuntimeProfileUpdate(BaseModel):
    telephony_provider: Literal["twilio", "livekit_sip"] = "twilio"
    primary_speech_provider: Literal["sarvam", "elevenlabs", "inworld"] = "sarvam"
    fallback_speech_provider: Literal["smallest", "sarvam", "elevenlabs", "inworld"] | None = None
    llm_provider: Literal["openai", "inworld"] = "openai"
    llm_model: str = Field("gpt-4o-mini", min_length=2, max_length=100)
    voice_runtime: Literal["pipeline", "inworld_realtime"] = "pipeline"
    knowledge_turn_mode: KnowledgeTurnMode = "tool_loop"
    stt_language: str = Field("auto", min_length=2, max_length=30)
    stt_model: InworldSTTModel = "auto"
    tts_delivery_mode: Literal["stable", "balanced", "creative"] = "balanced"
    diagnostic_recording_mode: DiagnosticRecordingMode = "off"
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
        if normalized == "auto" or re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", normalized):
            return normalized
        raise ValueError("STT language must be auto or a BCP-47 code such as en-GB or ar-AE")

    @model_validator(mode="after")
    def validate_llm_route(self):
        model = self.llm_model.strip()
        allowed = PRODUCTION_LLM_MODELS[self.llm_provider]
        if model not in allowed:
            choices = ", ".join(allowed)
            provider_name = "OpenAI" if self.llm_provider == "openai" else "Inworld"
            raise ValueError(f"{provider_name} LLM routes support only: {choices}")
        self.llm_model = model
        if self.voice_runtime == "inworld_realtime" and self.llm_provider != "inworld":
            raise ValueError(
                "Native Inworld Realtime requires the Inworld LLM route; the selected "
                "model is then executed inside the same Inworld Realtime session"
            )
        if self.voice_runtime == "inworld_realtime" and model == "auto":
            raise ValueError("Native Inworld Realtime requires an explicit production model route")
        if (
            self.knowledge_turn_mode == "single_pass_experimental"
            and self.voice_runtime != "inworld_realtime"
        ):
            raise ValueError("Experimental single-pass knowledge requires Native Inworld Realtime")
        return self


class RuntimeProfileResponse(BaseModel):
    id: UUID | None = None
    agent_id: UUID
    enabled: bool
    telephony_provider: str
    primary_speech_provider: str
    fallback_speech_provider: str | None
    llm_provider: str
    llm_model: str
    voice_runtime: Literal["pipeline", "inworld_realtime"]
    knowledge_turn_mode: KnowledgeTurnMode
    stt_language: str
    stt_model: InworldSTTModel
    tts_delivery_mode: Literal["stable", "balanced", "creative"]
    diagnostic_recording_mode: DiagnosticRecordingMode
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
    inbound_trunk_id: str = Field(min_length=4, max_length=255)
    dispatch_rule_id: str = Field(min_length=4, max_length=255)
    outbound_trunk_id: str | None = Field(default=None, min_length=4, max_length=255)
    agent_name: str = Field(default="vav-inworld", min_length=2, max_length=100)

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class SipCredentialStatus(BaseModel):
    configured: bool
    route_recorded: bool = False
    gateway_provisioned: bool = False
    inbound_trunk_hint: str | None = None
    dispatch_rule_hint: str | None = None
    agent_name: str | None = None
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
