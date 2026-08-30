"""Campaign management endpoints."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent
from app.models.call import Call
from app.models.campaign import Campaign, CampaignContact, CampaignContactAttempt
from app.models.workflow import Workflow
from app.schemas.campaign import (
    CampaignAttemptReconcile,
    CampaignAttemptResponse,
    CampaignContactCreate,
    CampaignContactResponse,
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
)
from app.services.audit import record_audit_event
from app.services.campaign_lifecycle import refresh_campaign_metrics, sync_campaign_call_lifecycle
from app.services.provider_credentials import load_provider_config

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


async def _validate_campaign_references(
    db: AsyncSession,
    tenant_id: UUID,
    agent_id: UUID,
    workflow_id: UUID | None,
) -> tuple[Agent, Workflow | None]:
    agent_result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    workflow = None
    if workflow_id is not None:
        workflow_result = await db.execute(
            select(Workflow).where(
                Workflow.id == workflow_id,
                Workflow.tenant_id == tenant_id,
            )
        )
        workflow = workflow_result.scalar_one_or_none()
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")
    return agent, workflow


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_effective_schedule(campaign: Campaign, data: CampaignUpdate) -> None:
    fields = data.model_fields_set
    start_hour = (
        data.calling_hours_start
        if "calling_hours_start" in fields
        else campaign.calling_hours_start
    )
    end_hour = (
        data.calling_hours_end if "calling_hours_end" in fields else campaign.calling_hours_end
    )
    if (start_hour is None) != (end_hour is None):
        raise HTTPException(
            status_code=422,
            detail="Calling-hours start and end must both be set or both be omitted",
        )
    if start_hour is not None and start_hour == end_hour:
        raise HTTPException(
            status_code=422,
            detail="Calling-hours start and end cannot be the same",
        )

    timezone_name = data.timezone if "timezone" in fields else campaign.timezone
    try:
        ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Timezone must be a valid IANA timezone",
        ) from exc

    scheduled_start = (
        data.scheduled_start if "scheduled_start" in fields else campaign.scheduled_start
    )
    scheduled_end = data.scheduled_end if "scheduled_end" in fields else campaign.scheduled_end
    if scheduled_start and scheduled_end:
        if _as_utc(scheduled_end) <= _as_utc(scheduled_start):
            raise HTTPException(
                status_code=422,
                detail="Scheduled end must be after scheduled start",
            )


def _validate_campaign_dispatch_provider(
    campaign: Campaign,
    agent: Agent,
    *,
    twilio_default_from_number: str = "",
) -> None:
    provider = (agent.voice_provider or "").strip().lower()
    if provider not in {"smallest", "twilio"}:
        raise HTTPException(status_code=409, detail="Campaign voice provider is not supported")
    if provider == "smallest":
        if not agent.provider_agent_id:
            raise HTTPException(
                status_code=409,
                detail="Provision the campaign agent on Smallest.ai before starting",
            )
        if not agent.provider_revision_id or not agent.last_synced_at:
            raise HTTPException(
                status_code=409,
                detail="Complete the initial Smallest.ai publish before starting",
            )
        if agent.sync_status != "synced":
            raise HTTPException(
                status_code=409,
                detail="Publish and verify the campaign agent's current changes before starting",
            )
        if any(
            key in (campaign.settings or {})
            for key in ("from_number", "from_product_id", "version_id")
        ):
            raise HTTPException(
                status_code=409,
                detail="Smallest.ai campaign resources require tenant-owned inventory",
            )
    if provider == "twilio":
        configured_number = (campaign.settings or {}).get("from_number")
        if not configured_number and not twilio_default_from_number:
            raise HTTPException(
                status_code=409,
                detail="Configure a Twilio from number before starting the campaign",
            )


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    query = select(Campaign).where(Campaign.tenant_id == current_user.tenant_id)
    if status_filter:
        query = query.where(Campaign.status == status_filter)
    query = (
        query.order_by(Campaign.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )

    result = await db.execute(query)
    return [CampaignResponse.model_validate(c) for c in result.scalars().all()]


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    data: CampaignCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    await _validate_campaign_references(
        db,
        current_user.tenant_id,
        data.agent_id,
        data.workflow_id,
    )
    campaign = Campaign(
        tenant_id=current_user.tenant_id,
        agent_id=data.agent_id,
        workflow_id=data.workflow_id,
        name=data.name,
        description=data.description,
        campaign_type=data.campaign_type,
        scheduled_start=data.scheduled_start,
        scheduled_end=data.scheduled_end,
        calling_hours_start=data.calling_hours_start,
        calling_hours_end=data.calling_hours_end,
        timezone=data.timezone,
        max_concurrent_calls=data.max_concurrent_calls,
        retry_attempts=data.retry_attempts,
    )
    db.add(campaign)
    await db.flush()

    # Add contacts
    for contact_data in data.contacts:
        contact = CampaignContact(
            tenant_id=current_user.tenant_id,
            campaign_id=campaign.id,
            **contact_data.model_dump(),
        )
        db.add(contact)

    campaign.total_contacts = len(data.contacts)
    await db.flush()

    return CampaignResponse.model_validate(campaign)


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignResponse.model_validate(campaign)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: UUID,
    data: CampaignUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Campaign)
        .where(Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id)
        .with_for_update()
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status == "running":
        raise HTTPException(status_code=409, detail="Pause the campaign before editing it")

    agent_id = data.agent_id if "agent_id" in data.model_fields_set else campaign.agent_id
    workflow_id = (
        data.workflow_id if "workflow_id" in data.model_fields_set else campaign.workflow_id
    )
    if agent_id is None:
        raise HTTPException(status_code=422, detail="Campaign agent cannot be removed")
    await _validate_campaign_references(
        db,
        current_user.tenant_id,
        agent_id,
        workflow_id,
    )
    _validate_effective_schedule(campaign, data)

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(campaign, key, value)

    await db.flush()
    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/start", response_model=CampaignResponse)
async def start_campaign(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Start a campaign - triggers the campaign worker."""
    result = await db.execute(
        select(Campaign)
        .where(Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id)
        .with_for_update()
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status not in ("draft", "paused"):
        raise HTTPException(
            status_code=400, detail=f"Cannot start campaign in '{campaign.status}' status"
        )

    if campaign.agent_id is None:
        raise HTTPException(status_code=409, detail="Campaign has no agent")
    agent, workflow = await _validate_campaign_references(
        db,
        current_user.tenant_id,
        campaign.agent_id,
        campaign.workflow_id,
    )
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="Campaign agent is inactive")
    twilio_config = await load_provider_config(db, current_user.tenant_id, "twilio")
    _validate_campaign_dispatch_provider(
        campaign,
        agent,
        twilio_default_from_number=str(
            (twilio_config or {}).get("default_from_number") or settings.twilio_default_from_number
        ).strip(),
    )
    if workflow is not None and not workflow.is_active:
        raise HTTPException(status_code=409, detail="Campaign workflow is inactive")
    if campaign.scheduled_end is not None:
        if _as_utc(campaign.scheduled_end) <= datetime.now(UTC):
            raise HTTPException(status_code=409, detail="Campaign schedule has already ended")

    unresolved_attempts = await db.scalar(
        select(func.count())
        .select_from(CampaignContactAttempt)
        .where(
            CampaignContactAttempt.campaign_id == campaign.id,
            CampaignContactAttempt.tenant_id == current_user.tenant_id,
            CampaignContactAttempt.state == "unknown",
        )
    )
    if unresolved_attempts:
        raise HTTPException(
            status_code=409,
            detail="Reconcile unknown provider dispatches before resuming the campaign",
        )

    pending_contacts = await db.scalar(
        select(func.count())
        .select_from(CampaignContact)
        .where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.tenant_id == current_user.tenant_id,
            CampaignContact.status == "pending",
        )
    )
    if not pending_contacts:
        raise HTTPException(status_code=409, detail="Campaign has no pending contacts")

    campaign.status = "running"
    await db.commit()

    # Trigger async campaign worker
    from app.tasks.campaign_tasks import run_campaign

    try:
        run_campaign.delay(str(campaign.id), str(current_user.tenant_id))
    except Exception as exc:
        campaign.status = "paused"
        campaign.settings = {
            **(campaign.settings or {}),
            "last_dispatch_error": "Could not enqueue the campaign worker",
        }
        await db.commit()
        raise HTTPException(status_code=503, detail="Campaign worker is unavailable") from exc

    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Campaign)
        .where(Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id)
        .with_for_update()
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status == "paused":
        return CampaignResponse.model_validate(campaign)
    if campaign.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause campaign in '{campaign.status}' status",
        )

    campaign.status = "paused"
    await db.flush()
    return CampaignResponse.model_validate(campaign)


# Campaign contacts
@router.get("/{campaign_id}/contacts", response_model=list[CampaignContactResponse])
async def list_campaign_contacts(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    query = select(CampaignContact).where(
        CampaignContact.campaign_id == campaign_id,
        CampaignContact.tenant_id == current_user.tenant_id,
    )
    if status_filter:
        query = query.where(CampaignContact.status == status_filter)

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    return [CampaignContactResponse.model_validate(c) for c in result.scalars().all()]


@router.get("/{campaign_id}/attempts", response_model=list[CampaignAttemptResponse])
async def list_campaign_attempts(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
    state_filter: str | None = Query(None, alias="state", max_length=30),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    campaign_exists = await db.scalar(
        select(Campaign.id).where(
            Campaign.id == campaign_id,
            Campaign.tenant_id == current_user.tenant_id,
        )
    )
    if campaign_exists is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    query = select(CampaignContactAttempt).where(
        CampaignContactAttempt.campaign_id == campaign_id,
        CampaignContactAttempt.tenant_id == current_user.tenant_id,
    )
    if state_filter:
        query = query.where(CampaignContactAttempt.state == state_filter)
    query = (
        query.order_by(CampaignContactAttempt.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    attempts = (await db.execute(query)).scalars().all()
    return [CampaignAttemptResponse.model_validate(attempt) for attempt in attempts]


@router.post(
    "/{campaign_id}/attempts/{attempt_id}/reconcile",
    response_model=CampaignAttemptResponse,
)
async def reconcile_campaign_attempt(
    campaign_id: UUID,
    attempt_id: UUID,
    data: CampaignAttemptReconcile,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Release a dispatch only after the provider proves no call was created.

    Tenant operators cannot attach a provider call identity: shared-account
    identifiers do not prove tenant ownership. Existing calls reconcile only
    through authenticated provider callbacks or a trusted platform runbook.
    """
    campaign = (
        await db.execute(
            select(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.tenant_id == current_user.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "paused":
        raise HTTPException(status_code=409, detail="Pause the campaign before reconciliation")

    attempt_probe = (
        await db.execute(
            select(CampaignContactAttempt).where(
                CampaignContactAttempt.id == attempt_id,
                CampaignContactAttempt.campaign_id == campaign.id,
                CampaignContactAttempt.tenant_id == current_user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if attempt_probe is None:
        raise HTTPException(status_code=404, detail="Campaign attempt not found")

    contact = (
        await db.execute(
            select(CampaignContact)
            .where(
                CampaignContact.id == attempt_probe.contact_id,
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.tenant_id == current_user.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    attempt = (
        await db.execute(
            select(CampaignContactAttempt)
            .where(
                CampaignContactAttempt.id == attempt_probe.id,
                CampaignContactAttempt.contact_id == attempt_probe.contact_id,
                CampaignContactAttempt.campaign_id == campaign.id,
                CampaignContactAttempt.tenant_id == current_user.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if contact is None or attempt is None or attempt.call_id is None:
        raise HTTPException(status_code=409, detail="Campaign attempt mapping is incomplete")
    call = (
        await db.execute(
            select(Call)
            .where(
                Call.id == attempt.call_id,
                Call.campaign_id == campaign.id,
                Call.tenant_id == current_user.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=409, detail="Campaign call mapping is incomplete")
    if (
        attempt.state != "unknown"
        or contact.status != "dispatch_unknown"
        or call.status != "dispatch_unknown"
        or contact.last_call_id != call.id
    ):
        raise HTTPException(
            status_code=409, detail="Campaign attempt is not awaiting reconciliation"
        )
    if attempt.provider_call_sid or call.provider_call_sid:
        raise HTTPException(
            status_code=409,
            detail=(
                "A known accepted provider call cannot be released by a workspace user; "
                "await a signed callback or trusted platform reconciliation"
            ),
        )

    now = datetime.now(UTC)
    reconciliation_metadata = {
        "action": data.action,
        "actor_user_id": str(current_user.id),
        "reason": data.reason,
        "reconciled_at": now.isoformat(),
    }
    call.status = "failed"
    attempt.state = "rejected"
    attempt.error_code = "manual_definitive_rejection"
    call.ended_at = now
    attempt.error_message = data.reason
    call.call_metadata = {
        **(call.call_metadata or {}),
        "manual_dispatch_reconciliation": reconciliation_metadata,
    }

    await sync_campaign_call_lifecycle(db, call)
    counts = await refresh_campaign_metrics(db, campaign)
    if counts.pending == 0 and counts.active == 0 and counts.completed == counts.total:
        campaign.status = "completed"
    campaign.settings = {
        **(campaign.settings or {}),
        "last_reconciled_attempt_id": str(attempt.id),
        "last_reconciled_at": now.isoformat(),
    }
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action=f"campaign_attempt.{data.action}",
        resource_type="campaign_attempt",
        resource_id=str(attempt.id),
        details={
            "campaign_id": str(campaign.id),
            "contact_id": str(contact.id),
            "call_id": str(call.id),
        },
    )
    await db.flush()
    return CampaignAttemptResponse.model_validate(attempt)


@router.post(
    "/{campaign_id}/contacts",
    response_model=list[CampaignContactResponse],
    status_code=status.HTTP_201_CREATED,
)
async def add_campaign_contacts(
    campaign_id: UUID,
    contacts: Annotated[
        list[CampaignContactCreate],
        Body(min_length=1, max_length=1_000),
    ],
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Bulk add contacts to a campaign."""
    result = await db.execute(
        select(Campaign)
        .where(Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id)
        .with_for_update()
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status not in ("draft", "paused"):
        raise HTTPException(status_code=409, detail="Pause the campaign before adding contacts")

    phone_numbers = [contact.phone_number for contact in contacts]
    if len(phone_numbers) != len(set(phone_numbers)):
        raise HTTPException(
            status_code=409,
            detail="A phone number can appear only once in a campaign",
        )
    existing_number = await db.scalar(
        select(CampaignContact.phone_number)
        .where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.tenant_id == current_user.tenant_id,
            CampaignContact.phone_number.in_(phone_numbers),
        )
        .limit(1)
    )
    if existing_number:
        raise HTTPException(
            status_code=409,
            detail=f"Contact {existing_number} already belongs to this campaign",
        )

    created = []
    for contact_data in contacts:
        contact = CampaignContact(
            tenant_id=current_user.tenant_id,
            campaign_id=campaign_id,
            **contact_data.model_dump(),
        )
        db.add(contact)
        created.append(contact)

    campaign.total_contacts += len(contacts)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A phone number can appear only once in a campaign",
        ) from exc

    return [CampaignContactResponse.model_validate(c) for c in created]
