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


class CostCurrencySnapshot(BaseModel):
    display: list[str]
    usd_to_aed: float
    inr_to_aed: float
    fx_effective_date: str
    source_url: str
    notes: str


class CostReportSummary(BaseModel):
    total_calls: int
    answered_calls: int
    completed_calls: int
    successful_calls: int
    total_minutes: float
    avg_duration_seconds: float
    answer_rate: float
    success_rate: float
    estimated_cost_usd: float
    estimated_cost_aed: float
    avg_cost_per_call_usd: float
    avg_cost_per_call_aed: float
    cost_per_minute_usd: float
    cost_per_minute_aed: float
    priced_calls: int
    fully_priced_calls: int
    unpriced_calls: int
    cost_coverage: float
    full_cost_coverage: float
    ledger_estimate_usd: float
    ledger_estimate_aed: float
    calls_by_status: dict[str, int]
    calls_by_direction: dict[str, int]


class ProviderCostBreakdown(BaseModel):
    provider: str
    service: str
    calls: int
    quantity: float
    unit: str
    cost_usd: float
    cost_aed: float
    source_url: str
    basis: str


class CostTrendPoint(BaseModel):
    date: str
    calls: int
    minutes: float
    cost_usd: float
    cost_aed: float


class CostComponent(BaseModel):
    provider: str
    service: str
    quantity: float
    unit: str
    rate_usd: float
    cost_usd: float
    cost_aed: float
    source_url: str
    basis: str


class CallCostRow(BaseModel):
    call_id: str
    created_at: str
    agent_id: str | None
    agent_name: str
    direction: str
    status: str
    disposition: str | None
    telephony_provider: str
    speech_provider: str | None
    from_number: str
    to_number: str
    duration_seconds: int
    cost_usd: float
    cost_aed: float
    ledger_cost_usd: float
    ledger_cost_aed: float
    cost_state: str
    pricing_completeness: str
    missing_cost_inputs: list[str]
    components: list[CostComponent]


class ProviderRateCard(BaseModel):
    provider: str
    service: str
    native_amount: float
    native_currency: str
    unit: str
    usd: float
    aed: float
    source_url: str
    effective_date: str
    notes: str


class CostMethodology(BaseModel):
    primary_total: str
    not_included: str
    invoice_status: str


class CostReport(BaseModel):
    period_start: str
    period_end: str
    currency: CostCurrencySnapshot
    summary: CostReportSummary
    provider_breakdown: list[ProviderCostBreakdown]
    trend: list[CostTrendPoint]
    calls: list[CallCostRow]
    rate_cards: list[ProviderRateCard]
    methodology: CostMethodology
