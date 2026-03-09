import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Boolean, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantScopedModel


class Integration(TenantScopedModel):
    __tablename__ = "integrations"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False)  # webhook, crm, zapier
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # webhook config: {"url": "...", "secret": "...", "events": ["call.completed", "campaign.finished"]}
    # crm config: {"provider": "hubspot", "api_key": "...", "sync_contacts": true}
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class WebhookEvent(TenantScopedModel):
    __tablename__ = "webhook_events"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending, sent, failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
