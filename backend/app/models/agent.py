import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantScopedModel


class Agent(TenantScopedModel):
    __tablename__ = "agents"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # AI Configuration
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(50), default="smallest")
    model_name: Mapped[str] = mapped_column(String(100), default="electron")
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    max_tokens: Mapped[int] = mapped_column(Integer, default=500)

    # Voice Configuration
    voice_provider: Mapped[str] = mapped_column(String(50), default="smallest")
    voice_id: Mapped[str] = mapped_column(String(100), default="")
    language: Mapped[str] = mapped_column(String(63), default="en")
    supported_languages: Mapped[list[str]] = mapped_column(JSONB, default=lambda: ["en"])
    language_switching_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    language_switching_mode: Mapped[str] = mapped_column(String(20), default="disabled")
    speech_rate: Mapped[float] = mapped_column(Float, default=1.0)

    # Smallest.ai provider state. Secrets are never stored on the agent.
    provider_agent_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    provider_branch_id: Mapped[str | None] = mapped_column(String(100))
    provider_revision_id: Mapped[str | None] = mapped_column(String(100))
    provider_config: Mapped[dict | None] = mapped_column(JSONB)
    sync_status: Mapped[str] = mapped_column(String(30), default="local_only")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Dubai")

    # Behavior
    greeting_message: Mapped[str | None] = mapped_column(Text)
    fallback_message: Mapped[str | None] = mapped_column(Text)
    max_call_duration_seconds: Mapped[int] = mapped_column(Integer, default=600)
    transfer_number: Mapped[str | None] = mapped_column(String(20))
    agent_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)

    # Relationships
    tenant = relationship("Tenant", back_populates="agents")
    knowledge_bases = relationship(
        "KnowledgeBase",
        back_populates="legacy_agent",
        foreign_keys="KnowledgeBase.agent_id",
        lazy="selectin",
    )
    knowledge_binding = relationship(
        "AgentKnowledgeBinding",
        back_populates="agent",
        uselist=False,
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    calls = relationship("Call", back_populates="agent", lazy="noload")
    runtime_profile = relationship(
        "AgentRuntimeProfile",
        back_populates="agent",
        uselist=False,
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class AgentRuntimeProfile(TenantScopedModel):
    """Provider-neutral serving policy for calls handled by VAV itself."""

    __tablename__ = "agent_runtime_profiles"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_agent_runtime_profiles_agent_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    telephony_provider: Mapped[str] = mapped_column(String(30), default="twilio")
    primary_speech_provider: Mapped[str] = mapped_column(String(30), default="sarvam")
    fallback_speech_provider: Mapped[str | None] = mapped_column(String(30))
    llm_provider: Mapped[str] = mapped_column(String(30), default="openai")
    llm_model: Mapped[str] = mapped_column(String(100), default="gpt-4o-mini")
    stt_language: Mapped[str] = mapped_column(String(30), default="auto")
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=1)
    daily_call_limit: Mapped[int] = mapped_column(Integer, default=100)
    monthly_budget_cents: Mapped[int] = mapped_column(Integer, default=5000)
    assigned_numbers: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_config: Mapped[dict | None] = mapped_column("config", JSONB)

    agent = relationship("Agent", back_populates="runtime_profile")


class KnowledgeBase(TenantScopedModel):
    __tablename__ = "knowledge_bases"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Kept nullable for backwards compatibility with the original per-agent
    # scaffold. New knowledge bases are workspace resources and use bindings.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(50))
    content: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    provider: Mapped[str] = mapped_column(String(50), default="smallest")
    provider_knowledge_base_id: Mapped[str | None] = mapped_column(String(100), index=True)
    sync_status: Mapped[str] = mapped_column(String(30), default="local_only")
    sync_error: Mapped[str | None] = mapped_column(Text)
    approval_status: Mapped[str] = mapped_column(String(30), default="draft")
    scope_type: Mapped[str] = mapped_column(String(30), default="workspace")
    scope_label: Mapped[str | None] = mapped_column(String(255))
    languages: Mapped[list[str]] = mapped_column(JSONB, default=lambda: ["en"])
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_source_count: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    legacy_agent = relationship("Agent", back_populates="knowledge_bases", foreign_keys=[agent_id])
    sources = relationship(
        "KnowledgeSource",
        back_populates="knowledge_base",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    agent_bindings = relationship(
        "AgentKnowledgeBinding",
        back_populates="knowledge_base",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class KnowledgeSource(TenantScopedModel):
    __tablename__ = "knowledge_sources"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    file_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    provider_item_id: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    knowledge_base = relationship("KnowledgeBase", back_populates="sources")


class AgentKnowledgeBinding(TenantScopedModel):
    __tablename__ = "agent_knowledge_bindings"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_agent_knowledge_bindings_agent_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50), default="smallest")
    sync_status: Mapped[str] = mapped_column(String(30), default="pending")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent = relationship("Agent", back_populates="knowledge_binding")
    knowledge_base = relationship("KnowledgeBase", back_populates="agent_bindings")
