import uuid

from sqlalchemy import ForeignKey, String, Boolean, Text, Float, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
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
    model_provider: Mapped[str] = mapped_column(String(50), default="openai")  # openai, anthropic
    model_name: Mapped[str] = mapped_column(String(100), default="gpt-4o")
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    max_tokens: Mapped[int] = mapped_column(Integer, default=500)

    # Voice Configuration
    voice_provider: Mapped[str] = mapped_column(String(50), default="twilio")
    voice_id: Mapped[str] = mapped_column(String(100), default="en-US-Standard-A")
    language: Mapped[str] = mapped_column(String(10), default="en-US")
    speech_rate: Mapped[float] = mapped_column(Float, default=1.0)

    # Behavior
    greeting_message: Mapped[str | None] = mapped_column(Text)
    fallback_message: Mapped[str | None] = mapped_column(Text)
    max_call_duration_seconds: Mapped[int] = mapped_column(Integer, default=600)
    transfer_number: Mapped[str | None] = mapped_column(String(20))
    metadata: Mapped[dict | None] = mapped_column(JSONB)

    # Relationships
    tenant = relationship("Tenant", back_populates="agents")
    knowledge_bases = relationship("KnowledgeBase", back_populates="agent", lazy="selectin")
    calls = relationship("Call", back_populates="agent", lazy="noload")


class KnowledgeBase(TenantScopedModel):
    __tablename__ = "knowledge_bases"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50))  # text, url, file
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    agent = relationship("Agent", back_populates="knowledge_bases")
