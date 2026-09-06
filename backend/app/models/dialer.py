"""Customer workspace and durable dialer queue, separate from shared knowledge."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantScopedModel


class DialerBase(TenantScopedModel):
    __abstract__ = True

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )


class DialerCustomer(DialerBase):
    __tablename__ = "dialer_customers"
    __table_args__ = (UniqueConstraint("tenant_id", "company", "phone_number"),)

    company: Mapped[str] = mapped_column(String(160))
    phone_number: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(160))
    language: Mapped[str] = mapped_column(String(30), default="en")
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Dubai")
    notes: Mapped[str] = mapped_column(Text, default="")
    contact_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_reference: Mapped[str] = mapped_column(String(500), default="")
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)


class DialerCampaign(DialerBase):
    __tablename__ = "dialer_campaigns"

    name: Mapped[str] = mapped_column(String(255))
    company: Mapped[str] = mapped_column(String(160))
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(30))
    purpose: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)


class DialerJob(DialerBase):
    __tablename__ = "dialer_jobs"
    __table_args__ = (UniqueConstraint("campaign_id", "event_key"),)

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialer_campaigns.id"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialer_customers.id"), index=True
    )
    event_key: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Reserved deterministic ID can precede the Call row; intentionally no FK.
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    call_ids: Mapped[list] = mapped_column(JSONB, default=list)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(String(250))
