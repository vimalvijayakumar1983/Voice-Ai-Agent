"""Admin-operated customer workspace and dialer controls. No dialing on import."""

import asyncio
import csv
from datetime import UTC, datetime
from io import StringIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, require_role
from app.models.agent import Agent
from app.models.call import Call, CallSummary
from app.models.compliance import DncEntry
from app.models.dialer import DialerCampaign, DialerCustomer, DialerJob
from app.models.tenant import Tenant
from app.schemas.dialer import (
    CampaignAction,
    CampaignInput,
    CampaignResponse,
    CustomerInput,
    CustomerPermission,
    CustomerResponse,
    EnqueueInput,
    JobResponse,
)
from app.services.audit import record_audit_event
from app.services.dialer_import import MAX_BYTES, parse_customers
from app.services.dialer_policy import ACTIVE, MODES, dial_capacity, eligibility
from app.services.phone_numbers import is_number_on_tenant_dnc, tenant_phone_dnc_lock

router = APIRouter(prefix="/dialer", tags=["AI Dialer"])
operator = require_role("owner", "admin")


async def owned(db, model, identity, user, *, lock=False):
    statement = select(model).where(model.id == identity, model.tenant_id == user.tenant_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = await db.scalar(statement)
    if row is None:
        raise HTTPException(404, "Record not found")
    return row


async def audit(db, user, action, identity=None, **details):
    await record_audit_event(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action=f"dialer.{action}",
        resource_type="dialer",
        resource_id=str(identity) if identity else None,
        details=details,
    )


async def job_responses(db, rows, tenant_id):
    """Read the current disposition; post-call analysis may finish after queue reconciliation."""
    outcomes = dict(
        (
            await db.execute(
                select(Call.id, Call.disposition).where(
                    Call.tenant_id == tenant_id,
                    Call.id.in_([row.call_id for row in rows if row.call_id]),
                )
            )
        ).all()
    )
    results = []
    for row in rows:
        item = JobResponse.model_validate(row)
        item.outcome = outcomes.get(row.call_id) or item.outcome
        results.append(item)
    return results


@router.get("/capabilities")
async def capabilities(user: CurrentUser = Depends(operator)):
    return {
        "live_enabled": settings.dialer_live_enabled,
        "tenant_concurrency": settings.dialer_tenant_concurrency,
        "modes": MODES,
        "predictive_policy": "AI capacity reserved for every attempt; no overbooking",
        "private_workflows_enabled": False,
        "dncr": "Workspace DNC is automatic. National DNCR review is an operator prerequisite.",
    }


@router.get("/customers", response_model=list[CustomerResponse])
async def customers(
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
    company: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    query = select(DialerCustomer).where(DialerCustomer.tenant_id == user.tenant_id)
    if company:
        query = query.where(DialerCustomer.company == company)
    return (
        await db.scalars(
            query.order_by(DialerCustomer.created_at.desc()).offset(offset).limit(limit)
        )
    ).all()


@router.post("/customers", response_model=CustomerResponse, status_code=201)
async def add_customer(
    data: CustomerInput,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    await db.scalar(select(Tenant).where(Tenant.id == user.tenant_id).with_for_update())
    existing = await db.scalar(
        select(DialerCustomer).where(
            DialerCustomer.tenant_id == user.tenant_id,
            DialerCustomer.company == data.company,
            DialerCustomer.phone_number == data.phone_number,
        )
    )
    if existing:
        raise HTTPException(409, "This company already has a customer with that phone number")
    customer = DialerCustomer(tenant_id=user.tenant_id, **data.model_dump())
    db.add(customer)
    await db.flush()
    await audit(db, user, "customer_created", customer.id)
    return customer


@router.post("/customers/import")
async def import_customers(
    file: UploadFile,
    commit: bool = False,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read(MAX_BYTES + 1)
    try:
        rows, errors, total = await asyncio.to_thread(parse_customers, file.filename or "", content)
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise HTTPException(422, str(exc)) from exc
        raise HTTPException(
            422, "Invalid or unsupported workbook; export a values-only CSV"
        ) from exc
    result = {
        "rows": total,
        "valid": len(rows),
        "errors": errors,
        "created": 0,
        "existing": 0,
        "preview": [row.model_dump() for row in rows[:20]],
        "committed": False,
    }
    if not commit:
        return result
    if errors:
        raise HTTPException(422, "Fix the import errors before committing; no customers imported")
    # Serialize imports/customer creation so repeat submissions never create duplicates.
    await db.scalar(select(Tenant).where(Tenant.id == user.tenant_id).with_for_update())
    keys = set(
        (
            await db.execute(
                select(DialerCustomer.company, DialerCustomer.phone_number).where(
                    DialerCustomer.tenant_id == user.tenant_id,
                    DialerCustomer.phone_number.in_([row.phone_number for row in rows]),
                )
            )
        ).all()
    )
    for row in rows:
        if (row.company, row.phone_number) in keys:
            result["existing"] += 1  # Never overwrite history, consent or opt-out during import.
        else:
            db.add(DialerCustomer(tenant_id=user.tenant_id, **row.model_dump()))
            result["created"] += 1
    result["committed"] = True
    await audit(
        db, user, "customers_imported", created=result["created"], existing=result["existing"]
    )
    return result


@router.patch("/customers/{customer_id}/permission", response_model=CustomerResponse)
async def permission(
    customer_id: UUID,
    data: CustomerPermission,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    initial = await owned(db, DialerCustomer, customer_id, user)
    async with tenant_phone_dnc_lock(db, user.tenant_id, initial.phone_number):
        customer = await owned(db, DialerCustomer, customer_id, user, lock=True)
        if not data.opted_out:
            raise HTTPException(
                409, "Restoring permission requires a separate reviewed consent workflow"
            )
        customer.opted_out = True
        if not await is_number_on_tenant_dnc(db, user.tenant_id, customer.phone_number):
            db.add(
                DncEntry(
                    tenant_id=user.tenant_id,
                    phone_number=customer.phone_number,
                    reason="customer_request",
                    source="dialer",
                    added_by=user.id,
                )
            )
        await audit(db, user, "customer_opt_out", customer_id, reason=data.reason)
        await db.commit()
        return customer


@router.get("/customers/{customer_id}/history", response_model=list[JobResponse])
async def history(
    customer_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    await owned(db, DialerCustomer, customer_id, user)
    rows = (
        await db.scalars(
            select(DialerJob)
            .where(
                DialerJob.tenant_id == user.tenant_id,
                DialerJob.customer_id == customer_id,
            )
            .order_by(DialerJob.created_at.desc())
            .limit(200)
        )
    ).all()
    return await job_responses(db, rows, user.tenant_id)


@router.get("/campaigns", response_model=list[CampaignResponse])
async def campaigns(user: CurrentUser = Depends(operator), db: AsyncSession = Depends(get_db)):
    return (
        await db.scalars(
            select(DialerCampaign)
            .where(
                DialerCampaign.tenant_id == user.tenant_id,
            )
            .order_by(DialerCampaign.created_at.desc())
            .limit(200)
        )
    ).all()


@router.post("/campaigns", response_model=CampaignResponse, status_code=201)
async def add_campaign(
    data: CampaignInput,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    agent = await owned(db, Agent, data.agent_id, user)
    if not agent.is_active:
        raise HTTPException(409, "Agent is inactive")
    campaign = DialerCampaign(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        name=data.name,
        company=data.company,
        agent_id=data.agent_id,
        mode=data.mode,
        purpose=data.purpose,
        config=data.model_dump(
            mode="json", exclude={"name", "company", "agent_id", "mode", "purpose"}
        ),
    )
    db.add(campaign)
    await db.flush()
    await audit(db, user, "campaign_created", campaign.id, mode=campaign.mode)
    return campaign


@router.post("/campaigns/{campaign_id}/queue", response_model=list[JobResponse])
async def enqueue(
    campaign_id: UUID,
    data: EnqueueInput,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    campaign = await owned(db, DialerCampaign, campaign_id, user, lock=True)
    if campaign.status == "cancelled":
        raise HTTPException(409, "Campaign is cancelled")
    if campaign.mode == "scheduled" and data.available_at is None:
        raise HTTPException(422, "Callbacks require an explicit date, time and timezone offset")
    result = []
    for customer_id in data.customer_ids:
        customer = await owned(db, DialerCustomer, customer_id, user)
        if customer.company != campaign.company:
            raise HTTPException(409, "Customer company does not match the campaign")
        key = f"{data.event_key}:{customer_id}"
        job = await db.scalar(
            select(DialerJob).where(
                DialerJob.tenant_id == user.tenant_id,
                DialerJob.campaign_id == campaign_id,
                DialerJob.event_key == key,
            )
        )
        if job is not None:
            from app.services.dialer_policy import utc

            requested = utc(job.requested_at) if job.requested_at else None
            if requested != data.available_at:
                raise HTTPException(409, "Event key already used with a different callback time")
            result.append(job)
            continue
        job = DialerJob(
            tenant_id=user.tenant_id,
            campaign_id=campaign_id,
            customer_id=customer_id,
            event_key=key,
            available_at=data.available_at or datetime.now(UTC),
            requested_at=data.available_at,
        )
        db.add(job)
        result.append(job)
    await db.flush()
    await audit(db, user, "jobs_queued", campaign_id, count=len(result))
    return result


@router.post("/campaigns/{campaign_id}/action", response_model=CampaignResponse)
async def action(
    campaign_id: UUID,
    data: CampaignAction,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    campaign = await owned(db, DialerCampaign, campaign_id, user, lock=True)
    if campaign.status == "cancelled":
        raise HTTPException(409, "Cancelled campaigns cannot be restarted")
    if data.action == "start":
        if not settings.dialer_live_enabled:
            raise HTTPException(
                409, "Live dialer execution is disabled; use simulation until rollout approval"
            )
        if not campaign.config.get("compliance_approved") or campaign.purpose == "collections":
            raise HTTPException(
                409, "Compliance review or verified private-data workflow is required"
            )
        agent = await owned(db, Agent, campaign.agent_id, user)
        if not agent.is_active:
            raise HTTPException(409, "Agent is inactive")
        campaign.owner_id = user.id  # Rechecked for active admin authority on every dispatch.
        campaign.status = "running"
    else:
        campaign.status = "paused" if data.action == "pause" else "cancelled"
        if data.action == "cancel":
            await db.execute(
                update(DialerJob)
                .where(
                    DialerJob.tenant_id == user.tenant_id,
                    DialerJob.campaign_id == campaign_id,
                    DialerJob.state.in_({"queued", "reserved"}),
                )
                .values(state="cancelled")
            )
    await audit(db, user, data.action, campaign_id)
    # Periodic queue sweep is authoritative; queue publication isn't required for durability.
    return campaign


@router.get("/campaigns/{campaign_id}/jobs", response_model=list[JobResponse])
async def jobs(
    campaign_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
    offset: int = Query(0, ge=0),
):
    await owned(db, DialerCampaign, campaign_id, user)
    rows = (
        await db.scalars(
            select(DialerJob)
            .where(
                DialerJob.tenant_id == user.tenant_id,
                DialerJob.campaign_id == campaign_id,
            )
            .order_by(DialerJob.available_at)
            .offset(offset)
            .limit(200)
        )
    ).all()
    return await job_responses(db, rows, user.tenant_id)


@router.post("/jobs/{job_id}/approve", response_model=JobResponse)
async def approve_job(
    job_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    job = await owned(db, DialerJob, job_id, user, lock=True)
    if job.state != "queued":
        raise HTTPException(409, "Only queued calls can be approved")
    job.approved = True
    await audit(db, user, "preview_approved", job_id)
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    # Read the parent first; lock order is campaign, then job throughout dispatch.
    initial = await owned(db, DialerJob, job_id, user)
    await owned(db, DialerCampaign, initial.campaign_id, user, lock=True)
    job = await owned(db, DialerJob, job_id, user, lock=True)
    if job.state not in {"queued", "reserved", "dispatching"}:
        raise HTTPException(409, "Call may already be active; this control cannot hang it up")
    job.state = "cancelled"
    await audit(db, user, "job_cancelled", job_id)
    return job


@router.get("/campaigns/{campaign_id}/simulation")
async def simulation(
    campaign_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    from types import SimpleNamespace

    from app.services.compliance_policy import is_outbound_consent_revoked
    from app.services.phone_numbers import is_number_on_tenant_dnc

    campaign = await owned(db, DialerCampaign, campaign_id, user)
    proposed = SimpleNamespace(
        status="running",
        company=campaign.company,
        mode=campaign.mode,
        purpose=campaign.purpose,
        config=campaign.config,
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
                DialerJob.campaign_id == campaign_id,
                DialerJob.tenant_id == user.tenant_id,
                DialerJob.state == "queued",
            )
            .order_by(DialerJob.available_at)
            .limit(200)
        )
    ).all()
    active = int(
        await db.scalar(
            select(func.count())
            .select_from(DialerJob)
            .where(
                DialerJob.tenant_id == user.tenant_id,
                DialerJob.campaign_id == campaign_id,
                DialerJob.state.in_(ACTIVE),
            )
        )
        or 0
    )
    plan = []
    for job, customer in rows:
        reason = eligibility(proposed, job, customer)
        if await is_number_on_tenant_dnc(db, user.tenant_id, customer.phone_number):
            reason = "do_not_call"
        if await is_outbound_consent_revoked(db, user.tenant_id, customer.phone_number):
            reason = "consent_revoked"
        plan.append(
            {
                "job_id": str(job.id),
                "customer": customer.name,
                "eligible": reason is None,
                "reason": reason or "eligible_pending_provider_readiness",
            }
        )
    return {
        "live_call_made": False,
        "live_enabled": settings.dialer_live_enabled,
        "capacity_without_provider_validation": dial_capacity(
            campaign.mode, campaign.config, active
        ),
        "predictive_note": "Simulation uses conservative warmup; live pacing uses observed calls",
        "jobs": plan,
    }


@router.get("/campaigns/{campaign_id}/report")
async def report(
    campaign_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    await owned(db, DialerCampaign, campaign_id, user)
    counts = (
        await db.execute(
            select(DialerJob.state, func.count())
            .where(
                DialerJob.campaign_id == campaign_id,
                DialerJob.tenant_id == user.tenant_id,
            )
            .group_by(DialerJob.state)
        )
    ).all()
    attempts = await db.scalar(
        select(func.coalesce(func.sum(DialerJob.attempts), 0)).where(
            DialerJob.campaign_id == campaign_id,
            DialerJob.tenant_id == user.tenant_id,
        )
    )
    return {
        "states": dict(counts),
        "attempts": attempts,
        "note": "Completed calls are not proof of confirmed bookings or qualified leads.",
    }


@router.get("/campaigns/{campaign_id}/export")
async def export_jobs(
    campaign_id: UUID,
    user: CurrentUser = Depends(operator),
    db: AsyncSession = Depends(get_db),
):
    await owned(db, DialerCampaign, campaign_id, user)
    rows = (
        await db.execute(
            select(
                DialerJob,
                DialerCustomer,
                Call.disposition,
                Call.duration_seconds,
                CallSummary.summary,
            )
            .join(
                DialerCustomer,
                (DialerCustomer.id == DialerJob.customer_id)
                & (DialerCustomer.tenant_id == DialerJob.tenant_id),
            )
            .outerjoin(
                Call, (Call.id == DialerJob.call_id) & (Call.tenant_id == DialerJob.tenant_id)
            )
            .outerjoin(
                CallSummary,
                (CallSummary.call_id == Call.id) & (CallSummary.tenant_id == Call.tenant_id),
            )
            .where(DialerJob.campaign_id == campaign_id, DialerJob.tenant_id == user.tenant_id)
            .order_by(DialerJob.created_at)
            .limit(10000)
        )
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "customer",
            "phone_number",
            "state",
            "attempts",
            "outcome",
            "call_id",
            "scheduled_at",
            "duration_seconds",
            "summary",
            "attempt_call_ids",
        ]
    )
    for job, customer, disposition, duration, summary in rows:
        values = [
            customer.name,
            customer.phone_number,
            job.state,
            job.attempts,
            disposition or job.outcome or "",
            job.call_id or "",
            job.available_at.isoformat(),
            duration if duration is not None else "",
            summary or "",
            ";".join(job.call_ids or []),
        ]
        writer.writerow(
            [
                "'" + str(v) if str(v).lstrip().startswith(("=", "+", "-", "@")) else v
                for v in values
            ]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="dialer-results.csv"',
            "Cache-Control": "no-store",
        },
    )
