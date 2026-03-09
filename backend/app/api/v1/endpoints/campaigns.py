"""Campaign management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.campaign import Campaign, CampaignContact
from app.schemas.campaign import (
    CampaignContactCreate,
    CampaignContactResponse,
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
)

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


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
    query = query.order_by(Campaign.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    return [CampaignResponse.model_validate(c) for c in result.scalars().all()]


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    data: CampaignCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(campaign, key, value)

    await db.flush()
    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/start", response_model=CampaignResponse)
async def start_campaign(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Start a campaign - triggers the campaign worker."""
    result = await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status not in ("draft", "paused"):
        raise HTTPException(status_code=400, detail=f"Cannot start campaign in '{campaign.status}' status")

    campaign.status = "running"
    await db.flush()

    # Trigger async campaign worker
    from app.tasks.campaign_tasks import run_campaign

    run_campaign.delay(str(campaign.id), str(current_user.tenant_id))

    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign(
    campaign_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
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


@router.post(
    "/{campaign_id}/contacts",
    response_model=list[CampaignContactResponse],
    status_code=status.HTTP_201_CREATED,
)
async def add_campaign_contacts(
    campaign_id: UUID,
    contacts: list[CampaignContactCreate],
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk add contacts to a campaign."""
    result = await db.execute(
        select(Campaign).where(
            Campaign.id == campaign_id, Campaign.tenant_id == current_user.tenant_id
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

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
    await db.flush()

    return [CampaignContactResponse.model_validate(c) for c in created]
