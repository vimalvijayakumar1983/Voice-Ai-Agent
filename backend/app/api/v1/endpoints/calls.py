"""Call management endpoints - logs, transcripts, summaries, and outbound calls."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.agent import Agent
from app.models.call import Call, CallSummary, CallTranscript
from app.providers.smallest import SmallestAIError, get_smallest_client
from app.schemas.call import (
    CallOutbound,
    CallResponse,
    CallSummaryResponse,
    CallTranscriptResponse,
)
from app.telephony.base import CallRequest
from app.telephony.twilio_provider import get_telephony_provider

router = APIRouter(prefix="/calls", tags=["Calls"])


@router.get("", response_model=list[CallResponse])
async def list_calls(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: UUID | None = None,
    campaign_id: UUID | None = None,
    direction: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    query = select(Call).where(Call.tenant_id == current_user.tenant_id)

    if agent_id:
        query = query.where(Call.agent_id == agent_id)
    if campaign_id:
        query = query.where(Call.campaign_id == campaign_id)
    if direction:
        query = query.where(Call.direction == direction)
    if status_filter:
        query = query.where(Call.status == status_filter)
    if from_date:
        query = query.where(Call.created_at >= from_date)
    if to_date:
        query = query.where(Call.created_at <= to_date)

    query = query.order_by(Call.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    return [CallResponse.model_validate(c) for c in result.scalars().all()]


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Call).where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return CallResponse.model_validate(call)


@router.post("", response_model=CallResponse, status_code=201)
async def initiate_outbound_call(
    data: CallOutbound,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Initiate an outbound call using the specified agent."""
    # Verify agent
    result = await db.execute(
        select(Agent).where(Agent.id == data.agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    is_smallest = agent.voice_provider == "smallest"
    from_number = data.from_number or (
        settings.smallest_default_from_number
        if is_smallest
        else settings.twilio_default_from_number
    )
    if not from_number and not is_smallest:
        raise HTTPException(
            status_code=400,
            detail="No from_number provided and no default configured",
        )
    from_number = from_number or "provider-managed"

    # Create call record
    call = Call(
        tenant_id=current_user.tenant_id,
        agent_id=agent.id,
        direction="outbound",
        status="initiated",
        from_number=from_number,
        to_number=data.to_number,
        provider="smallest" if is_smallest else "twilio",
        call_metadata=data.context,
    )
    db.add(call)
    await db.flush()

    try:
        if is_smallest:
            if not agent.provider_agent_id:
                raise SmallestAIError(
                    "Provision this agent on Smallest.ai before placing a call.",
                    status_code=409,
                )
            conversation_id = await get_smallest_client().start_outbound_call(
                agent_id=agent.provider_agent_id,
                phone_number=data.to_number,
                variables=data.context,
                from_product_id=data.from_product_id,
                version_id=data.version_id,
            )
            call.provider_call_sid = conversation_id
        else:
            provider = get_telephony_provider()
            webhook_url = f"{settings.base_url}/api/v1/webhooks/twilio/voice/{call.id}"
            status_url = f"{settings.base_url}/api/v1/webhooks/twilio/status/{call.id}"
            provider_result = await provider.make_call(
                CallRequest(
                    to_number=data.to_number,
                    from_number=from_number,
                    webhook_url=webhook_url,
                    status_callback_url=status_url,
                )
            )
            call.provider_call_sid = provider_result.provider_call_sid
        call.status = "ringing"
        call.started_at = datetime.now(UTC)
    except Exception as e:
        call.status = "failed"
        call.call_metadata = {**(call.call_metadata or {}), "error": str(e)}

    await db.flush()
    return CallResponse.model_validate(call)


@router.get("/{call_id}/transcript", response_model=CallTranscriptResponse)
async def get_call_transcript(
    call_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CallTranscript).where(
            CallTranscript.call_id == call_id,
            CallTranscript.tenant_id == current_user.tenant_id,
        )
    )
    transcript = result.scalar_one_or_none()
    if not transcript:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return CallTranscriptResponse.model_validate(transcript)


@router.get("/{call_id}/summary", response_model=CallSummaryResponse)
async def get_call_summary(
    call_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CallSummary).where(
            CallSummary.call_id == call_id,
            CallSummary.tenant_id == current_user.tenant_id,
        )
    )
    summary = result.scalar_one_or_none()
    if not summary:
        raise HTTPException(status_code=404, detail="Summary not found")
    return CallSummaryResponse.model_validate(summary)
