"""Tenant-owned custom voice metadata.

Reference audio is sent directly to the configured speech provider and is
never persisted by VAV. This table is the entitlement boundary that prevents
one tenant from discovering or selecting another tenant's private clone when
the provider account is shared.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantScopedModel


class VoiceClone(TenantScopedModel):
    __tablename__ = "voice_clones"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50), default="smallest")
    provider_voice_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(63), default="en")
    accent: Mapped[str | None] = mapped_column(String(100))
    gender: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100), default="lightning-v3.1")
    model_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="creating", index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
