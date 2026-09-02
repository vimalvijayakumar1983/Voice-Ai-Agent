from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.agent_catalog import language_code
from app.services.provider_variables import validate_provider_variables


def _deduplicate_languages(languages: list[str]) -> list[str]:
    return list(dict.fromkeys(_normalize_language(language) for language in languages))


def _normalize_language(language: str) -> str:
    normalized = language_code(language)
    if not normalized:
        raise ValueError("Language must be a safe ISO or provider language code")
    return normalized


def _validate_timezone(timezone: str) -> str:
    normalized = timezone.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Timezone must be a valid IANA timezone") from exc
    return normalized


def validate_language_configuration(
    language: str,
    supported_languages: list[str],
    switching_enabled: bool,
    switching_mode: str,
) -> None:
    if language not in supported_languages:
        raise ValueError("Primary language must be included in supported languages")
    if switching_enabled != (switching_mode == "automatic"):
        raise ValueError(
            "Language switching mode must be automatic when switching is enabled, "
            "and disabled when switching is disabled"
        )
    if switching_enabled and len(supported_languages) < 2:
        raise ValueError("Automatic language switching requires at least two supported languages")


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str | None = Field(None, max_length=2000)
    system_prompt: str = Field(min_length=10, max_length=4000)
    model_provider: Literal["smallest"] = "smallest"
    model_name: Literal["electron"] = "electron"
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(500, ge=32, le=8192)
    voice_provider: Literal["smallest", "sarvam", "elevenlabs", "inworld"] = "smallest"
    voice_id: str = Field("", max_length=100)
    language: str = Field("en", min_length=2, max_length=63)
    supported_languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    language_switching_enabled: bool | None = None
    language_switching_mode: Literal["disabled", "automatic"] | None = None
    speech_rate: float = Field(1.0, ge=0.5, le=2)
    greeting_message: str | None = Field(None, max_length=500)
    fallback_message: str | None = Field(None, max_length=500)
    max_call_duration_seconds: int = Field(600, ge=30, le=7200)
    transfer_number: str | None = Field(None, pattern=r"^\+[1-9]\d{7,14}$")
    timezone: str = Field("Asia/Dubai", max_length=64)

    @field_validator("supported_languages")
    @classmethod
    def normalize_supported_languages(cls, value: list[str]) -> list[str]:
        return _deduplicate_languages(value)

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return _normalize_language(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone(value)

    @model_validator(mode="after")
    def validate_language_configuration(self):
        if self.language_switching_enabled is None and self.language_switching_mode is None:
            self.language_switching_enabled = len(self.supported_languages) > 1
            self.language_switching_mode = (
                "automatic" if self.language_switching_enabled else "disabled"
            )
        elif self.language_switching_enabled is None:
            self.language_switching_enabled = self.language_switching_mode == "automatic"
        elif self.language_switching_mode is None:
            self.language_switching_mode = (
                "automatic" if self.language_switching_enabled else "disabled"
            )
        validate_language_configuration(
            self.language,
            self.supported_languages,
            self.language_switching_enabled,
            self.language_switching_mode,
        )
        return self


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=255)
    description: str | None = Field(None, max_length=2000)
    system_prompt: str | None = Field(None, min_length=10, max_length=4000)
    model_provider: Literal["smallest"] | None = None
    model_name: Literal["electron"] | None = None
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=32, le=8192)
    voice_provider: Literal["smallest", "sarvam", "elevenlabs", "inworld"] | None = None
    voice_id: str | None = Field(None, max_length=100)
    language: str | None = Field(None, min_length=2, max_length=63)
    supported_languages: list[str] | None = Field(None, min_length=1)
    language_switching_enabled: bool | None = None
    language_switching_mode: Literal["disabled", "automatic"] | None = None
    speech_rate: float | None = Field(None, ge=0.5, le=2)
    greeting_message: str | None = Field(None, max_length=500)
    fallback_message: str | None = Field(None, max_length=500)
    max_call_duration_seconds: int | None = Field(None, ge=30, le=7200)
    transfer_number: str | None = Field(None, pattern=r"^\+[1-9]\d{7,14}$")
    is_active: bool | None = None
    timezone: str | None = Field(None, max_length=64)

    @field_validator("supported_languages")
    @classmethod
    def normalize_supported_languages(cls, value: list[str] | None) -> list[str] | None:
        return _deduplicate_languages(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return _normalize_language(value) if value is not None else None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        return _validate_timezone(value) if value is not None else None

    @model_validator(mode="after")
    def reject_null_for_required_columns(self):
        required = {
            "name",
            "system_prompt",
            "model_provider",
            "model_name",
            "temperature",
            "max_tokens",
            "voice_provider",
            "voice_id",
            "language",
            "supported_languages",
            "language_switching_enabled",
            "language_switching_mode",
            "speech_rate",
            "max_call_duration_seconds",
            "is_active",
            "timezone",
        }
        null_fields = sorted(
            field for field in required & self.model_fields_set if getattr(self, field) is None
        )
        if null_fields:
            raise ValueError(f"Fields cannot be null: {', '.join(null_fields)}")
        return self


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
    supported_languages: list[str]
    language_switching_enabled: bool
    language_switching_mode: Literal["disabled", "automatic"]
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


class AgentAIDraftRequest(BaseModel):
    brief: str = Field(min_length=20, max_length=4000)
    provider_preference: Literal["auto", "smallest", "sarvam", "elevenlabs", "inworld"] = "auto"
    primary_language: str = Field("en", min_length=2, max_length=63)
    timezone: str = Field("Asia/Dubai", max_length=64)

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    @field_validator("primary_language")
    @classmethod
    def normalize_primary_language(cls, value: str) -> str:
        return _normalize_language(value)

    @field_validator("timezone")
    @classmethod
    def validate_draft_timezone(cls, value: str) -> str:
        return _validate_timezone(value)


class AgentAIDraftResponse(BaseModel):
    draft: AgentCreate
    rationale: str = Field(max_length=1500)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    recommended_knowledge_base_id: UUID | None = None
    recommended_knowledge_base_name: str | None = None
    model: str


class SmallestSessionRequest(BaseModel):
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @field_validator("variables")
    @classmethod
    def validate_variables(
        cls,
        value: dict[str, str | int | float | bool],
    ) -> dict[str, str | int | float | bool]:
        return validate_provider_variables(value, label="Session variables") or {}


class SmallestSessionResponse(BaseModel):
    access_token: str
    expires_in: int
    sample_rate: int = 24000


class VoicePreviewRequest(BaseModel):
    provider: Literal["smallest", "sarvam", "elevenlabs", "inworld"] = "smallest"
    voice_id: str = Field(min_length=1, max_length=100)
    language: str | None = Field(None, min_length=2, max_length=63)

    model_config = {"extra": "forbid"}

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return _normalize_language(value) if value is not None else None


class SarvamCredentialRequest(BaseModel):
    api_key: str = Field(min_length=20, max_length=512)

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Sarvam API key must not contain whitespace")
        return value


class ProviderCredentialStatus(BaseModel):
    provider: Literal["sarvam"] = "sarvam"
    configured: bool
    source: Literal["workspace", "platform", "none"]
    updated_at: datetime | None = None


class VoiceCloneResponse(BaseModel):
    id: UUID
    provider: str
    provider_voice_id: str | None
    display_name: str
    description: str | None
    language: str
    accent: str | None
    gender: str | None
    model: str
    model_ids: list[str]
    status: str
    last_error: str | None
    last_synced_at: datetime | None
    consent_confirmed_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SmallestProviderResolution(BaseModel):
    action: Literal[
        "confirm_create_absent",
        "confirm_publish_absent",
    ]
    confirmation: str = Field(min_length=10, max_length=100)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_resolution_confirmation(self):
        confirmations = {
            "confirm_create_absent": "I CONFIRM NO REMOTE AGENT EXISTS",
            "confirm_publish_absent": "I CONFIRM NO NEW REVISION EXISTS",
        }
        if self.confirmation != confirmations[self.action]:
            raise ValueError("Provider resolution confirmation does not match the selected action")
        return self


class VoiceCatalogItem(BaseModel):
    provider: Literal["smallest", "sarvam", "elevenlabs", "inworld"] = "smallest"
    id: str
    name: str
    languages: list[str]
    accent: str | None = None
    gender: str | None = None
    age: str | None = None
    use_cases: list[str] = Field(default_factory=list)
    synthesizer_model: str | None = None
    unavailability_reason: str | None = None
    voice_pool: Literal["standard", "pro", "cloned", "unknown"] = "unknown"
    source: Literal["catalog", "cloned"] = "catalog"


class LanguageCatalogItem(BaseModel):
    code: str
    name: str


class AgentTemplate(BaseModel):
    id: str
    name: str
    category: str
    description: str
    system_prompt: str
    greeting_message: str
    default_language: str = "en"
    supported_languages: list[str] = Field(default_factory=lambda: ["en"])
    voice_id: str = ""
    speech_rate: float = 1.0
    temperature: float = 0.5
    timezone: str = "Asia/Dubai"


class ProviderFieldCapability(BaseModel):
    status: Literal["synced", "create_only", "local_only"]
    provider_field: str | None = None
    reason: str | None = None


class AgentProviderCatalog(BaseModel):
    provider: str = "multi"
    voice_model: str = "provider-specific"
    voices: list[VoiceCatalogItem]
    languages: list[LanguageCatalogItem]
    templates: list[AgentTemplate]
    field_capabilities: dict[str, ProviderFieldCapability] = Field(default_factory=dict)


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
