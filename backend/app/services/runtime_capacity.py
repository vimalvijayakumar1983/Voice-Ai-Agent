"""Shared atomic capacity and budget reservation for VAV realtime sessions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.services.usage_ledger import lock_agent_runtime_limits, monthly_agent_budget_commitment

TERMINAL_CALL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "no_answer",
        "busy",
        "canceled",
        "cancelled",
        "terminal_unknown",
    }
)


class RuntimeCapacityError(RuntimeError):
    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind


async def enforce_runtime_capacity(
    db: AsyncSession,
    *,
    model: Agent,
    profile: AgentRuntimeProfile,
    lock_already_held: bool = False,
) -> None:
    """Lock, measure, and reserve one prospective session atomically."""
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    if not lock_already_held:
        await lock_agent_runtime_limits(db, tenant_id=model.tenant_id, agent_id=model.id)
    daily_calls = await db.scalar(
        select(func.count())
        .select_from(Call)
        .where(
            Call.tenant_id == model.tenant_id,
            Call.agent_id == model.id,
            Call.created_at >= day_start,
        )
    )
    active_calls = await db.scalar(
        select(func.count())
        .select_from(Call)
        .where(
            Call.tenant_id == model.tenant_id,
            Call.agent_id == model.id,
            Call.status.notin_(TERMINAL_CALL_STATUSES),
        )
    )
    monthly_budget = await monthly_agent_budget_commitment(
        db,
        tenant_id=model.tenant_id,
        agent_id=model.id,
        month_start=month_start,
        max_call_duration_seconds=model.max_call_duration_seconds,
        include_prospective_call=True,
    )
    if int(daily_calls or 0) >= profile.daily_call_limit:
        raise RuntimeCapacityError("Agent daily call limit has been reached", kind="daily")
    if int(active_calls or 0) >= profile.max_concurrent_calls:
        raise RuntimeCapacityError(
            "Agent concurrent call limit has been reached",
            kind="concurrent",
        )
    if monthly_budget.total_cents > profile.monthly_budget_cents:
        raise RuntimeCapacityError("Agent monthly call budget has been reached", kind="budget")
