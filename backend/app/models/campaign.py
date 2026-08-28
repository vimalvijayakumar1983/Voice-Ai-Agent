import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantScopedModel


class Campaign(TenantScopedModel):
    __tablename__ = "campaigns"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="SET NULL")
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(30), default="draft"
    )  # draft, scheduled, running, paused, completed, cancelled
    campaign_type: Mapped[str] = mapped_column(String(30), default="outbound")  # outbound, survey

    # Scheduling
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calling_hours_start: Mapped[str | None] = mapped_column(String(5))  # "09:00"
    calling_hours_end: Mapped[str | None] = mapped_column(String(5))  # "17:00"
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=5)
    retry_attempts: Mapped[int] = mapped_column(Integer, default=2)

    # Stats (denormalized for fast reads)
    total_contacts: Mapped[int] = mapped_column(Integer, default=0)
    completed_contacts: Mapped[int] = mapped_column(Integer, default=0)
    successful_contacts: Mapped[int] = mapped_column(Integer, default=0)

    settings: Mapped[dict | None] = mapped_column(JSONB)

    # Relationships
    contacts = relationship("CampaignContact", back_populates="campaign", lazy="noload")
    calls = relationship("Call", backref="campaign_ref", lazy="noload")


class CampaignContact(TenantScopedModel):
    __tablename__ = "campaign_contacts"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "phone_number",
            name="uq_campaign_contact_phone_number",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(30), default="pending"
    )  # pending, dispatching, calling, completed, failed, skipped, dnc, dispatch_unknown
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    context_data: Mapped[dict | None] = mapped_column(JSONB)  # custom fields for personalization

    # Relationships
    campaign = relationship("Campaign", back_populates="contacts")


class CampaignContactAttempt(TenantScopedModel):
    """A durable, single-dispatch identity for one campaign contact attempt.

    Provider calls are never made until this row and its local ``Call`` row are
    committed.  If a worker dies after the provider accepts a request, a retry
    sees the non-retryable dispatch state and waits for the signed provider
    callback to reconcile it instead of dialing the contact again.
    """

    __tablename__ = "campaign_contact_attempts"
    __table_args__ = (
        UniqueConstraint(
            "contact_id",
            "attempt_number",
            name="uq_campaign_contact_attempt_number",
        ),
        UniqueConstraint("idempotency_key", name="uq_campaign_attempt_idempotency_key"),
        UniqueConstraint("call_id", name="uq_campaign_attempt_call_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_contacts.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_call_sid: Mapped[str | None] = mapped_column(String(100), index=True)
    state: Mapped[str] = mapped_column(
        String(30), default="claimed", nullable=False, index=True
    )  # claimed, dispatching, accepted, completed, failed, rejected, unknown, cancelled
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    contact = relationship("CampaignContact", lazy="noload")
    call = relationship("Call", lazy="noload")


class ProviderCallbackOutbox(TenantScopedModel):
    """Durable follow-up work produced by a best-effort provider webhook."""

    __tablename__ = "provider_callback_outbox"
    __table_args__ = (UniqueConstraint("event_key", name="uq_provider_callback_outbox_event_key"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
