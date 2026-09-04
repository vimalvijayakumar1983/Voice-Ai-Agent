from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.services.integration_security import (
    IntegrationConfigError,
    validate_public_https_url,
)

KnowledgeScope = Literal["workspace", "group", "division", "branch", "department"]
KnowledgeStatus = Literal["local_only", "provisioning", "processing", "ready", "error"]
SourceType = Literal["website", "sitemap", "url", "file", "text"]
SourceStatus = Literal["pending", "processing", "indexed", "failed", "local_only"]
KnowledgeProcessingMode = Literal["automatic", "fast", "ai_verified"]


def _clean_list(values: list[str], *, maximum: int, item_maximum: int) -> list[str]:
    cleaned = []
    for value in values:
        item = value.strip()
        if item and item not in cleaned:
            cleaned.append(item[:item_maximum])
    return cleaned[:maximum]


class KnowledgeBaseCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=40)]
    description: Annotated[str, Field(max_length=1000)] = ""
    scope_type: KnowledgeScope = "workspace"
    scope_label: Annotated[str | None, Field(max_length=255)] = None
    languages: list[str] = Field(default_factory=lambda: ["en"], max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name", "description")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("languages")
    @classmethod
    def clean_languages(cls, value: list[str]) -> list[str]:
        return _clean_list(value, maximum=20, item_maximum=20) or ["en"]

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return _clean_list(value, maximum=20, item_maximum=40)


class KnowledgeAIDraftRequest(BaseModel):
    brief: Annotated[str, Field(min_length=20, max_length=4000)]
    scope_preference: Literal["auto", "workspace", "group", "division", "branch", "department"] = (
        "auto"
    )
    languages: list[str] = Field(
        default_factory=lambda: ["en"],
        min_length=1,
        max_length=20,
    )

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_primary_language(cls, value: object) -> object:
        # Keep the previous single-language request compatible during rolling deploys.
        if isinstance(value, dict) and "languages" not in value and "primary_language" in value:
            migrated = dict(value)
            migrated["languages"] = migrated.pop("primary_language")
            return migrated
        return value

    @field_validator("languages", mode="before")
    @classmethod
    def accept_language_list_or_legacy_primary(cls, value: object) -> object:
        if isinstance(value, str):
            return value.split(",")
        return value

    @field_validator("languages")
    @classmethod
    def clean_languages(cls, values: list[str]) -> list[str]:
        cleaned = _clean_list(
            [value.lower().replace("_", "-") for value in values],
            maximum=20,
            item_maximum=20,
        )
        for language in cleaned:
            parts = language.split("-")
            if not (2 <= len(parts[0]) <= 3 and parts[0].isalpha()) or not all(
                2 <= len(part) <= 8 and part.isalnum() for part in parts[1:]
            ):
                raise ValueError("Use valid language codes such as en, ar, hi, or en-GB.")
        if not cleaned:
            raise ValueError("Enter at least one language code.")
        return cleaned


class KnowledgeAIDraftResponse(BaseModel):
    draft: KnowledgeBaseCreate
    rationale: Annotated[str, Field(max_length=1500)]
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    recommended_sources: list[str] = Field(default_factory=list, max_length=8)
    model: str


class KnowledgeBaseUpdate(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    description: Annotated[str | None, Field(max_length=1000)] = None
    scope_type: KnowledgeScope | None = None
    scope_label: Annotated[str | None, Field(max_length=255)] = None
    languages: list[str] | None = Field(default=None, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=20)

    @field_validator("name", "description")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class KnowledgeSourceResponse(BaseModel):
    id: UUID
    knowledge_base_id: UUID
    source_type: SourceType
    name: str
    location: str | None
    mime_type: str | None
    size_bytes: int | None
    status: SourceStatus
    provider_item_id: str | None
    error_message: str | None
    source_metadata: dict | None
    retrieval_ready: bool = False
    extracted_character_count: int = 0
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeAgentBindingResponse(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str
    knowledge_base_id: UUID
    sync_status: str
    last_synced_at: datetime | None


class KnowledgeCrawlPageResponse(BaseModel):
    id: UUID
    knowledge_source_id: UUID | None
    url: str
    canonical_url: str
    depth: int
    discovered_via: str
    status: str
    error_code: str | None
    error_message: str | None
    retry_count: int
    last_attempted_at: datetime | None

    model_config = {"from_attributes": True}


class KnowledgeCrawlResponse(BaseModel):
    id: UUID
    knowledge_base_id: UUID
    root_url: str
    allowed_host: str
    status: str
    max_pages: int
    max_depth: int
    include_subdomains: bool
    discovered_count: int
    queued_count: int
    indexed_count: int
    failed_count: int
    skipped_count: int
    options: dict | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    pages: list[KnowledgeCrawlPageResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeSpeechLexiconResponse(BaseModel):
    artifact_id: UUID
    compiler_version: str
    source_revision_sha256: str
    content_sha256: str
    generated_at: datetime
    source_count: int
    entry_count: int
    coverage: dict[str, int | float]


class KnowledgeServingRevisionResponse(BaseModel):
    revision_id: UUID
    compiler_version: str
    source_revision_sha256: str
    chunk_revision_sha256: str
    fact_revision_sha256: str
    entity_revision_sha256: str
    content_sha256: str
    published_at: datetime
    source_count: int
    chunk_count: int
    fact_count: int
    entity_count: int
    speech_lexicon_artifact_id: UUID


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    provider: str
    provider_knowledge_base_id: str | None
    sync_status: KnowledgeStatus
    sync_error: str | None
    approval_status: Literal["draft", "approved"]
    scope_type: KnowledgeScope
    scope_label: str | None
    languages: list[str]
    tags: list[str]
    source_count: int
    indexed_source_count: int
    last_synced_at: datetime | None
    published_at: datetime | None
    speech_lexicon: KnowledgeSpeechLexiconResponse | None = None
    serving_revision: KnowledgeServingRevisionResponse | None = None
    has_pending_changes: bool = False
    sources: list[KnowledgeSourceResponse] = Field(default_factory=list)
    agent_bindings: list[KnowledgeAgentBindingResponse] = Field(default_factory=list)
    crawls: list[KnowledgeCrawlResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class UrlSourceCreate(BaseModel):
    urls: Annotated[list[HttpUrl], Field(min_length=1, max_length=100)]

    @field_validator("urls")
    @classmethod
    def require_public_https_urls(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        for value in values:
            _validate_knowledge_url(value)
        return values


class SitemapDiscoveryRequest(BaseModel):
    sitemap_url: HttpUrl

    @field_validator("sitemap_url")
    @classmethod
    def require_public_https_url(cls, value: HttpUrl) -> HttpUrl:
        _validate_knowledge_url(value)
        return value


class SitemapDiscoveryResponse(BaseModel):
    urls: list[str]


class KnowledgeCrawlCreate(BaseModel):
    homepage_url: HttpUrl
    max_pages: int = Field(default=100, ge=1, le=500)
    max_depth: int = Field(default=3, ge=0, le=8)
    include_subdomains: bool = False
    processing_mode: KnowledgeProcessingMode = "automatic"

    @field_validator("homepage_url")
    @classmethod
    def require_public_homepage(cls, value: HttpUrl) -> HttpUrl:
        _validate_knowledge_url(value)
        return value


class TextSourceCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    content: Annotated[str, Field(min_length=20, max_length=100_000)]

    @field_validator("name", "content")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()


class AgentKnowledgeBindRequest(BaseModel):
    agent_id: UUID


class KnowledgeApprovalRequest(BaseModel):
    approved: bool


class KnowledgeReleaseReactivationRequest(BaseModel):
    """Compare-and-swap contract for an audited historical release restore."""

    expected_current_revision_id: UUID | None
    reason: Annotated[str, Field(min_length=3, max_length=500)]

    model_config = {"extra": "forbid"}

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def _validate_knowledge_url(value: HttpUrl) -> None:
    try:
        validate_public_https_url(str(value))
    except IntegrationConfigError as exc:
        message = str(exc).replace("Integration URL", "Knowledge source URL")
        raise ValueError(message) from exc
