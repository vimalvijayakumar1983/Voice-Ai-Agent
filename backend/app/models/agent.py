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
    disposition_profile: Mapped[str] = mapped_column(String(30), default="general")
    post_call_analysis_mode: Mapped[str] = mapped_column(String(30), default="provider_first")

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

    @property
    def knowledge_company_scope(self) -> dict | None:
        return (self.agent_metadata or {}).get("knowledge_company_scope")

    @knowledge_company_scope.setter
    def knowledge_company_scope(self, value: dict | None) -> None:
        self.agent_metadata = {**(self.agent_metadata or {}), "knowledge_company_scope": value}

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
    # Points at one immutable, source-revision-stamped speech lexicon.  New
    # source revisions create a new artifact and approval atomically swaps this
    # pointer; historical artifacts are retained for audit and call replay.
    speech_lexicon_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "knowledge_speech_lexicons.id",
            name="fk_knowledge_bases_speech_lexicon_artifact_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        index=True,
    )
    # Atomic blue/green publication pointer. Draft source rows may continue to
    # change while calls read the immutable serving revision referenced here.
    serving_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "knowledge_serving_revisions.id",
            name="fk_knowledge_bases_serving_revision_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        index=True,
    )
    # Explicit revocation fence for calls that reserve a serving revision before
    # joining LiveKit. Ordinary blue/green publication leaves this generation
    # unchanged, while unapproval increments it so a pre-admission reservation
    # cannot begin speaking after access was revoked.
    serving_revocation_generation: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

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
    crawls = relationship(
        "KnowledgeCrawl",
        back_populates="knowledge_base",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="KnowledgeCrawl.created_at.desc()",
    )
    speech_lexicons = relationship(
        "KnowledgeSpeechLexicon",
        back_populates="knowledge_base",
        foreign_keys="KnowledgeSpeechLexicon.knowledge_base_id",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    speech_lexicon = relationship(
        "KnowledgeSpeechLexicon",
        primaryjoin=(
            "foreign(KnowledgeBase.speech_lexicon_artifact_id) == KnowledgeSpeechLexicon.id"
        ),
        lazy="selectin",
        viewonly=True,
    )
    serving_revisions = relationship(
        "KnowledgeServingRevision",
        back_populates="knowledge_base",
        foreign_keys="KnowledgeServingRevision.knowledge_base_id",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    serving_revision = relationship(
        "KnowledgeServingRevision",
        primaryjoin=("foreign(KnowledgeBase.serving_revision_id) == KnowledgeServingRevision.id"),
        lazy="selectin",
        viewonly=True,
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
    # Website sources keep the immutable extraction separately from the
    # retrieval document produced by the knowledge compiler.  This makes the
    # generated representation auditable and lets a new compiler version
    # rebuild knowledge without downloading the page again.
    raw_content: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    structured_content: Mapped[dict | None] = mapped_column(JSONB)
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    provider_item_id: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    knowledge_base = relationship("KnowledgeBase", back_populates="sources")
    crawl_pages = relationship(
        "KnowledgeCrawlPage",
        back_populates="knowledge_source",
        lazy="noload",
    )


class KnowledgeProviderCleanup(TenantScopedModel):
    """Durable deletion intent for a provider artifact that must not be served.

    Rows stay present until the remote delete is confirmed.  Provider-backed
    binding and publication paths therefore have one transactional, fail-closed
    signal instead of depending on a best-effort worker log entry.
    """

    __tablename__ = "knowledge_provider_cleanups"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_knowledge_base_id",
            "provider_item_id",
            name="uq_knowledge_provider_cleanup_artifact",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Deleting a local knowledge base must not discard the only durable record
    # of an already-created remote artifact.
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        index=True,
    )
    # A failed/removed source must not erase cleanup work for its remote item.
    knowledge_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"),
        index=True,
    )
    repair_run_id: Mapped[str | None] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_knowledge_base_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_item_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_artifact_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class KnowledgeSpeechLexicon(TenantScopedModel):
    """Immutable speech-recognition artifact for one approved KB revision.

    The row is append-only by service contract.  ``KnowledgeBase`` owns the
    mutable publication pointer, so a call can always identify exactly which
    source revision and compiler output supplied its recognition vocabulary.
    """

    __tablename__ = "knowledge_speech_lexicons"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "source_revision_sha256",
            "compiler_version",
            name="uq_knowledge_speech_lexicon_revision",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
    )
    source_revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entries: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    source_revisions: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    coverage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    knowledge_base = relationship(
        "KnowledgeBase",
        back_populates="speech_lexicons",
        foreign_keys=[knowledge_base_id],
    )


class KnowledgeServingRevision(TenantScopedModel):
    """Immutable, fully self-contained knowledge release served to callers."""

    __tablename__ = "knowledge_serving_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "content_sha256",
            "compiler_version",
            name="uq_knowledge_serving_revision_content",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
    )
    speech_lexicon_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_speech_lexicons.id", ondelete="RESTRICT"),
        index=True,
    )
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    knowledge_name: Mapped[str] = mapped_column(String(255), nullable=False)
    knowledge_description: Mapped[str | None] = mapped_column(Text)
    knowledge_content: Mapped[str | None] = mapped_column(Text)
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_label: Mapped[str | None] = mapped_column(String(255))
    languages: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_knowledge_base_id: Mapped[str | None] = mapped_column(String(100))
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    knowledge_base = relationship(
        "KnowledgeBase",
        back_populates="serving_revisions",
        foreign_keys=[knowledge_base_id],
    )
    sources = relationship(
        "KnowledgeServingRevisionSource",
        back_populates="serving_revision",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    speech_lexicon = relationship("KnowledgeSpeechLexicon", lazy="noload", viewonly=True)


class KnowledgeServingRevisionSource(TenantScopedModel):
    """Immutable copy of one compiled source inside a serving revision."""

    __tablename__ = "knowledge_serving_revision_sources"
    __table_args__ = (
        UniqueConstraint(
            "serving_revision_id",
            "original_source_id",
            name="uq_knowledge_serving_revision_source",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    serving_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_serving_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
    )
    original_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_content: Mapped[dict | None] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict | None] = mapped_column(JSONB)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)

    serving_revision = relationship("KnowledgeServingRevision", back_populates="sources")


class KnowledgeCrawl(TenantScopedModel):
    """One durable, user-visible whole-site crawl operation."""

    __tablename__ = "knowledge_crawls"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    root_url: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_host: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    max_pages: Mapped[int] = mapped_column(Integer, default=100)
    max_depth: Mapped[int] = mapped_column(Integer, default=3)
    include_subdomains: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    options: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    knowledge_base = relationship("KnowledgeBase", back_populates="crawls")
    pages = relationship(
        "KnowledgeCrawlPage",
        back_populates="crawl",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class KnowledgeCrawlPage(TenantScopedModel):
    """Page-level crawl ledger used for progress, retries, and auditability."""

    __tablename__ = "knowledge_crawl_pages"
    __table_args__ = (
        UniqueConstraint("crawl_id", "canonical_url", name="uq_knowledge_crawl_page_url"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    crawl_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_crawls.id", ondelete="CASCADE"), index=True
    )
    knowledge_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"),
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    discovered_via: Mapped[str] = mapped_column(String(30), default="link")
    status: Mapped[str] = mapped_column(String(30), default="discovered", index=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    crawl = relationship("KnowledgeCrawl", back_populates="pages")
    knowledge_source = relationship("KnowledgeSource", back_populates="crawl_pages")


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
