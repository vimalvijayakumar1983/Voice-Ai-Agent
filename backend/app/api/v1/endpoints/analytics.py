"""Analytics endpoints."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, and_, case, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.call import Call
from app.models.agent import Agent
from app.models.campaign import Campaign
from app.schemas.analytics import (
    AgentPerformance,
    AnalyticsOverview,
    AnalyticsTimeSeries,
    CampaignAnalytics,
)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/overview", response_model=AnalyticsOverview)
async def get_overview(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = and_(Call.tenant_id == current_user.tenant_id, Call.created_at >= since)

    # Aggregate stats
    result = await db.execute(
        select(
            func.count(Call.id).label("total_calls"),
            func.coalesce(func.sum(Call.duration_seconds), 0).label("total_seconds"),
            func.coalesce(func.avg(Call.duration_seconds), 0).label("avg_duration"),
            func.coalesce(func.sum(Call.cost_cents), 0).label("total_cost"),
        ).where(base)
    )
    row = result.one()

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
        total_cost_cents=row.total_cost,
        calls_by_status=calls_by_status,
        calls_by_direction=calls_by_direction,
        calls_by_disposition=calls_by_disposition,
    )


@router.get("/timeseries", response_model=AnalyticsTimeSeries)
async def get_timeseries(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    period: str = Query("day", regex="^(day|week|month)$"),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = and_(Call.tenant_id == current_user.tenant_id, Call.created_at >= since)

    date_col = cast(Call.created_at, Date)
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

    data = [
        {"date": str(row.date), "calls": row.calls, "minutes": round(row.seconds / 60.0, 1)}
        for row in result.all()
    ]

    return AnalyticsTimeSeries(period=period, data=data)


@router.get("/agents", response_model=list[AgentPerformance])
async def get_agent_performance(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            Agent.id,
            Agent.name,
            func.count(Call.id).label("total_calls"),
            func.coalesce(func.avg(Call.duration_seconds), 0).label("avg_duration"),
            func.coalesce(func.avg(Call.sentiment_score), None).label("avg_sentiment"),
        )
        .join(Call, and_(Call.agent_id == Agent.id, Call.created_at >= since))
        .where(Agent.tenant_id == current_user.tenant_id)
        .group_by(Agent.id, Agent.name)
    )

    agents = []
    for row in result.all():
        agents.append(
            AgentPerformance(
                agent_id=str(row.id),
                agent_name=row.name,
                total_calls=row.total_calls,
                avg_duration_seconds=float(row.avg_duration),
                success_rate=0.0,  # TODO: compute from dispositions
                avg_sentiment=float(row.avg_sentiment) if row.avg_sentiment else None,
            )
        )

    return agents


@router.get("/campaigns", response_model=list[CampaignAnalytics])
async def get_campaign_analytics(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Campaign).where(Campaign.tenant_id == current_user.tenant_id).order_by(
            Campaign.created_at.desc()
        ).limit(20)
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
