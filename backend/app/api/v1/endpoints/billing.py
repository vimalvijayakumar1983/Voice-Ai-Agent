"""Billing, provider-cost, and usage-reporting endpoints."""

import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.billing import BillingPlan, TenantSubscription, UsageRecord
from app.schemas.billing import BillingPlanResponse, CostReport, SubscriptionResponse, UsageSummary
from app.services.cost_reporting import build_cost_report

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


async def _cost_report(
    db: AsyncSession,
    current_user: CurrentUser,
    *,
    days: int,
    provider: str | None,
    speech_provider: str | None,
    agent_id: UUID | None,
    direction: str | None,
    status: str | None,
) -> dict:
    until = datetime.now(UTC)
    return await build_cost_report(
        db,
        tenant_id=current_user.tenant_id,
        since=until - timedelta(days=days),
        until=until,
        provider=provider,
        speech_provider=speech_provider,
        agent_id=agent_id,
        direction=direction,
        status=status,
    )


@router.get("/cost-report", response_model=CostReport)
async def get_cost_report(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    provider: str | None = Query(
        None,
        pattern="^(twilio|smallest|livekit_sip|livekit_webrtc)$",
    ),
    speech_provider: str | None = Query(None, pattern="^(inworld|sarvam|elevenlabs|smallest)$"),
    agent_id: UUID | None = None,
    direction: str | None = Query(None, pattern="^(inbound|outbound)$"),
    status: str | None = Query(None, max_length=30),
):
    """Return traceable provider estimates and operational call metrics."""
    return await _cost_report(
        db,
        current_user,
        days=days,
        provider=provider,
        speech_provider=speech_provider,
        agent_id=agent_id,
        direction=direction,
        status=status,
    )


@router.get("/cost-report.csv")
async def export_cost_report(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    provider: str | None = Query(
        None,
        pattern="^(twilio|smallest|livekit_sip|livekit_webrtc)$",
    ),
    speech_provider: str | None = Query(None, pattern="^(inworld|sarvam|elevenlabs|smallest)$"),
    agent_id: UUID | None = None,
    direction: str | None = Query(None, pattern="^(inbound|outbound)$"),
    status: str | None = Query(None, max_length=30),
):
    """Export the filtered call-cost ledger in a finance-friendly CSV."""
    report = await _cost_report(
        db,
        current_user,
        days=days,
        provider=provider,
        speech_provider=speech_provider,
        agent_id=agent_id,
        direction=direction,
        status=status,
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "call_id",
            "created_at",
            "agent_name",
            "direction",
            "status",
            "disposition",
            "telephony_provider",
            "speech_provider",
            "from_number",
            "to_number",
            "duration_seconds",
            "estimated_cost_usd",
            "estimated_cost_aed",
            "ledger_estimate_usd",
            "ledger_estimate_aed",
            "cost_state",
            "pricing_completeness",
            "missing_cost_inputs",
        ],
    )
    writer.writeheader()
    for call in report["calls"]:
        writer.writerow(
            {
                "call_id": call["call_id"],
                "created_at": call["created_at"],
                "agent_name": call["agent_name"],
                "direction": call["direction"],
                "status": call["status"],
                "disposition": call["disposition"] or "",
                "telephony_provider": call["telephony_provider"],
                "speech_provider": call["speech_provider"] or "",
                "from_number": call["from_number"],
                "to_number": call["to_number"],
                "duration_seconds": call["duration_seconds"],
                "estimated_cost_usd": call["cost_usd"],
                "estimated_cost_aed": call["cost_aed"],
                "ledger_estimate_usd": call["ledger_cost_usd"],
                "ledger_estimate_aed": call["ledger_cost_aed"],
                "cost_state": call["cost_state"],
                "pricing_completeness": call["pricing_completeness"],
                "missing_cost_inputs": "; ".join(call["missing_cost_inputs"]),
            }
        )
    filename = f"vav-cost-call-report-{datetime.now(UTC).date().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
