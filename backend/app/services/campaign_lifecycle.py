"""Idempotent campaign contact and aggregate lifecycle updates."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import Call
from app.models.campaign import Campaign, CampaignContact, CampaignContactAttempt

TERMINAL_CALL_STATUSES = {
    "completed",
    "failed",
    "busy",
    "no_answer",
    "canceled",
    "cancelled",
}
TERMINAL_CONTACT_STATUSES = {"completed", "failed", "skipped", "dnc"}
ACTIVE_CONTACT_STATUSES = {"dispatching", "calling", "dispatch_unknown"}


@dataclass(frozen=True)
class CampaignLifecycleResult:
    campaign_id: str | None = None
    tenant_id: str | None = None
    should_dispatch: bool = False
    campaign_completed: bool = False


@dataclass(frozen=True)
class CampaignCounts:
    total: int
    completed: int
    successful: int
    pending: int
    active: int


async def refresh_campaign_metrics(
    db: AsyncSession,
    campaign: Campaign,
) -> CampaignCounts:
    """Recompute denormalized counters from contact truth.

    Recalculation, rather than counter increments, makes duplicate and
    out-of-order provider callbacks harmless.
    """

    await db.flush()
    row = (
        await db.execute(
            select(
                func.count(CampaignContact.id),
                func.coalesce(
                    func.sum(
                        case(
                            (CampaignContact.status.in_(TERMINAL_CONTACT_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(case((CampaignContact.status == "completed", 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((CampaignContact.status == "pending", 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (CampaignContact.status.in_(ACTIVE_CONTACT_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                CampaignContact.tenant_id == campaign.tenant_id,
                CampaignContact.campaign_id == campaign.id,
            )
        )
    ).one()
    counts = CampaignCounts(*(int(value or 0) for value in row))
    campaign.total_contacts = counts.total
    campaign.completed_contacts = counts.completed
    campaign.successful_contacts = counts.successful
    return counts


async def sync_campaign_call_lifecycle(
    db: AsyncSession,
    call: Call,
    *,
    provider_callback: bool = False,
) -> CampaignLifecycleResult:
    """Merge a provider call state into its campaign contact exactly once."""

    if call.campaign_id is None or call.tenant_id is None:
        return CampaignLifecycleResult()

    attempt_probe = (
        await db.execute(
            select(CampaignContactAttempt).where(
                CampaignContactAttempt.tenant_id == call.tenant_id,
                CampaignContactAttempt.campaign_id == call.campaign_id,
                CampaignContactAttempt.call_id == call.id,
            )
        )
    ).scalar_one_or_none()

    # Campaign is the aggregate lock and is always acquired before contact and
    # attempt locks, matching the dispatcher lock order.
    campaign = (
        await db.execute(
            select(Campaign)
            .where(
                Campaign.id == call.campaign_id,
                Campaign.tenant_id == call.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if campaign is None:
        return CampaignLifecycleResult()

    contact_query = select(CampaignContact).where(
        CampaignContact.tenant_id == call.tenant_id,
        CampaignContact.campaign_id == call.campaign_id,
    )
    if attempt_probe is not None:
        contact_query = contact_query.where(CampaignContact.id == attempt_probe.contact_id)
    else:
        # Compatibility for calls created before durable attempt records were
        # introduced. New dispatches always take the attempt path above.
        contact_query = contact_query.where(CampaignContact.last_call_id == call.id)
    contact = (
        await db.execute(contact_query.with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if contact is None:
        return CampaignLifecycleResult()

    attempt = None
    if attempt_probe is not None:
        attempt = (
            await db.execute(
                select(CampaignContactAttempt)
                .where(
                    CampaignContactAttempt.id == attempt_probe.id,
                    CampaignContactAttempt.tenant_id == call.tenant_id,
                    CampaignContactAttempt.campaign_id == call.campaign_id,
                    CampaignContactAttempt.contact_id == contact.id,
                    CampaignContactAttempt.call_id == call.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    now = datetime.now(UTC)
    call_is_terminal = call.status in TERMINAL_CALL_STATUSES
    # Only the call currently attached to the contact may change contact
    # state. A delayed duplicate callback for an older failed attempt must not
    # move a newer active attempt back to pending and make it dial twice.
    is_current_attempt = contact.last_call_id == call.id
    if call.provider_call_sid and attempt is not None:
        attempt.provider_call_sid = call.provider_call_sid

    if not call_is_terminal:
        terminal_callback_timed_out = bool(
            attempt
            and attempt.state == "unknown"
            and attempt.error_code == "terminal_callback_timeout"
        )
        if (
            not terminal_callback_timed_out
            and is_current_attempt
            and contact.status not in TERMINAL_CONTACT_STATUSES
        ):
            contact.status = "calling"
        if (
            attempt is not None
            and not terminal_callback_timed_out
            and attempt.state
            not in {
                "completed",
                "failed",
                "rejected",
                "cancelled",
            }
        ):
            attempt.state = "accepted"
            attempt.accepted_at = attempt.accepted_at or now
    else:
        succeeded = call.status == "completed"
        if attempt is not None:
            # A signed provider callback is stronger evidence than a prior
            # HTTP rejection observed by the dispatch worker.
            if provider_callback or attempt.state != "rejected":
                attempt.state = "completed" if succeeded else "failed"
            attempt.accepted_at = attempt.accepted_at or call.started_at or now
            attempt.finished_at = attempt.finished_at or call.ended_at or now

        if is_current_attempt:
            if succeeded:
                contact.status = "completed"
            elif contact.attempts <= campaign.retry_attempts and campaign.status not in {
                "completed",
                "cancelled",
            }:
                contact.status = "pending"
            else:
                contact.status = "failed"

    counts = await refresh_campaign_metrics(db, campaign)
    campaign_completed = False
    if (
        campaign.status == "running"
        and counts.pending == 0
        and counts.active == 0
        and counts.completed == counts.total
    ):
        campaign.status = "completed"
        campaign_completed = True

    should_dispatch = (
        campaign.status == "running"
        and counts.pending > 0
        and counts.active < max(campaign.max_concurrent_calls or 1, 1)
    )
    return CampaignLifecycleResult(
        campaign_id=str(campaign.id),
        tenant_id=str(campaign.tenant_id),
        should_dispatch=should_dispatch,
        campaign_completed=campaign_completed,
    )
