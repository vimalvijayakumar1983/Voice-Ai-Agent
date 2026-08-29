"""Call management endpoints - logs, transcripts, summaries, and outbound calls."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid5

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
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
    BrowserConversationRegister,
    CallOutbound,
    CallResponse,
    CallSummaryResponse,
    CallTranscriptResponse,
    ProviderHistorySyncResponse,
)
from app.services.audit import record_audit_event
from app.services.call_metadata import agent_configuration_snapshot
from app.services.campaign_lifecycle import TERMINAL_CALL_STATUSES
from app.services.compliance_policy import (
    is_outbound_consent_revoked,
    is_recording_consent_revoked,
)
from app.services.phone_numbers import is_number_on_tenant_dnc, tenant_phone_dnc_lock
from app.services.recordings import RecordingError, fetch_call_recording
from app.telephony.base import CallRequest
from app.telephony.twilio_provider import get_telephony_provider

router = APIRouter(prefix="/calls", tags=["Calls"])
CALL_IDEMPOTENCY_NAMESPACE = UUID("0a259bf8-ed0d-43d7-94b5-e9dcb5dc3d31")
logger = structlog.get_logger()
MAX_PROVIDER_HISTORY_RECORDS = 100


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


def _ensure_browser_conversation_identity(
    call: Call,
    *,
    tenant_id: UUID,
    agent_id: UUID,
) -> None:
    metadata = call.call_metadata or {}
    conversation_type = metadata.get("conversation_type")
    channel = metadata.get("channel")
    if (
        call.tenant_id != tenant_id
        or call.agent_id != agent_id
        or call.provider != "smallest"
        or call.direction != "inbound"
        or conversation_type in {"telephonyInbound", "telephonyOutbound"}
        or channel == "phone"
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider conversation is not this agent's browser session",
        )


def _mark_browser_conversation_started(call: Call, agent_snapshot: dict) -> None:
    now = datetime.now(UTC)
    if call.status not in TERMINAL_CALL_STATUSES:
        call.status = "in_progress"
        call.started_at = call.started_at or now
        call.answered_at = call.answered_at or now
    metadata = dict(call.call_metadata or {})
    metadata.setdefault("agent_configuration", agent_snapshot)
    metadata["conversation_type"] = "webcall"
    metadata["channel"] = "browser"
    call.call_metadata = metadata


def _provider_history_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _provider_history_status(value: object) -> str:
    status = str(value or "").lower().replace("-", "_")
    return {
        "pending": "initiated",
        "in_queue": "initiated",
        "processing": "in_progress",
        "active": "in_progress",
        "in_progress": "in_progress",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "failed",
        "canceled": "failed",
        "no_answer": "no_answer",
        "busy": "busy",
    }.get(status, "initiated")


async def _sync_provider_conversation_detail(
    db: AsyncSession,
    *,
    call: Call,
    detail: dict,
) -> None:
    turns = detail.get("transcript")
    if isinstance(turns, list) and all(isinstance(turn, dict) for turn in turns):
        transcript = await db.scalar(
            select(CallTranscript).where(CallTranscript.call_id == call.id)
        )
        full_text = "\n".join(
            f"{str(turn.get('role', 'unknown')).title()}: {turn.get('content', '')}"
            for turn in turns
        )
        if transcript:
            transcript.turns = turns
            transcript.full_text = full_text
        else:
            db.add(
                CallTranscript(
                    tenant_id=call.tenant_id,
                    call_id=call.id,
                    turns=turns,
                    full_text=full_text,
                )
            )

    analytics = detail.get("postCallAnalytics")
    if not isinstance(analytics, dict):
        return
    summary_text = analytics.get("summary")
    if isinstance(summary_text, str) and summary_text.strip():
        summary = await db.scalar(select(CallSummary).where(CallSummary.call_id == call.id))
        dispositions = analytics.get("dispositionMetrics")
        metrics = dispositions if isinstance(dispositions, list) else []
        if summary:
            summary.summary = summary_text.strip()
        else:
            db.add(
                CallSummary(
                    tenant_id=call.tenant_id,
                    call_id=call.id,
                    summary=summary_text.strip(),
                    key_topics=[],
                    action_items=[],
                    sentiment=None,
                )
            )
        first_metric = next((metric for metric in metrics if isinstance(metric, dict)), None)
        if first_metric and first_metric.get("value"):
            call.disposition = str(first_metric["value"])[:50]


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


@router.post("/sync-provider-history", response_model=ProviderHistorySyncResponse)
async def sync_provider_history(
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Backfill a bounded page of Smallest.ai web calls for this workspace."""
    agents = (
        await db.scalars(
            select(Agent).where(
                Agent.tenant_id == current_user.tenant_id,
                Agent.voice_provider == "smallest",
                Agent.provider_agent_id.is_not(None),
            )
        )
    ).all()
    client = get_smallest_client()
    scanned = imported = updated = failed = 0

    for agent in agents:
        if scanned >= MAX_PROVIDER_HISTORY_RECORDS:
            break
        remaining = MAX_PROVIDER_HISTORY_RECORDS - scanned
        try:
            logs = await client.list_web_conversation_logs(
                agent_id=agent.provider_agent_id,
                limit=min(remaining, 50),
            )
        except SmallestAIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        agent_snapshot = agent_configuration_snapshot(agent)
        for provider_summary in logs:
            if scanned >= MAX_PROVIDER_HISTORY_RECORDS:
                break
            scanned += 1
            provider_call_id = provider_summary.get("callId")
            provider_call_type = str(provider_summary.get("callType") or "").lower()
            provider_agent_id = provider_summary.get("agentId")
            if (
                not isinstance(provider_call_id, str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", provider_call_id)
                or provider_call_type != "webcall"
                or (
                    isinstance(provider_agent_id, str)
                    and provider_agent_id
                    and provider_agent_id != agent.provider_agent_id
                )
            ):
                failed += 1
                continue

            try:
                detail = await client.get_conversation_log(call_id=provider_call_id)
            except SmallestAIError:
                failed += 1
                continue
            detail_type = str(detail.get("type") or "webcall").lower()
            if detail_type != "webcall":
                failed += 1
                continue

            call = await db.scalar(select(Call).where(Call.provider_call_sid == provider_call_id))
            if call:
                _ensure_browser_conversation_identity(
                    call,
                    tenant_id=current_user.tenant_id,
                    agent_id=agent.id,
                )
                updated += 1
            else:
                started_at = _provider_history_timestamp(provider_summary.get("timestamp"))
                call = Call(
                    tenant_id=current_user.tenant_id,
                    agent_id=agent.id,
                    direction="inbound",
                    status="initiated",
                    from_number="browser",
                    to_number="voice-agent",
                    provider="smallest",
                    provider_call_sid=provider_call_id,
                    started_at=started_at,
                    answered_at=started_at,
                    created_at=started_at or datetime.now(UTC),
                    call_metadata={
                        "agent_configuration": agent_snapshot,
                        "conversation_type": "webcall",
                        "channel": "browser",
                    },
                )
                try:
                    async with db.begin_nested():
                        db.add(call)
                        await db.flush()
                except IntegrityError:
                    call = await db.scalar(
                        select(Call).where(Call.provider_call_sid == provider_call_id)
                    )
                    if not call:
                        raise
                    _ensure_browser_conversation_identity(
                        call,
                        tenant_id=current_user.tenant_id,
                        agent_id=agent.id,
                    )
                    updated += 1
                else:
                    imported += 1

            provider_status = _provider_history_status(
                detail.get("status") or provider_summary.get("callStatus")
            )
            if (
                call.status not in TERMINAL_CALL_STATUSES
                or provider_status in TERMINAL_CALL_STATUSES
            ):
                call.status = provider_status
            duration = detail.get("duration")
            if isinstance(duration, (int, float)) and duration >= 0:
                call.duration_seconds = round(duration)
            else:
                duration_ms = provider_summary.get("callDurationMs")
                if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
                    call.duration_seconds = round(duration_ms / 1000)
            started_at = call.started_at or _provider_history_timestamp(
                provider_summary.get("timestamp")
            )
            call.started_at = started_at
            call.answered_at = call.answered_at or started_at
            if call.status in TERMINAL_CALL_STATUSES and started_at and call.duration_seconds:
                call.ended_at = started_at + timedelta(seconds=call.duration_seconds)
            metadata = dict(call.call_metadata or {})
            metadata.setdefault("agent_configuration", agent_snapshot)
            metadata["conversation_type"] = "webcall"
            metadata["channel"] = "browser"
            metadata["provider_history_synced_at"] = datetime.now(UTC).isoformat()
            call.call_metadata = metadata
            await _sync_provider_conversation_detail(db, call=call, detail=detail)

    await db.commit()
    return ProviderHistorySyncResponse(
        scanned=scanned,
        imported=imported,
        updated=updated,
        failed=failed,
    )


@router.post("/browser-sessions", response_model=CallResponse)
async def register_browser_conversation(
    data: BrowserConversationRegister,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Idempotently bind a started Smallest.ai web call to local history."""
    agent = await db.scalar(
        select(Agent).where(
            Agent.id == data.agent_id,
            Agent.tenant_id == current_user.tenant_id,
        )
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="Agent is inactive")
    if agent.voice_provider != "smallest" or not agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Agent is not provisioned on Smallest.ai")
    agent_id = agent.id
    agent_snapshot = agent_configuration_snapshot(agent)

    existing = await db.scalar(
        select(Call).where(Call.provider_call_sid == data.provider_call_id)
    )
    if existing:
        _ensure_browser_conversation_identity(
            existing,
            tenant_id=current_user.tenant_id,
            agent_id=agent_id,
        )
        _mark_browser_conversation_started(existing, agent_snapshot)
        await db.commit()
        return CallResponse.model_validate(existing)

    now = datetime.now(UTC)
    call = Call(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        direction="inbound",
        status="in_progress",
        from_number="browser",
        to_number="voice-agent",
        provider="smallest",
        provider_call_sid=data.provider_call_id,
        started_at=now,
        answered_at=now,
        call_metadata={
            "agent_configuration": agent_snapshot,
            "conversation_type": "webcall",
            "channel": "browser",
        },
    )
    db.add(call)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raced_call = await db.scalar(
            select(Call).where(Call.provider_call_sid == data.provider_call_id)
        )
        if not raced_call:
            raise
        _ensure_browser_conversation_identity(
            raced_call,
            tenant_id=current_user.tenant_id,
            agent_id=agent_id,
        )
        _mark_browser_conversation_started(raced_call, agent_snapshot)
        await db.commit()
        return CallResponse.model_validate(raced_call)
    return CallResponse.model_validate(call)


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
    if await is_outbound_consent_revoked(db, current_user.tenant_id, data.to_number):
        raise HTTPException(
            status_code=409,
            detail="Outbound consent has been revoked for this phone number",
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
    if is_smallest and agent.sync_status != "synced":
        raise HTTPException(
            status_code=409,
            detail="Publish and verify the agent's current changes before placing a call",
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
    provider_identity = (
        agent.voice_provider,
        agent.provider_agent_id,
        agent.provider_revision_id,
        from_number,
    )

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
        call_metadata={
            "request": request_identity,
            "agent_configuration": agent_configuration_snapshot(agent),
        },
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
        if await is_outbound_consent_revoked(
            db,
            current_user.tenant_id,
            data.to_number,
        ):
            call.status = "failed"
            call.call_metadata = {
                **(call.call_metadata or {}),
                "dispatch_error": "consent_revoked",
            }
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
                current_agent.provider_revision_id,
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
                or current_agent.sync_status != "synced"
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
                else ("agent_not_synced" if provider_not_ready else "agent_provider_changed")
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
                    version_id=current_agent.provider_revision_id,
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


@router.get(
    "/{call_id}/recording",
    response_class=Response,
    responses={
        200: {
            "description": "Tenant-authorized call recording audio",
            "content": {
                "audio/mpeg": {},
                "audio/wav": {},
                "audio/ogg": {},
                "audio/mp4": {},
            },
        }
    },
)
async def get_call_recording(
    call_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Return bounded audio without revealing the provider's recording URL."""
    result = await db.execute(
        select(Call).where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    customer_number = call.to_number if call.direction == "outbound" else call.from_number

    async def reject_revoked_recording_access() -> None:
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="call.recording_access_blocked",
            resource_type="call",
            resource_id=str(call.id),
            details={"reason": "recording_consent_revoked"},
        )
        await db.commit()
        raise HTTPException(
            status_code=403,
            detail="Recording playback is blocked by an explicit customer revocation",
        )

    if await is_recording_consent_revoked(
        db,
        current_user.tenant_id,
        customer_number,
    ):
        await reject_revoked_recording_access()

    try:
        recording = await fetch_call_recording(call)
    except RecordingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # A revocation may commit while the provider bytes are being retrieved.
    # Re-read immediately before release so such a race never reaches the
    # browser; the first check above avoids an unnecessary provider fetch.
    if await is_recording_consent_revoked(
        db,
        current_user.tenant_id,
        customer_number,
    ):
        await reject_revoked_recording_access()

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="call.recording_accessed",
        resource_type="call",
        resource_id=str(call.id),
        details={"provider": call.provider, "bytes": len(recording.content)},
    )
    await db.commit()

    return Response(
        content=recording.content,
        media_type=recording.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="call-recording.{recording.extension}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


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
