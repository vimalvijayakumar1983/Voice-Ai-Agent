import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantScopedModel


class CommerceSession(TenantScopedModel):
    __tablename__ = "commerce_sessions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[str] = mapped_column(String(30), default="web_voice")
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    cart_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    browser_checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Browser storage and customer PII are authenticated-encrypted; neither is
    # exposed by response schemas or written into audit-event details.
    encrypted_context: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[str | None] = mapped_column(String(30))
    cart_fingerprint: Mapped[str | None] = mapped_column(String(64))
    confirmation_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    order_reference: Mapped[str | None] = mapped_column(String(120))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    actions = relationship(
        "CommerceAction",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="CommerceAction.created_at",
        passive_deletes=True,
    )


class CommerceAction(TenantScopedModel):
    __tablename__ = "commerce_actions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_commerce_action_idempotency"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    result_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    session = relationship("CommerceSession", back_populates="actions")
