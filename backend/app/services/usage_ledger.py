"""Authoritative usage-ledger aggregation and conservative call reservations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import UsageRecord
from app.models.call import Call
from app.services.campaign_lifecycle import TERMINAL_CALL_STATUSES

CALL_MINUTE_LEDGER_CENTS_PER_MINUTE = 5


@dataclass(frozen=True)
class MonthlyBudgetCommitment:
    ledger_cents: int
    unprocessed_reservation_cents: int
    prospective_reservation_cents: int

    @property
    def total_cents(self) -> int:
        return (
            self.ledger_cents
            + self.unprocessed_reservation_cents
            + self.prospective_reservation_cents
        )


def metered_call_cost_cents(duration_seconds: int | float | None) -> int:
    """Match the persisted call-minute ledger's existing whole-cent calculation."""
    seconds = max(float(duration_seconds or 0), 0.0)
    return int(seconds / 60 * CALL_MINUTE_LEDGER_CENTS_PER_MINUTE)


def conservative_call_reservation_cents(duration_seconds: int | float | None) -> int:
    """Round up so an unmetered call never creates a budget blind spot."""
    seconds = max(float(duration_seconds or 0), 0.0)
    if seconds == 0:
        return 0
    return ceil(seconds / 60 * CALL_MINUTE_LEDGER_CENTS_PER_MINUTE)


def _unsettled_call_duration_reservation(call: Call, fallback_seconds: int) -> int:
    """Use an immutable browser-call cap instead of today's mutable agent cap."""
    metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
    reserved_duration = metadata.get("reserved_max_duration_seconds")
    if (
        call.provider == "livekit_webrtc"
        and metadata.get("channel") == "browser"
        and not isinstance(reserved_duration, bool)
        and isinstance(reserved_duration, int)
        and 30 <= reserved_duration <= 7200
    ):
        return reserved_duration
    return fallback_seconds


async def lock_agent_runtime_limits(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
) -> None:
    """Serialize all per-agent capacity checks and reservations across replicas."""
    if db.get_bind().dialect.name != "postgresql":
        return
    # Daily, concurrent, and monthly checks must use one fixed lock in every
    # inbound/outbound path. A month-scoped key would split reservations that
    # race across a UTC boundary.
    lock_key = f"runtime-limits:{tenant_id}:{agent_id}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


async def monthly_agent_budget_commitment(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    month_start: datetime,
    max_call_duration_seconds: int,
    include_prospective_call: bool,
) -> MonthlyBudgetCommitment:
    """Combine measured ledger cost with safe reservations for unsettled calls."""
    ledger_cents = await db.scalar(
        select(func.coalesce(func.sum(UsageRecord.cost_cents), 0))
        .select_from(UsageRecord)
        .join(
            Call,
            and_(
                Call.id == UsageRecord.call_id,
                Call.tenant_id == UsageRecord.tenant_id,
            ),
        )
        .where(
            UsageRecord.tenant_id == tenant_id,
            Call.tenant_id == tenant_id,
            Call.agent_id == agent_id,
            Call.created_at >= month_start,
        )
    )

    metered_call_ids = select(UsageRecord.call_id).where(
        UsageRecord.tenant_id == tenant_id,
        UsageRecord.usage_type == "call_minutes",
        UsageRecord.call_id.is_not(None),
    )
    unsettled_calls = (
        await db.scalars(
            select(Call).where(
                Call.tenant_id == tenant_id,
                Call.agent_id == agent_id,
                Call.created_at >= month_start,
                Call.id.not_in(metered_call_ids),
            )
        )
    ).all()

    unprocessed_reservation = 0
    for call in unsettled_calls:
        if call.duration_seconds and call.duration_seconds > 0:
            unprocessed_reservation += conservative_call_reservation_cents(call.duration_seconds)
        elif call.answered_at is not None or call.status not in TERMINAL_CALL_STATUSES:
            unprocessed_reservation += conservative_call_reservation_cents(
                _unsettled_call_duration_reservation(
                    call,
                    max_call_duration_seconds,
                )
            )

    prospective_reservation = (
        conservative_call_reservation_cents(max_call_duration_seconds)
        if include_prospective_call
        else 0
    )
    return MonthlyBudgetCommitment(
        ledger_cents=int(ledger_cents or 0),
        unprocessed_reservation_cents=unprocessed_reservation,
        prospective_reservation_cents=prospective_reservation,
    )
