import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantScopedModel


class Call(TenantScopedModel):
    __tablename__ = "calls"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )

    # Call metadata
    direction: Mapped[str] = mapped_column(String(10))  # inbound, outbound
    status: Mapped[str] = mapped_column(
        String(30), default="initiated"
    )  # initiated, ringing, in_progress, completed, failed, no_answer, busy
    from_number: Mapped[str] = mapped_column(String(20), nullable=False)
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)

    # Telephony provider data
    provider: Mapped[str] = mapped_column(String(30), default="twilio")
    provider_call_sid: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    provider_recording_url: Mapped[str | None] = mapped_column(Text)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    # Cost tracking
    cost_cents: Mapped[int | None] = mapped_column(Integer)

    # Outcome
    # interested, not_interested, callback, dnc, voicemail
    disposition: Mapped[str | None] = mapped_column(String(50))
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    call_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)

    # Relationships
    agent = relationship("Agent", back_populates="calls")
    transcript = relationship(
        "CallTranscript", back_populates="call", uselist=False, lazy="selectin"
    )
    summary = relationship("CallSummary", back_populates="call", uselist=False, lazy="selectin")


class CallTranscript(TenantScopedModel):
    __tablename__ = "call_transcripts"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), unique=True
    )
    turns: Mapped[dict] = mapped_column(JSONB, nullable=False)  # [{role, content, timestamp}]
    full_text: Mapped[str | None] = mapped_column(Text)

    # Relationships
    call = relationship("Call", back_populates="transcript")


class CallSummary(TenantScopedModel):
    __tablename__ = "call_summaries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), unique=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_topics: Mapped[dict | None] = mapped_column(JSONB)  # ["topic1", "topic2"]
    action_items: Mapped[dict | None] = mapped_column(JSONB)  # ["item1", "item2"]
    sentiment: Mapped[str | None] = mapped_column(String(20))  # positive, neutral, negative

    # Relationships
    call = relationship("Call", back_populates="summary")
