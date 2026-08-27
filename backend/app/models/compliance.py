import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantScopedModel


class DncEntry(TenantScopedModel):
    """Do Not Call registry per tenant."""

    __tablename__ = "dnc_entries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(
        String(100)
    )  # customer_request, regulatory, complaint
    source: Mapped[str | None] = mapped_column(String(50))  # manual, api, call_disposition
    added_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ConsentRecord(TenantScopedModel):
    """Track consent for recording, outbound calls, etc."""

    __tablename__ = "consent_records"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    consent_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # recording, outbound_call, data_processing
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # granted, revoked
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict | None] = mapped_column(JSONB)  # {"call_id": "...", "method": "verbal"}
