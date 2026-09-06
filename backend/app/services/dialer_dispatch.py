"""Durable dialer reservations using the existing guarded outbound-call service.

No provider implementation is duplicated here. A call with ambiguous acceptance
is held for review, never redialed just because a worker/task was lost.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid5

from sqlalchemy import func, select

from app.core.config import settings
from app.middleware.tenant import CurrentUser
from app.models.call import Call
from app.models.dialer import DialerCampaign, DialerCustomer, DialerJob
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.call import CallOutbound
from app.services.compliance_policy import is_outbound_consent_revoked
from app.services.dialer_policy import ACTIVE, TERMINAL, UNCERTAIN, dial_capacity, eligibility, utc
from app.services.phone_numbers import is_number_on_tenant_dnc


async def reconcile(db, campaign, now):
    jobs = (
        await db.scalars(
            select(DialerJob)
            .where(
                DialerJob.tenant_id == campaign.tenant_id,
                DialerJob.campaign_id == campaign.id,
                DialerJob.state.in_(ACTIVE),
            )
            .with_for_update()
        )
    ).all()
    for job in jobs:
        if job.state == "reserved":
            if job.claimed_at and utc(job.claimed_at) < now - timedelta(minutes=5):
                job.state = "queued"  # No worker began; a late task checks state before dispatch.
            continue
        call = (
            await db.scalar(
                select(Call).where(
                    Call.id == job.call_id,
                    Call.tenant_id == campaign.tenant_id,
                )
            )
            if job.call_id
            else None
        )
        if call and call.status in TERMINAL:
            job.outcome = call.disposition or call.status
            if (
                call.status in {"busy", "no_answer"}
                and job.attempts < campaign.config["max_attempts_per_customer"]
                and campaign.status == "running"
            ):
                job.state = "queued"
                job.approved = False  # Preview approval never authorizes automatic redials.
                job.available_at = now + timedelta(hours=campaign.config["retry_delay_hours"])
            else:
                job.state = "completed" if call.status == "completed" else "failed"
            continue
        if call and call.status in {"ringing", "in_progress", "initiated", "active"}:
            job.state = "calling"
        elif (call and call.status in UNCERTAIN) or (
            job.claimed_at
            and utc(job.claimed_at) < now - timedelta(minutes=5)
            and (not call or call.status == "dispatching")
        ):
            job.state = "unknown"
            job.error = "Provider acceptance is uncertain. Inspect the call before any new attempt."
            campaign.status = "paused"


async def claim_due_jobs(db, campaign_id, tenant_id, now=None):
    now = now or datetime.now(UTC)
    # Serialize capacity reservations across all campaigns belonging to a tenant.
    await db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    campaign = await db.scalar(
        select(DialerCampaign)
        .where(
            DialerCampaign.id == campaign_id,
            DialerCampaign.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if campaign is None:
        return []
    await reconcile(db, campaign, now)
    campaign.updated_at = now  # Fairness across batches of more than 200 campaigns.
    if not settings.dialer_live_enabled or campaign.status != "running":
        await db.commit()
        return []
    actor = await db.scalar(
        select(User).where(User.id == campaign.owner_id, User.tenant_id == tenant_id)
    )
    if actor is None or not actor.is_active or actor.role not in {"owner", "admin"}:
        campaign.status = "paused"
        await db.commit()
        return []
    active = list(
        (
            await db.scalars(
                select(DialerJob).where(
                    DialerJob.tenant_id == tenant_id,
                    DialerJob.state.in_(ACTIVE),
                )
            )
        ).all()
    )
    if any(job.state == "unknown" and job.campaign_id == campaign_id for job in active):
        campaign.status = "paused"
        await db.commit()
        return []
    local_active = [job for job in active if job.campaign_id == campaign_id]
    history = (
        await db.execute(
            select(Call.answered_at)
            .join(
                DialerJob,
                (DialerJob.call_id == Call.id) & (DialerJob.tenant_id == Call.tenant_id),
            )
            .where(
                DialerJob.campaign_id == campaign_id,
                Call.tenant_id == tenant_id,
                Call.status.in_(TERMINAL),
            )
            .order_by(Call.created_at.desc())
            .limit(100)
        )
    ).all()
    answered_active = int(
        await db.scalar(
            select(func.count())
            .select_from(Call)
            .where(
                Call.tenant_id == tenant_id,
                Call.id.in_([job.call_id for job in local_active if job.call_id]),
                Call.answered_at.is_not(None),
            )
        )
        or 0
    )
    used = int(
        await db.scalar(
            select(func.coalesce(func.sum(DialerJob.attempts), 0)).where(
                DialerJob.tenant_id == tenant_id,
                DialerJob.campaign_id == campaign_id,
            )
        )
        or 0
    ) + sum(job.state == "reserved" for job in local_active)
    capacity = min(
        dial_capacity(
            campaign.mode,
            campaign.config,
            len(local_active),
            answered_active,
            [answered_at is not None for (answered_at,) in history],
        ),
        max(0, settings.dialer_tenant_concurrency - len(active)),
        max(0, campaign.config["max_attempts_total"] - used),
    )
    active_phones = set(
        (
            await db.scalars(
                select(DialerCustomer.phone_number).where(
                    DialerCustomer.tenant_id == tenant_id,
                    DialerCustomer.id.in_([job.customer_id for job in active]),
                )
            )
        ).all()
    )
    rows = (
        await db.execute(
            select(DialerJob, DialerCustomer)
            .join(
                DialerCustomer,
                (DialerCustomer.id == DialerJob.customer_id)
                & (DialerCustomer.tenant_id == DialerJob.tenant_id),
            )
            .where(
                DialerJob.tenant_id == tenant_id,
                DialerJob.campaign_id == campaign_id,
                DialerJob.state == "queued",
                DialerJob.available_at <= now,
                DialerJob.approved.is_(True) if campaign.mode == "preview" else True,
            )
            .order_by(DialerJob.available_at, DialerJob.created_at)
            .limit(500)
        )
    ).all()
    claimed = []
    for job, customer in rows:
        if len(claimed) >= capacity:
            break
        reason = eligibility(campaign, job, customer, now)
        if customer.phone_number in active_phones:
            job.available_at = now + timedelta(minutes=1)
            continue
        if await is_number_on_tenant_dnc(db, tenant_id, customer.phone_number):
            reason = "do_not_call"
        if await is_outbound_consent_revoked(db, tenant_id, customer.phone_number):
            reason = "consent_revoked"
        if reason:
            if reason in {
                "company_mismatch",
                "contact_permission_missing_or_revoked",
                "attempt_limit",
                "do_not_call",
                "consent_revoked",
            }:
                job.state, job.outcome = "skipped", reason
            elif reason == "outside_calling_hours":
                # Do not let one timezone's blocked rows starve later eligible rows.
                job.available_at = now + timedelta(minutes=1)
            continue
        job.state, job.claimed_at, job.error = "reserved", now, None
        active_phones.add(customer.phone_number)
        claimed.append(str(job.id))
    await db.commit()  # Reserve BEFORE queue publication; sweep recovers undelivered reservations.
    return claimed


async def dispatch_job(db, job_id, tenant_id):
    from app.api.v1.endpoints.calls import CALL_IDEMPOTENCY_NAMESPACE, dispatch_outbound_call

    job = await db.scalar(
        select(DialerJob).where(
            DialerJob.id == job_id,
            DialerJob.tenant_id == tenant_id,
        )
    )
    if job is None:
        return
    campaign_id = job.campaign_id
    customer_id = job.customer_id
    campaign = await db.scalar(
        select(DialerCampaign)
        .where(
            DialerCampaign.id == campaign_id,
            DialerCampaign.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await db.scalar(
        select(DialerJob)
        .where(
            DialerJob.id == job_id,
            DialerJob.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job.state != "reserved":
        await db.rollback()
        return
    customer = await db.scalar(
        select(DialerCustomer).where(
            DialerCustomer.id == job.customer_id,
            DialerCustomer.tenant_id == tenant_id,
        )
    )
    actor = await db.scalar(
        select(User).where(User.id == campaign.owner_id, User.tenant_id == tenant_id)
    )
    if (
        not settings.dialer_live_enabled
        or customer is None
        or eligibility(campaign, job, customer)
        or actor is None
        or not actor.is_active
        or actor.role not in {"owner", "admin"}
    ):
        job.state = "queued" if campaign.status != "cancelled" else "cancelled"
        await db.commit()
        return
    job.attempts += 1
    key = f"dialer:{job.id}:{job.attempts}"
    reserved_call_id = uuid5(CALL_IDEMPOTENCY_NAMESPACE, f"{tenant_id}:{key}")
    job.call_id = reserved_call_id
    job.call_ids = [*job.call_ids, str(reserved_call_id)]
    job.state, job.claimed_at = "dispatching", datetime.now(UTC)
    user = CurrentUser(
        id=actor.id,
        tenant_id=tenant_id,
        email=actor.email,
        role=actor.role,
        full_name=actor.full_name,
    )
    # No private history/notes are sent into the shared KB or provider variables.
    data = CallOutbound(
        agent_id=campaign.agent_id,
        to_number=customer.phone_number,
        context={
            "customer_name": customer.name,
            "preferred_language": customer.language,
            "company_name": campaign.company,
            "call_purpose": campaign.purpose,
            "approved_offer": campaign.config["approved_offer"],
        },
    )
    await db.commit()

    async def final_guard(session):
        current_campaign = await session.scalar(
            select(DialerCampaign)
            .where(
                DialerCampaign.id == campaign_id,
                DialerCampaign.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_job = await session.scalar(
            select(DialerJob)
            .where(
                DialerJob.id == job_id,
                DialerJob.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_customer = await session.scalar(
            select(DialerCustomer)
            .where(
                DialerCustomer.id == customer_id,
                DialerCustomer.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_actor = await session.scalar(
            select(User)
            .where(
                User.id == user.id,
                User.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
        return bool(
            settings.dialer_live_enabled
            and current_campaign
            and current_job
            and current_customer
            and current_actor
            and current_actor.is_active
            and current_actor.role in {"owner", "admin"}
            and current_campaign.owner_id == user.id
            and current_campaign.agent_id == data.agent_id
            and current_job.state == "dispatching"
            and current_job.call_id == reserved_call_id
            and current_customer.phone_number == data.to_number
            and eligibility(current_campaign, current_job, current_customer) is None
        )

    try:
        await dispatch_outbound_call(data, key, user, db, dispatch_guard=final_guard)
    except Exception:
        # Never retry paid dispatch automatically after an exception. Call truth
        # or the watchdog below determines whether the attempt is safe/unknown.
        await db.rollback()
    parent = await db.scalar(
        select(DialerCampaign)
        .where(
            DialerCampaign.id == campaign_id,
            DialerCampaign.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current_job = await db.scalar(
        select(DialerJob)
        .where(
            DialerJob.id == job_id,
            DialerJob.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    call = await db.scalar(
        select(Call).where(Call.id == reserved_call_id, Call.tenant_id == tenant_id)
    )
    if current_job and current_job.state == "dispatching":
        if call is None:
            current_job.state = "failed"
            current_job.error = (
                "Outbound readiness or authorization rejected the attempt; inspect agent settings."
            )
            if parent and parent.status == "running":
                parent.status = "paused"
        elif call.status in {"busy", "no_answer"}:
            # Let reconcile apply the same retry policy for synchronous and async results.
            current_job.state = "calling"
        elif call.status in TERMINAL:
            current_job.state = "completed" if call.status == "completed" else "failed"
            current_job.outcome = call.disposition or call.status
        else:
            current_job.state = "unknown" if call.status in UNCERTAIN else "calling"
            if current_job.state == "unknown" and parent and parent.status == "running":
                parent.status = "paused"
    await db.commit()
