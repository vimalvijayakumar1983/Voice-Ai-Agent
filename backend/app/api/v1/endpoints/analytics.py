"""Analytics endpoints."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.agent import Agent
from app.models.billing import UsageRecord
from app.models.call import Call
from app.models.campaign import Campaign
from app.schemas.analytics import (
    AgentPerformance,
    AnalyticsOverview,
    AnalyticsTimeSeries,
    CampaignAnalytics,
)

router = APIRouter(prefix="/analytics", tags=["Analytics"])

SUCCESSFUL_DISPOSITIONS = frozenset(
    {
        "appointment_booked",
        "booked",
        "converted",
        "interested",
        "sale",
        "success",
        "successful",
    }
)


def _period_bucket(value: date, period: str) -> date:
    if period == "week":
        return value - timedelta(days=value.weekday())
    if period == "month":
        return value.replace(day=1)
    return value


def _normalized_disposition():
    """Return a portable SQL expression for canonical disposition matching."""
    value = func.lower(func.trim(Call.disposition))
    return func.replace(func.replace(value, "-", "_"), " ", "_")


@router.get("/overview", response_model=AnalyticsOverview)
async def get_overview(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    base = and_(Call.tenant_id == current_user.tenant_id, Call.created_at >= since)

    # Aggregate stats
    result = await db.execute(
        select(
            func.count(Call.id).label("total_calls"),
            func.coalesce(func.sum(Call.duration_seconds), 0).label("total_seconds"),
            func.coalesce(func.avg(Call.duration_seconds), 0).label("avg_duration"),
        ).where(base)
    )
    row = result.one()

    # Usage records are the billing ledger. Call.cost_cents is retained only
    # as legacy/provider metadata and must not be added to ledger costs.
    cost_result = await db.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_cents), 0)).where(
            UsageRecord.tenant_id == current_user.tenant_id,
            UsageRecord.created_at >= since,
            UsageRecord.created_at <= now,
        )
    )
    total_cost_cents = int(cost_result.scalar_one() or 0)

    # Calls by status
    status_result = await db.execute(
        select(Call.status, func.count(Call.id)).where(base).group_by(Call.status)
    )
    calls_by_status = dict(status_result.all())

    # Calls by direction
    dir_result = await db.execute(
        select(Call.direction, func.count(Call.id)).where(base).group_by(Call.direction)
    )
    calls_by_direction = dict(dir_result.all())

    # Calls by disposition
    disp_result = await db.execute(
        select(Call.disposition, func.count(Call.id))
        .where(base, Call.disposition.isnot(None))
        .group_by(Call.disposition)
    )
    calls_by_disposition = dict(disp_result.all())

    return AnalyticsOverview(
        total_calls=row.total_calls,
        total_minutes=row.total_seconds / 60.0,
        avg_duration_seconds=float(row.avg_duration),
        total_cost_cents=total_cost_cents,
        calls_by_status=calls_by_status,
        calls_by_direction=calls_by_direction,
        calls_by_disposition=calls_by_disposition,
    )


@router.get("/timeseries", response_model=AnalyticsTimeSeries)
async def get_timeseries(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    period: str = Query("day", pattern="^(day|week|month)$"),
):
    since = datetime.now(UTC) - timedelta(days=days)
    base = and_(Call.tenant_id == current_user.tenant_id, Call.created_at >= since)

    date_col = func.date(Call.created_at)
    result = await db.execute(
        select(
            date_col.label("date"),
            func.count(Call.id).label("calls"),
            func.coalesce(func.sum(Call.duration_seconds), 0).label("seconds"),
        )
        .where(base)
        .group_by(date_col)
        .order_by(date_col)
    )

    # Keep the SQL expression portable across PostgreSQL and the SQLite test
    # suite, then combine daily rows into the requested calendar bucket. Weeks
    # begin on Monday and months begin on their first day.
    buckets: dict[date, dict[str, int]] = defaultdict(lambda: {"calls": 0, "seconds": 0})
    for row in result.all():
        value = row.date
        if isinstance(value, str):
            value = date.fromisoformat(value)
        bucket = _period_bucket(value, period)
        buckets[bucket]["calls"] += int(row.calls or 0)
        buckets[bucket]["seconds"] += int(row.seconds or 0)

    data = [
        {
            "date": bucket.isoformat(),
            "calls": values["calls"],
            "minutes": round(values["seconds"] / 60.0, 1),
        }
        for bucket, values in sorted(buckets.items())
    ]

    return AnalyticsTimeSeries(period=period, data=data)


@router.get("/agents", response_model=list[AgentPerformance])
async def get_agent_performance(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    since = datetime.now(UTC) - timedelta(days=days)
    normalized_disposition = _normalized_disposition()

    result = await db.execute(
        select(
            Agent.id,
            Agent.name,
            func.count(Call.id).label("total_calls"),
            func.coalesce(func.avg(Call.duration_seconds), 0).label("avg_duration"),
            func.coalesce(func.avg(Call.sentiment_score), None).label("avg_sentiment"),
            func.sum(
                case(
                    (
                        and_(
                            Call.status == "completed",
                            normalized_disposition.in_(SUCCESSFUL_DISPOSITIONS),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("successful_calls"),
            func.sum(case((Call.status == "completed", 1), else_=0)).label("completed_calls"),
        )
        .join(Call, and_(Call.agent_id == Agent.id, Call.created_at >= since))
        .where(Agent.tenant_id == current_user.tenant_id)
        .group_by(Agent.id, Agent.name)
    )

    agents = []
    for row in result.all():
        completed_calls = int(row.completed_calls or 0)
        successful_calls = int(row.successful_calls or 0)
        agents.append(
            AgentPerformance(
                agent_id=str(row.id),
                agent_name=row.name,
                total_calls=row.total_calls,
                avg_duration_seconds=float(row.avg_duration),
                success_rate=(successful_calls / completed_calls if completed_calls else 0.0),
                avg_sentiment=(float(row.avg_sentiment) if row.avg_sentiment is not None else None),
            )
        )

    return agents


@router.get("/campaigns", response_model=list[CampaignAnalytics])
async def get_campaign_analytics(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Campaign)
        .where(Campaign.tenant_id == current_user.tenant_id)
        .order_by(Campaign.created_at.desc())
        .limit(20)
    )

    analytics = []
    for campaign in result.scalars().all():
        total = campaign.total_contacts or 1
        analytics.append(
            CampaignAnalytics(
                campaign_id=str(campaign.id),
                campaign_name=campaign.name,
                total_contacts=campaign.total_contacts,
                completed=campaign.completed_contacts,
                successful=campaign.successful_contacts,
                completion_rate=campaign.completed_contacts / total,
                success_rate=campaign.successful_contacts / total,
            )
        )

    return analytics
