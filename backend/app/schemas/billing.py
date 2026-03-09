from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class BillingPlanResponse(BaseModel):
    id: UUID
    name: str
    base_price_cents: int
    included_minutes: int
    per_minute_cents: int
    max_agents: int
    max_concurrent_calls: int
    features: dict | None

    model_config = {"from_attributes": True}


class SubscriptionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    plan_id: UUID
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None

    model_config = {"from_attributes": True}


class UsageSummary(BaseModel):
    period_start: datetime
    period_end: datetime
    total_minutes: float
    total_ai_tokens: int
    total_cost_cents: int
    included_minutes: int
    overage_minutes: float
    overage_cost_cents: int
