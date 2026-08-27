"""Billing and usage endpoints."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.billing import BillingPlan, TenantSubscription, UsageRecord
from app.schemas.billing import BillingPlanResponse, SubscriptionResponse, UsageSummary

router = APIRouter(prefix="/billing", tags=["Billing"])


@router.get("/plans", response_model=list[BillingPlanResponse])
async def list_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BillingPlan).where(BillingPlan.is_active.is_(True)))
    return [BillingPlanResponse.model_validate(p) for p in result.scalars().all()]


@router.get("/subscription", response_model=SubscriptionResponse | None)
async def get_subscription(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TenantSubscription).where(TenantSubscription.tenant_id == current_user.tenant_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return None
    return SubscriptionResponse.model_validate(sub)


@router.get("/usage", response_model=UsageSummary)
async def get_usage(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    since = datetime.now(UTC) - timedelta(days=days)
    now = datetime.now(UTC)

    result = await db.execute(
        select(
            func.coalesce(
                func.sum(UsageRecord.quantity).filter(UsageRecord.usage_type == "call_minutes"), 0
            ).label("total_minutes"),
            func.coalesce(
                func.sum(UsageRecord.quantity).filter(UsageRecord.usage_type == "ai_tokens"), 0
            ).label("total_tokens"),
            func.coalesce(func.sum(UsageRecord.cost_cents), 0).label("total_cost"),
        ).where(
            and_(
                UsageRecord.tenant_id == current_user.tenant_id,
                UsageRecord.created_at >= since,
            )
        )
    )
    row = result.one()

    # Get plan's included minutes
    sub_result = await db.execute(
        select(TenantSubscription, BillingPlan)
        .join(BillingPlan, TenantSubscription.plan_id == BillingPlan.id)
        .where(TenantSubscription.tenant_id == current_user.tenant_id)
    )
    sub_row = sub_result.first()
    included_minutes = sub_row[1].included_minutes if sub_row else 0
    per_minute_cents = sub_row[1].per_minute_cents if sub_row else 5

    total_minutes = float(row.total_minutes)
    overage = max(0, total_minutes - included_minutes)

    return UsageSummary(
        period_start=since,
        period_end=now,
        total_minutes=total_minutes,
        total_ai_tokens=int(row.total_tokens),
        total_cost_cents=int(row.total_cost),
        included_minutes=included_minutes,
        overage_minutes=overage,
        overage_cost_cents=int(overage * per_minute_cents),
    )
