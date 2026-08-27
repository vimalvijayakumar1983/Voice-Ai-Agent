from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _deduplicate_languages(languages: list[str]) -> list[str]:
    return list(
        dict.fromkeys(language.strip().lower() for language in languages if language.strip())
    )


def _normalize_language(language: str) -> str:
    return language.strip().lower()


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str | None = None
    system_prompt: str = Field(min_length=10, max_length=4000)
    model_provider: str = "smallest"
    model_name: str = "electron"
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(500, ge=32, le=8192)
    voice_provider: str = "smallest"
    voice_id: str = ""
    language: str = "en"
    supported_languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    speech_rate: float = Field(1.0, ge=0.5, le=2)
    greeting_message: str | None = Field(None, max_length=500)
    fallback_message: str | None = None
    max_call_duration_seconds: int = 600
    transfer_number: str | None = None
    timezone: str = "Asia/Dubai"

    @field_validator("supported_languages")
    @classmethod
    def normalize_supported_languages(cls, value: list[str]) -> list[str]:
        return _deduplicate_languages(value)

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return _normalize_language(value)

    @model_validator(mode="after")
    def validate_language_configuration(self):
        if self.language not in self.supported_languages:
            raise ValueError("Primary language must be included in supported languages")
        if "ta" in self.supported_languages and len(self.supported_languages) > 1:
            raise ValueError("Tamil cannot be combined with other supported languages")
        return self


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
    supported_languages: list[str] | None = Field(None, min_length=1)
    speech_rate: float | None = None
    greeting_message: str | None = None
    fallback_message: str | None = None
    max_call_duration_seconds: int | None = None
    transfer_number: str | None = None
    is_active: bool | None = None
    timezone: str | None = None

    @field_validator("supported_languages")
    @classmethod
    def normalize_supported_languages(cls, value: list[str] | None) -> list[str] | None:
        return _deduplicate_languages(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return _normalize_language(value) if value is not None else None


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


class SmallestSessionRequest(BaseModel):
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict)


class SmallestSessionResponse(BaseModel):
    access_token: str
    expires_in: int
    sample_rate: int = 24000


class VoiceCatalogItem(BaseModel):
    id: str
    name: str
    languages: list[str]
    accent: str | None = None
    gender: str | None = None
    age: str | None = None
    use_cases: list[str] = Field(default_factory=list)
    source: str = "catalog"


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


class AgentProviderCatalog(BaseModel):
    provider: str = "smallest"
    voice_model: str = "waves_lightning_v3_1"
    voices: list[VoiceCatalogItem]
    languages: list[LanguageCatalogItem]
    templates: list[AgentTemplate]


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
