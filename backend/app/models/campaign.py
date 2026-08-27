import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
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
    )  # pending, calling, completed, failed, skipped, dnc
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    context_data: Mapped[dict | None] = mapped_column(JSONB)  # custom fields for personalization

    # Relationships
    campaign = relationship("Campaign", back_populates="contacts")
