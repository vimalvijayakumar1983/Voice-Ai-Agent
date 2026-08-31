from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.services.integration_security import (
    IntegrationConfigError,
    validate_public_https_url,
)

KnowledgeScope = Literal["workspace", "group", "division", "branch", "department"]
KnowledgeStatus = Literal["local_only", "provisioning", "processing", "ready", "error"]
SourceType = Literal["website", "sitemap", "url", "file", "text"]
SourceStatus = Literal["pending", "processing", "indexed", "failed", "local_only"]


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
    sources: list[KnowledgeSourceResponse] = Field(default_factory=list)
    agent_bindings: list[KnowledgeAgentBindingResponse] = Field(default_factory=list)
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


def _validate_knowledge_url(value: HttpUrl) -> None:
    try:
        validate_public_https_url(str(value))
    except IntegrationConfigError as exc:
        message = str(exc).replace("Integration URL", "Knowledge source URL")
        raise ValueError(message) from exc
