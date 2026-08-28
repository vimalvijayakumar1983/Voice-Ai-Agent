"""Call management endpoints - logs, transcripts, summaries, and outbound calls."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid5

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent
from app.models.call import Call, CallSummary, CallTranscript
from app.providers.smallest import SmallestAIError, get_smallest_client
from app.schemas.call import (
    CallOutbound,
    CallResponse,
    CallSummaryResponse,
    CallTranscriptResponse,
)
from app.services.phone_numbers import is_number_on_tenant_dnc, tenant_phone_dnc_lock
from app.telephony.base import CallRequest
from app.telephony.twilio_provider import get_telephony_provider

router = APIRouter(prefix="/calls", tags=["Calls"])
CALL_IDEMPOTENCY_NAMESPACE = UUID("0a259bf8-ed0d-43d7-94b5-e9dcb5dc3d31")
logger = structlog.get_logger()


def _call_request_identity(data: CallOutbound) -> dict:
    return {
        "agent_id": str(data.agent_id),
        "to_number": data.to_number,
        "from_number": data.from_number,
        "context": data.context,
    }


def _ensure_same_idempotent_request(call: Call, request_identity: dict) -> None:
    if (call.call_metadata or {}).get("request", {}) != request_identity:
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was already used for a different call request",
        )


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
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Initiate at most one outbound call for a tenant idempotency key."""
    call_id = uuid5(
        CALL_IDEMPOTENCY_NAMESPACE,
        f"{current_user.tenant_id}:{idempotency_key}",
    )
    existing_result = await db.execute(
        select(Call).where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
    )
    existing_call = existing_result.scalar_one_or_none()
    request_identity = _call_request_identity(data)
    if existing_call:
        _ensure_same_idempotent_request(existing_call, request_identity)
        return CallResponse.model_validate(existing_call)

    # Verify agent
    result = await db.execute(
        select(Agent).where(Agent.id == data.agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="Agent is inactive")
    if agent.voice_provider not in {"smallest", "twilio"}:
        raise HTTPException(status_code=422, detail="Agent voice provider is not supported")

    if await is_number_on_tenant_dnc(db, current_user.tenant_id, data.to_number):
        raise HTTPException(
            status_code=409,
            detail="Phone number is on this workspace's Do Not Call list",
        )

    is_smallest = agent.voice_provider == "smallest"
    if is_smallest and not agent.provider_agent_id:
        raise HTTPException(
            status_code=409,
            detail="Provision this agent on Smallest.ai before placing a call",
        )
    if is_smallest and (not agent.provider_revision_id or not agent.last_synced_at):
        raise HTTPException(
            status_code=409,
            detail="Agent cannot place calls until its initial provider revision is published",
        )
    if is_smallest and data.from_number is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Smallest.ai caller identity is server-managed and cannot be supplied by clients"
            ),
        )
    from_number = (
        "provider-managed"
        if is_smallest
        else data.from_number or settings.twilio_default_from_number
    )
    if not from_number and not is_smallest:
        raise HTTPException(
            status_code=400,
            detail="No from_number provided and no default configured",
        )
    from_number = from_number or "provider-managed"
    provider_identity = (agent.voice_provider, agent.provider_agent_id, from_number)

    # Create call record
    call = Call(
        id=call_id,
        tenant_id=current_user.tenant_id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number=from_number,
        to_number=data.to_number,
        provider="smallest" if is_smallest else "twilio",
        call_metadata={"request": request_identity},
    )
    db.add(call)
    try:
        # Persist the durable dispatch claim before the paid provider side
        # effect. A crash leaves a reconcilable `dispatching` record; a retry
        # with the same key never dials again.
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raced_result = await db.execute(
            select(Call).where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
        )
        raced_call = raced_result.scalar_one_or_none()
        if raced_call:
            _ensure_same_idempotent_request(raced_call, request_identity)
            return CallResponse.model_validate(raced_call)
        raise

    from app.tasks.call_tasks import (
        DIRECT_CALL_WATCHDOG_STATUSES,
        DISPATCH_RECONCILE_DELAY_SECONDS,
        arm_direct_call_terminal_watchdog,
        reconcile_call_dispatch,
        reconcile_direct_call_terminal,
    )

    try:
        reconcile_call_dispatch.apply_async(
            args=(str(call.id), str(current_user.tenant_id)),
            countdown=DISPATCH_RECONCILE_DELAY_SECONDS,
        )
    except Exception:
        # Never place a paid call unless an ambiguous/crashed dispatch has a
        # durable path to a visible terminal state.
        call.status = "failed"
        call.call_metadata = {
            **(call.call_metadata or {}),
            "dispatch_error": "reconciliation_unavailable",
        }
        await db.commit()
        return CallResponse.model_validate(call)

    provider_call_sid: str | None = None
    dispatch_error: Exception | None = None
    dispatch_is_ambiguous = True
    async with tenant_phone_dnc_lock(db, current_user.tenant_id, data.to_number):
        # This check and provider invocation share the same tenant+number lock
        # as DNC POST/DELETE, closing the final check-to-call race across API
        # replicas. The transaction ends before the local guard is released.
        if await is_number_on_tenant_dnc(db, current_user.tenant_id, data.to_number):
            call.status = "failed"
            call.call_metadata = {**(call.call_metadata or {}), "dispatch_error": "dnc"}
            await db.commit()
            return CallResponse.model_validate(call)

        current_agent = (
            await db.execute(
                select(Agent)
                .where(
                    Agent.id == data.agent_id,
                    Agent.tenant_id == current_user.tenant_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        current_from_number = (
            "provider-managed"
            if current_agent and current_agent.voice_provider == "smallest"
            else data.from_number or settings.twilio_default_from_number
        )
        provider_identity_changed = bool(
            current_agent
            and (
                current_agent.voice_provider,
                current_agent.provider_agent_id,
                current_from_number,
            )
            != provider_identity
        )
        provider_not_ready = bool(
            current_agent
            and current_agent.voice_provider == "smallest"
            and (
                not current_agent.provider_agent_id
                or not current_agent.provider_revision_id
                or not current_agent.last_synced_at
            )
        )
        if (
            current_agent is None
            or not current_agent.is_active
            or provider_identity_changed
            or provider_not_ready
        ):
            reason = (
                "agent_inactive"
                if current_agent is not None and not current_agent.is_active
                else "agent_provider_changed"
            )
            call.status = "failed"
            call.call_metadata = {**(call.call_metadata or {}), "dispatch_error": reason}
            await db.commit()
            return CallResponse.model_validate(call)

        try:
            if is_smallest:
                provider_call_sid = await get_smallest_client().start_outbound_call(
                    agent_id=current_agent.provider_agent_id,
                    phone_number=data.to_number,
                    variables={**data.context, "_vav_call_id": str(call.id)},
                )
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
                provider_call_sid = provider_result.provider_call_sid
        except SmallestAIError as exc:
            dispatch_error = exc
            dispatch_is_ambiguous = exc.ambiguous
        except Exception as exc:
            dispatch_error = exc
        await db.commit()

    # A signed callback may update this row while the provider request is in
    # flight. Re-lock and repopulate before merging the request result so a late
    # HTTP response can never regress an answered/completed call.
    db.expire(call)
    current_result = await db.execute(
        select(Call)
        .where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current_call = current_result.scalar_one()
    if dispatch_error is None and provider_call_sid:
        if current_call.provider_call_sid and current_call.provider_call_sid != provider_call_sid:
            current_call.call_metadata = {
                **(current_call.call_metadata or {}),
                "dispatch_error": "provider_id_conflict",
            }
        else:
            current_call.provider_call_sid = current_call.provider_call_sid or provider_call_sid
            if current_call.status == "dispatching":
                current_call.status = "ringing"
                current_call.started_at = current_call.started_at or datetime.now(UTC)
    elif dispatch_error is not None and current_call.status == "dispatching":
        # Transport/timeout/5xx failures are ambiguous because the provider may
        # already have accepted the call. A provider 4xx response is definitive.
        current_call.status = "dispatch_unknown" if dispatch_is_ambiguous else "failed"
        current_call.call_metadata = {
            **(current_call.call_metadata or {}),
            "dispatch_error": type(dispatch_error).__name__,
        }

    terminal_watchdog_deadline = None
    if (
        current_call.direction == "outbound"
        and current_call.campaign_id is None
        and current_call.provider_call_sid
        and current_call.status in DIRECT_CALL_WATCHDOG_STATUSES
    ):
        terminal_watchdog_deadline = arm_direct_call_terminal_watchdog(
            current_call,
            current_agent.max_call_duration_seconds,
        )

    await db.commit()
    if terminal_watchdog_deadline is not None:
        try:
            reconcile_direct_call_terminal.apply_async(
                args=(str(current_call.id), str(current_user.tenant_id)),
                eta=terminal_watchdog_deadline,
            )
        except Exception as exc:
            # The periodic recovery scan uses the persisted deadline, so a
            # broker outage cannot strand an accepted call indefinitely.
            logger.warning(
                "direct_call_terminal_watchdog_enqueue_failed",
                call_id=str(current_call.id),
                error_type=type(exc).__name__,
            )
    return CallResponse.model_validate(current_call)


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
