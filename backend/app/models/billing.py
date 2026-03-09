import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Boolean, Integer, Float
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel, TenantScopedModel


class BillingPlan(BaseModel):
    """Platform-wide billing plans."""

    __tablename__ = "billing_plans"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(String(100))
    base_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    included_minutes: Mapped[int] = mapped_column(Integer, default=0)
    per_minute_cents: Mapped[int] = mapped_column(Integer, default=5)  # overage rate
    max_agents: Mapped[int] = mapped_column(Integer, default=5)
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=10)
    features: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class TenantSubscription(TenantScopedModel):
    __tablename__ = "tenant_subscriptions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), unique=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_plans.id")
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="active")  # active, past_due, cancelled, trialing
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageRecord(TenantScopedModel):
    __tablename__ = "usage_records"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    usage_type: Mapped[str] = mapped_column(String(50), nullable=False)  # call_minutes, ai_tokens, tts_chars
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)  # minutes, tokens, characters
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
