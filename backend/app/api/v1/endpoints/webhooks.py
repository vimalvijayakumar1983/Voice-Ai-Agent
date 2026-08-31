"""Verified webhook handlers for telephony and voice-provider events."""

import asyncio
import hashlib
import hmac
import json
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call, CallSummary, CallTranscript
from app.models.campaign import Campaign, CampaignContact, CampaignContactAttempt
from app.realtime.auth import create_media_token
from app.services.call_metadata import agent_configuration_snapshot
from app.services.campaign_lifecycle import (
    TERMINAL_CALL_STATUSES,
    CampaignLifecycleResult,
    sync_campaign_call_lifecycle,
)
from app.services.provider_callback_outbox import persist_provider_callback_actions
from app.services.provider_credentials import ProviderCredentialError, load_provider_config
from app.telephony.twilio_provider import get_telephony_provider

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = structlog.get_logger()
MAX_PROVIDER_WEBHOOK_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_FORM_FIELDS = 100
_LOCAL_CALLBACK_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


@dataclass(frozen=True)
class ProviderWebhookEffects:
    lifecycle: CampaignLifecycleResult = CampaignLifecycleResult()
    source_call_id: str | None = None
    source_tenant_id: str | None = None
    source_campaign_id: str | None = None
    process_call_id: str | None = None
    process_tenant_id: str | None = None
    process_revision: str | None = None
    process_event_type: str | None = None


def verify_smallest_signature(raw_body: bytes, signature: str) -> bool:
    """Validate Smallest.ai's hex-encoded HMAC-SHA256 signature."""
    if not settings.smallest_webhook_secret or not signature:
        return False
    expected = hmac.new(
        settings.smallest_webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


async def _read_bounded_webhook_body(
    request: Request,
    *,
    max_bytes: int = MAX_PROVIDER_WEBHOOK_BYTES,
) -> bytes:
    """Read a provider body with a hard streaming ceiling before verification."""
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            parsed_length = int(declared_length)
        except ValueError:
            parsed_length = -1
        if parsed_length > max_bytes:
            raise HTTPException(status_code=413, detail="Provider webhook body is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Provider webhook body is too large")
    return bytes(body)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _smallest_direction(conversation_type: str) -> str:
    normalized = "".join(
        character for character in conversation_type.lower() if character.isalnum()
    )
    if normalized in {"webcall", "web", "chat"}:
        return "inbound"
    return "inbound" if "inbound" in normalized else "outbound"


def _smallest_conversation_type(metadata: dict, variables: dict) -> str:
    """Resolve the provider channel, including its reserved web-call variable."""
    raw_value = next(
        (
            value.strip()
            for value in (
                metadata.get("conversationType"),
                variables.get("conversation_type"),
            )
            if isinstance(value, str) and value.strip()
        ),
        "telephonyOutbound",
    )
    normalized = "".join(character for character in raw_value.lower() if character.isalnum())
    canonical = {
        "telephonyinbound": "telephonyInbound",
        "telephonyoutbound": "telephonyOutbound",
        "web": "webcall",
        "webcall": "webcall",
        "chat": "chat",
    }
    return canonical.get(normalized, raw_value.strip()[:100])


def _smallest_channel(conversation_type: str) -> str:
    normalized = "".join(
        character for character in conversation_type.lower() if character.isalnum()
    )
    if normalized in {"web", "webcall"}:
        return "browser"
    if normalized == "chat":
        return "chat"
    return "phone"


async def _acquire_provider_callback_db_lock(db: AsyncSession, identity: str) -> None:
    """Serialize one provider call across API replicas for this transaction."""
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )


@asynccontextmanager
async def _local_provider_callback_lock(identity: str):
    """Mirror the database lock inside one process and SQLite tests."""
    lock = _LOCAL_CALLBACK_LOCKS.get(identity)
    if lock is None:
        lock = asyncio.Lock()
        _LOCAL_CALLBACK_LOCKS[identity] = lock
    async with lock:
        yield


def _merge_smallest_terminal_call_data(
    call: Call,
    call_data: dict,
    metadata: dict,
    call_metadata: dict,
) -> None:
    failure_status_map = {
        "no-answer": "no_answer",
        "canceled": "failed",
        "cancelled": "failed",
        "failed": "failed",
        "busy": "busy",
    }
    remote_status = str(call_data.get("callStatus") or "completed")
    if call.status not in TERMINAL_CALL_STATUSES:
        call.status = failure_status_map.get(remote_status, "completed")
    call_metadata["smallest_remote_call_status"] = remote_status
    call.from_number = str(call_data.get("fromNumber") or call.from_number)
    call.to_number = str(call_data.get("toNumber") or call.to_number)
    call.answered_at = _parse_timestamp(call_data.get("answerTime")) or call.answered_at
    call.ended_at = _parse_timestamp(call_data.get("endTime")) or datetime.now(UTC)
    duration = call_data.get("callDuration")
    if isinstance(duration, (int, float)):
        call.duration_seconds = round(duration)
    call.provider_recording_url = metadata.get("recordingUrl") or call.provider_recording_url


def _uuid_variable(variables: dict, name: str) -> UUID:
    value = variables.get(name)
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Smallest.ai campaign correlation field: {name}",
        ) from exc


async def _smallest_campaign_call_from_variables(
    db: AsyncSession,
    agent: Agent,
    variables: dict,
) -> tuple[Call | None, CampaignContactAttempt | None]:
    """Strictly bind a Smallest callback to a precommitted campaign call."""
    if "_voice_ai_call_id" not in variables:
        return None, None

    call_id = _uuid_variable(variables, "_voice_ai_call_id")
    campaign_id = _uuid_variable(variables, "_voice_ai_campaign_id")
    contact_id = _uuid_variable(variables, "_voice_ai_contact_id")
    attempt_id = _uuid_variable(variables, "_voice_ai_attempt_id")
    idempotency_key = variables.get("_voice_ai_idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Invalid Smallest.ai campaign idempotency key",
        )

    call = (
        await db.execute(
            select(Call).where(
                Call.id == call_id,
                Call.tenant_id == agent.tenant_id,
                Call.agent_id == agent.id,
                Call.campaign_id == campaign_id,
                Call.provider == "smallest",
            )
        )
    ).scalar_one_or_none()
    attempt = (
        await db.execute(
            select(CampaignContactAttempt).where(
                CampaignContactAttempt.id == attempt_id,
                CampaignContactAttempt.tenant_id == agent.tenant_id,
                CampaignContactAttempt.campaign_id == campaign_id,
                CampaignContactAttempt.contact_id == contact_id,
                CampaignContactAttempt.call_id == call_id,
                CampaignContactAttempt.idempotency_key == idempotency_key,
                CampaignContactAttempt.provider == "smallest",
            )
        )
    ).scalar_one_or_none()
    if call is None or attempt is None:
        raise HTTPException(
            status_code=409,
            detail="Smallest.ai campaign correlation does not match a local attempt",
        )
    return call, attempt


async def _smallest_local_call_from_variables(
    db: AsyncSession,
    agent: Agent,
    variables: dict,
) -> tuple[Call | None, CampaignContactAttempt | None]:
    # `_vav_call_id` is generated after tenant variables are sanitized and is
    # authoritative for both direct and campaign dispatches. Inspect that Call
    # first so caller-supplied `_voice_ai_*` values cannot force a legitimate
    # direct callback down the campaign parser.
    if "_vav_call_id" in variables:
        call_id = _uuid_variable(variables, "_vav_call_id")
        call = (
            await db.execute(
                select(Call).where(
                    Call.id == call_id,
                    Call.tenant_id == agent.tenant_id,
                    Call.agent_id == agent.id,
                    Call.provider == "smallest",
                    Call.direction == "outbound",
                )
            )
        ).scalar_one_or_none()
        if call is None:
            raise HTTPException(
                status_code=409,
                detail="Smallest.ai call correlation does not match a local dispatch",
            )
        if call.campaign_id is None:
            return call, None
        if "_voice_ai_call_id" not in variables:
            raise HTTPException(
                status_code=409,
                detail="Smallest.ai campaign correlation fields are missing",
            )
        return await _smallest_campaign_call_from_variables(db, agent, variables)

    if "_voice_ai_call_id" in variables:
        return await _smallest_campaign_call_from_variables(db, agent, variables)
    return None, None


async def _lock_callback_call_graph(
    db: AsyncSession,
    call_probe: Call,
) -> tuple[Call, CampaignContactAttempt | None]:
    """Lock a campaign call in dispatcher order before merging a callback."""
    locked_attempt = None
    if call_probe.campaign_id is not None:
        campaign = (
            await db.execute(
                select(Campaign)
                .where(
                    Campaign.id == call_probe.campaign_id,
                    Campaign.tenant_id == call_probe.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=409, detail="Call campaign mapping is invalid")

        attempt_probe = (
            await db.execute(
                select(CampaignContactAttempt).where(
                    CampaignContactAttempt.call_id == call_probe.id,
                    CampaignContactAttempt.campaign_id == campaign.id,
                    CampaignContactAttempt.tenant_id == campaign.tenant_id,
                )
            )
        ).scalar_one_or_none()
        contact_query = select(CampaignContact).where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.tenant_id == campaign.tenant_id,
        )
        if attempt_probe is not None:
            contact_query = contact_query.where(CampaignContact.id == attempt_probe.contact_id)
        else:
            contact_query = contact_query.where(CampaignContact.last_call_id == call_probe.id)
        contact = (
            await db.execute(
                contact_query.with_for_update().execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if contact is None:
            raise HTTPException(status_code=409, detail="Call contact mapping is invalid")
        if attempt_probe is not None:
            locked_attempt = (
                await db.execute(
                    select(CampaignContactAttempt)
                    .where(
                        CampaignContactAttempt.id == attempt_probe.id,
                        CampaignContactAttempt.contact_id == contact.id,
                        CampaignContactAttempt.campaign_id == campaign.id,
                        CampaignContactAttempt.tenant_id == campaign.tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if locked_attempt is None:
                raise HTTPException(status_code=409, detail="Call attempt mapping is invalid")

    call = (
        await db.execute(
            select(Call)
            .where(
                Call.id == call_probe.id,
                Call.tenant_id == call_probe.tenant_id,
                Call.agent_id == call_probe.agent_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=409, detail="Call mapping changed during callback")
    return call, locked_attempt


async def _upsert_call_transcript(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    call_id: UUID,
    turns: list,
    full_text: str,
) -> None:
    transcript = (
        await db.execute(
            select(CallTranscript)
            .where(
                CallTranscript.call_id == call_id,
                CallTranscript.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if transcript is None:
        try:
            async with db.begin_nested():
                transcript = CallTranscript(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    turns=turns,
                    full_text=full_text,
                )
                db.add(transcript)
                await db.flush()
        except IntegrityError:
            transcript = (
                await db.execute(
                    select(CallTranscript)
                    .where(
                        CallTranscript.call_id == call_id,
                        CallTranscript.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
    transcript.turns = turns
    transcript.full_text = full_text


async def _upsert_call_summary(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    call_id: UUID,
    summary_text: str,
    analytics: dict,
) -> None:
    summary = (
        await db.execute(
            select(CallSummary)
            .where(
                CallSummary.call_id == call_id,
                CallSummary.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if summary is None:
        try:
            async with db.begin_nested():
                summary = CallSummary(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    summary=summary_text,
                    key_topics=analytics.get("keyTopics") or [],
                    action_items=analytics.get("actionItems") or [],
                    sentiment=analytics.get("sentiment"),
                )
                db.add(summary)
                await db.flush()
        except IntegrityError:
            summary = (
                await db.execute(
                    select(CallSummary)
                    .where(
                        CallSummary.call_id == call_id,
                        CallSummary.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
    summary.summary = summary_text
    summary.key_topics = analytics.get("keyTopics") or []
    summary.action_items = analytics.get("actionItems") or []
    summary.sentiment = analytics.get("sentiment")


async def _persist_webhook_effects(
    db: AsyncSession,
    effects: ProviderWebhookEffects,
) -> tuple[UUID, ...]:
    if not effects.source_call_id or not effects.source_tenant_id:
        return ()
    return await persist_provider_callback_actions(
        db,
        call_id=UUID(effects.source_call_id),
        tenant_id=UUID(effects.source_tenant_id),
        campaign_id=(UUID(effects.source_campaign_id) if effects.source_campaign_id else None),
        process_completed_call=bool(effects.process_call_id),
        process_revision=effects.process_revision,
        process_event_type=effects.process_event_type,
        continue_campaign=effects.lifecycle.should_dispatch,
    )


def _kick_provider_outbox(outbox_ids: tuple[UUID, ...]) -> None:
    """Best-effort low-latency kick; Beat drains anything left pending."""
    from app.tasks.campaign_tasks import dispatch_provider_callback_outbox

    for outbox_id in outbox_ids:
        try:
            dispatch_provider_callback_outbox.delay(str(outbox_id))
        except Exception as exc:
            logger.warning(
                "provider_callback_outbox_kick_failed",
                outbox_id=str(outbox_id),
                error_type=type(exc).__name__,
            )


def _twilio_validation_url(request: Request) -> str:
    """Rebuild the public callback URL Twilio used when signing the request."""
    url = f"{settings.base_url.rstrip('/')}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


async def _parse_twilio_request(request: Request) -> dict[str, str]:
    cached = getattr(request.state, "twilio_params", None)
    if isinstance(cached, dict):
        return cached
    raw_body = await _read_bounded_webhook_body(request)
    try:
        pairs = parse_qsl(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=MAX_PROVIDER_FORM_FIELDS,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Twilio webhook form") from exc
    params = dict(pairs)
    request.state.twilio_params = params
    return params


def _validate_twilio_request(
    request: Request,
    params: dict[str, str],
    auth_token: str,
) -> None:
    """Reject callbacks that are not signed for the resolved workspace."""
    if not auth_token:
        raise HTTPException(status_code=503, detail="Twilio webhook validation is not configured")
    if not _twilio_request_is_valid(request, params, auth_token):
        raise HTTPException(status_code=401, detail="Invalid Twilio webhook signature")


def _twilio_request_is_valid(
    request: Request,
    params: dict[str, str],
    auth_token: str,
) -> bool:
    """Check a signature without revealing which tenant credential matched."""
    if not auth_token:
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    provider = get_telephony_provider(auth_token=auth_token)
    return bool(
        signature and provider.validate_webhook(_twilio_validation_url(request), params, signature)
    )


async def _verify_twilio_request(
    request: Request,
    *,
    auth_token: str | None = None,
) -> dict[str, str]:
    params = await _parse_twilio_request(request)
    _validate_twilio_request(request, params, auth_token or settings.twilio_auth_token)
    return params


async def _tenant_twilio_config(db: AsyncSession, tenant_id: UUID) -> dict:
    return await load_provider_config(db, tenant_id, "twilio") or {}


def _runtime_stream_url(call_id: UUID) -> str:
    public = urlsplit(settings.base_url)
    websocket_origin = urlunsplit(
        ("wss" if public.scheme == "https" else "ws", public.netloc, "", "", "")
    )
    return f"{websocket_origin}/api/v1/realtime/twilio/{call_id}"


def _runtime_stream_parameters(call_id: UUID) -> dict[str, str]:
    # Twilio does not forward query strings on <Stream> URLs. Custom
    # parameters arrive in the signed stream's `start` event instead.
    return {"token": create_media_token(call_id)}


async def _process_smallest_webhook(
    db: AsyncSession,
    payload: dict,
) -> ProviderWebhookEffects:
    """Idempotently merge an Atoms lifecycle event into local call intelligence."""
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="Invalid Smallest.ai metadata")

    provider_agent_id = metadata.get("agentId")
    provider_call_id = metadata.get("callId")
    event_type = metadata.get("eventType")
    identifiers = (provider_agent_id, provider_call_id, event_type)
    if not all(isinstance(value, str) and value for value in identifiers):
        raise HTTPException(status_code=400, detail="Missing Smallest.ai event identifiers")
    callback_identity = f"smallest:{provider_agent_id}:{provider_call_id}"
    await _acquire_provider_callback_db_lock(db, callback_identity)

    agent_result = await db.execute(
        select(Agent).where(Agent.provider_agent_id == provider_agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Smallest.ai agent mapping not found")

    variables = metadata.get("variables") or {}
    if not isinstance(variables, dict):
        variables = {}
    # The provider identity is globally unique. Do not hide a conflicting
    # tenant/agent row behind a scoped query and then fail on UNIQUE at flush.
    call_result = await db.execute(select(Call).where(Call.provider_call_sid == provider_call_id))
    call = call_result.scalar_one_or_none()
    if call is not None and (call.tenant_id != agent.tenant_id or call.agent_id != agent.id):
        raise HTTPException(
            status_code=409,
            detail="Smallest.ai call identity belongs to another agent",
        )
    correlated_attempt = None
    if call is None:
        try:
            call, correlated_attempt = await _smallest_local_call_from_variables(
                db,
                agent,
                variables,
            )
        except HTTPException:
            # Callback variables may contain legacy caller-supplied keys. A
            # malformed tuple is not authoritative and must not turn a signed,
            # best-effort provider event into a permanently lost 4xx. Unknown
            # outbound events are safely ACKed below without inventing a Call.
            call = None
            correlated_attempt = None
    call_data = metadata.get("callData") or {}
    if not isinstance(call_data, dict):
        call_data = {}
    conversation_type = _smallest_conversation_type(metadata, variables)

    # Smallest pre-conversation webhooks do not include variables. If this
    # event wins the race with our outbound API response, creating a second
    # unbound Call would consume the unique provider SID and prevent the
    # durable local claim from reconciling. A later correlated post event or a
    # committed provider response can bind the local claim; an uncorrelated
    # event must never invent a competing outbound Call.
    if call is None and _smallest_direction(conversation_type) == "outbound":
        return ProviderWebhookEffects()

    if call is not None:
        call, locked_attempt = await _lock_callback_call_graph(db, call)
        if correlated_attempt is not None and (
            locked_attempt is None or locked_attempt.id != correlated_attempt.id
        ):
            raise HTTPException(
                status_code=409,
                detail="Smallest.ai campaign attempt identity changed",
            )
        if call.provider_call_sid and call.provider_call_sid != provider_call_id:
            raise HTTPException(
                status_code=409,
                detail="Smallest.ai returned a conflicting call identity",
            )
        if locked_attempt is not None and (
            locked_attempt.provider_call_sid
            and locked_attempt.provider_call_sid != provider_call_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Smallest.ai returned a conflicting attempt identity",
            )
        call.provider_call_sid = provider_call_id

    if not call:
        call = Call(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            direction=_smallest_direction(conversation_type),
            status="initiated",
            from_number=str(
                call_data.get("fromNumber") or metadata.get("fromPhone") or "provider-managed"
            ),
            to_number=str(
                call_data.get("toNumber") or metadata.get("toPhone") or "provider-managed"
            ),
            provider="smallest",
            provider_call_sid=provider_call_id,
            call_metadata={},
        )
        db.add(call)
        await db.flush()

    call_metadata = dict(call.call_metadata or {})
    call_metadata.setdefault("agent_configuration", agent_configuration_snapshot(agent))
    call_metadata.setdefault("conversation_type", conversation_type)
    call_metadata.setdefault("channel", _smallest_channel(conversation_type))
    deliveries = list(call_metadata.get("smallest_webhook_deliveries", []))
    delivery_id = payload.get("id")
    if delivery_id and delivery_id not in deliveries:
        deliveries.append(delivery_id)
        call_metadata["smallest_webhook_deliveries"] = deliveries[-50:]
    if variables or "smallest_variables" not in call_metadata:
        call_metadata["smallest_variables"] = variables
    event_rank = {
        "pre-conversation": 1,
        "post-conversation": 2,
        "analytics-completed": 3,
    }.get(event_type, 0)
    rank_value = call_metadata.get("smallest_lifecycle_rank")
    stored_event_rank = rank_value if isinstance(rank_value, int) else 0
    may_update_terminal_fields = event_rank >= stored_event_rank

    if event_type == "pre-conversation":
        if call.status not in TERMINAL_CALL_STATUSES:
            call.status = "ringing"
        call.started_at = call.started_at or datetime.now(UTC)

    elif event_type == "post-conversation":
        if may_update_terminal_fields:
            _merge_smallest_terminal_call_data(call, call_data, metadata, call_metadata)

        turns = metadata.get("transcript") or []
        if isinstance(turns, list):
            full_text = "\n".join(
                f"{str(turn.get('role', 'unknown')).title()}: {turn.get('content', '')}"
                for turn in turns
                if isinstance(turn, dict)
            )
            await _upsert_call_transcript(
                db,
                tenant_id=agent.tenant_id,
                call_id=call.id,
                turns=turns,
                full_text=full_text,
            )

    elif event_type == "analytics-completed":
        if may_update_terminal_fields:
            _merge_smallest_terminal_call_data(call, call_data, metadata, call_metadata)
        analytics = metadata.get("analytics") or {}
        if not isinstance(analytics, dict):
            analytics = {}
        call_metadata["smallest_analytics"] = analytics
        summary_text = str(analytics.get("summary") or "Call analytics completed.")
        await _upsert_call_summary(
            db,
            tenant_id=agent.tenant_id,
            call_id=call.id,
            summary_text=summary_text,
            analytics=analytics,
        )

        dispositions = analytics.get("dispositionMetrics") or []
        if isinstance(dispositions, list) and dispositions:
            first = dispositions[0]
            if isinstance(first, dict):
                call.disposition = (
                    str(first.get("value") or first.get("result") or first.get("name") or "")
                    or call.disposition
                )

    call_metadata["smallest_lifecycle_rank"] = max(stored_event_rank, event_rank)

    call.call_metadata = call_metadata
    lifecycle = await sync_campaign_call_lifecycle(db, call, provider_callback=True)
    process_revision = None
    if event_type in {"post-conversation", "analytics-completed"}:
        delivery_identity = payload.get("id")
        if not isinstance(delivery_identity, str) or not delivery_identity:
            canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            delivery_identity = hashlib.sha256(canonical_payload.encode()).hexdigest()
        process_revision = f"smallest:{event_type}:{delivery_identity}"
    should_process_terminal_event = event_type == "analytics-completed" or (
        event_type == "post-conversation" and stored_event_rank < 2
    )
    if not should_process_terminal_event:
        process_revision = None
    process_event_type = None
    if event_type == "post-conversation" and stored_event_rank < 2:
        process_event_type = "call.completed"
    elif event_type == "analytics-completed":
        process_event_type = "call.completed" if stored_event_rank < 2 else "call.analytics_updated"
    return ProviderWebhookEffects(
        lifecycle=lifecycle,
        source_call_id=str(call.id),
        source_tenant_id=str(call.tenant_id),
        source_campaign_id=str(call.campaign_id) if call.campaign_id else None,
        process_call_id=str(call.id) if should_process_terminal_event else None,
        process_tenant_id=(str(call.tenant_id) if should_process_terminal_event else None),
        process_revision=process_revision,
        process_event_type=process_event_type,
    )


@router.post("/smallest", status_code=204)
async def smallest_webhook(request: Request):
    """Receive signed Atoms pre-call, post-call, and analytics events."""
    raw_body = await _read_bounded_webhook_body(request)
    signature = request.headers.get("X-Signature", "")
    if not verify_smallest_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid Smallest.ai webhook signature")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        local_identity = f"smallest:{metadata.get('agentId')}:{metadata.get('callId')}"
    else:
        local_identity = f"smallest:invalid:{hashlib.sha256(raw_body).hexdigest()}"
    async with _local_provider_callback_lock(local_identity):
        async with async_session_factory() as db:
            effects = await _process_smallest_webhook(db, payload)
            outbox_ids = await _persist_webhook_effects(db, effects)
            await db.commit()
    _kick_provider_outbox(outbox_ids)
    return Response(status_code=204)


@router.post("/twilio/voice/inbound")
async def twilio_inbound_webhook(request: Request):
    """Route an inbound DID only through an explicit, unique runtime assignment."""
    params = await _parse_twilio_request(request)
    to_number = params.get("To", "")
    from_number = params.get("From", "")
    call_sid = params.get("CallSid", "")
    if not to_number or not from_number or not call_sid:
        _validate_twilio_request(request, params, settings.twilio_auth_token)
        raise HTTPException(status_code=400, detail="Missing Twilio inbound call identity")

    async with async_session_factory() as db:
        candidates = (
            await db.execute(
                select(AgentRuntimeProfile, Agent)
                .join(
                    Agent,
                    (Agent.id == AgentRuntimeProfile.agent_id)
                    & (Agent.tenant_id == AgentRuntimeProfile.tenant_id),
                )
                .where(
                    AgentRuntimeProfile.enabled.is_(True),
                    AgentRuntimeProfile.status == "active",
                    AgentRuntimeProfile.telephony_provider == "twilio",
                    Agent.is_active.is_(True),
                    Agent.voice_provider == "sarvam",
                )
            )
        ).all()
        matches = [
            (profile, agent)
            for profile, agent in candidates
            if to_number in (profile.assigned_numbers or [])
        ]
        account_sid = params.get("AccountSid", "")
        validated_matches: list[tuple[AgentRuntimeProfile, Agent]] = []
        configured_token_seen = False
        for profile, candidate_agent in matches:
            try:
                twilio_config = await _tenant_twilio_config(db, candidate_agent.tenant_id)
            except ProviderCredentialError:
                logger.warning(
                    "twilio_inbound_credential_unavailable",
                    tenant_id=str(candidate_agent.tenant_id),
                )
                twilio_config = {}
            candidate_account_sid = str(
                twilio_config.get("account_sid") or settings.twilio_account_sid
            )
            candidate_auth_token = str(
                twilio_config.get("auth_token") or settings.twilio_auth_token
            )
            configured_token_seen = configured_token_seen or bool(candidate_auth_token)
            if account_sid and candidate_account_sid and account_sid != candidate_account_sid:
                continue
            if _twilio_request_is_valid(request, params, candidate_auth_token):
                validated_matches.append((profile, candidate_agent))

        if not validated_matches:
            if configured_token_seen:
                raise HTTPException(status_code=401, detail="Invalid Twilio webhook signature")
            # No tenant route exposed a credential. A platform credential may
            # still authenticate the request so Twilio receives safe fallback
            # TwiML instead of an unsigned routing response.
            _validate_twilio_request(request, params, settings.twilio_auth_token)

        if len(validated_matches) != 1:
            logger.warning(
                "twilio_inbound_number_unroutable",
                assigned_matches=len(matches),
                matched_agent_ids=[str(agent.id) for _profile, agent in matches],
                validated_matches=len(validated_matches),
                to_number=to_number,
            )
            return Response(
                content=(
                    '<?xml version="1.0"?><Response><Say>Sorry, this number is not '
                    "configured. Goodbye.</Say><Hangup/></Response>"
                ),
                media_type="application/xml",
            )

        _profile, agent = validated_matches[0]
        call = await db.scalar(select(Call).where(Call.provider_call_sid == call_sid))
        if call is None:
            now = datetime.now(UTC)
            call = Call(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                direction="inbound",
                status="in_progress",
                from_number=from_number,
                to_number=to_number,
                provider="twilio",
                provider_call_sid=call_sid,
                started_at=now,
                answered_at=now,
                call_metadata={
                    "agent_configuration": agent_configuration_snapshot(agent),
                    "conversation_type": "telephonyInbound",
                    "channel": "phone",
                    "speech_provider": "sarvam",
                },
            )
            db.add(call)
            await db.flush()
        elif call.tenant_id != agent.tenant_id or call.agent_id != agent.id:
            raise HTTPException(status_code=409, detail="Conflicting inbound call identity")
        await db.commit()

    twiml = get_telephony_provider().generate_connect_stream(
        _runtime_stream_url(call.id),
        _runtime_stream_parameters(call.id),
    )
    return Response(content=twiml.xml, media_type="application/xml")


@router.post("/twilio/voice/{call_id}")
async def twilio_voice_webhook(call_id: UUID, request: Request):
    """Handle Twilio voice webhook - called when outbound call connects."""
    untrusted_params = await _parse_twilio_request(request)
    if not untrusted_params.get("AccountSid"):
        _validate_twilio_request(
            request,
            untrusted_params,
            settings.twilio_auth_token,
        )
    async with async_session_factory() as db:
        result = await db.execute(select(Call).where(Call.id == call_id))
        call_probe = result.scalar_one_or_none()
        if not call_probe:
            await _verify_twilio_request(request)
            return Response(
                content='<?xml version="1.0"?><Response><Hangup/></Response>',
                media_type="application/xml",
            )
        twilio_config = await _tenant_twilio_config(db, call_probe.tenant_id)
        await _verify_twilio_request(
            request,
            auth_token=str(twilio_config.get("auth_token") or settings.twilio_auth_token),
        )
        call, _attempt = await _lock_callback_call_graph(db, call_probe)

        if call.status not in TERMINAL_CALL_STATUSES:
            call.status = "in_progress"
            call.answered_at = call.answered_at or datetime.now(UTC)

        # Get the agent
        agent_result = await db.execute(
            select(Agent).where(
                Agent.id == call.agent_id,
                Agent.tenant_id == call.tenant_id,
            )
        )
        agent = agent_result.scalar_one_or_none()

        provider = get_telephony_provider()
        runtime_profile = None
        if agent is not None:
            runtime_profile = await db.scalar(
                select(AgentRuntimeProfile).where(
                    AgentRuntimeProfile.agent_id == agent.id,
                    AgentRuntimeProfile.tenant_id == agent.tenant_id,
                )
            )

        if (
            agent
            and agent.voice_provider == "sarvam"
            and runtime_profile
            and runtime_profile.enabled
            and runtime_profile.status == "active"
            and runtime_profile.telephony_provider == "twilio"
        ):
            twiml = provider.generate_connect_stream(
                _runtime_stream_url(call.id),
                _runtime_stream_parameters(call.id),
            )
            metadata = dict(call.call_metadata or {})
            metadata["runtime_route"] = {
                "telephony_provider": "twilio",
                "speech_provider": "sarvam",
            }
            call.call_metadata = metadata
        else:
            twiml = provider.generate_greeting(
                "This voice agent is not active. Please contact the service administrator.",
                "en-US-Standard-A",
            )

        lifecycle = await sync_campaign_call_lifecycle(db, call, provider_callback=True)
        voice_effects = ProviderWebhookEffects(
            lifecycle=lifecycle,
            source_call_id=str(call.id),
            source_tenant_id=str(call.tenant_id),
            source_campaign_id=str(call.campaign_id) if call.campaign_id else None,
        )
        outbox_ids = await _persist_webhook_effects(db, voice_effects)
        await db.commit()

    _kick_provider_outbox(outbox_ids)
    return Response(content=twiml.xml, media_type="application/xml")


@router.post("/twilio/status/{call_id}")
async def twilio_status_callback(
    call_id: UUID,
    request: Request,
):
    """Handle Twilio status callbacks for call state changes."""
    untrusted_params = await _parse_twilio_request(request)
    if not untrusted_params.get("AccountSid"):
        _validate_twilio_request(
            request,
            untrusted_params,
            settings.twilio_auth_token,
        )
    async with async_session_factory() as db:
        result = await db.execute(select(Call).where(Call.id == call_id))
        call_probe = result.scalar_one_or_none()
        if not call_probe:
            await _verify_twilio_request(request)
            return {"status": "not_found"}
        twilio_config = await _tenant_twilio_config(db, call_probe.tenant_id)
        params = await _verify_twilio_request(
            request,
            auth_token=str(twilio_config.get("auth_token") or settings.twilio_auth_token),
        )
        call_sid = params.get("CallSid", "")
        call_status = params.get("CallStatus", "")
        call_duration = params.get("CallDuration", "0")
        recording_url = params.get("RecordingUrl", "")
        call, _attempt = await _lock_callback_call_graph(db, call_probe)

        status_map = {
            "completed": "completed",
            "busy": "busy",
            "no-answer": "no_answer",
            "canceled": "failed",
            "failed": "failed",
            "in-progress": "in_progress",
        }
        incoming_status = status_map.get(call_status, call_status)
        if call.status not in TERMINAL_CALL_STATUSES:
            call.status = incoming_status
        if call.provider_call_sid and call_sid and call.provider_call_sid != call_sid:
            raise HTTPException(status_code=409, detail="Conflicting Twilio call identity")
        call.provider_call_sid = call_sid or call.provider_call_sid

        if call_duration:
            try:
                call.duration_seconds = max(int(call_duration), 0)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Twilio call duration") from exc

        if recording_url:
            call.provider_recording_url = recording_url

        is_terminal = call_status in (
            "completed",
            "busy",
            "no-answer",
            "canceled",
            "failed",
        )
        if is_terminal:
            call.ended_at = datetime.now(UTC)
        lifecycle = await sync_campaign_call_lifecycle(db, call, provider_callback=True)
        status_effects = ProviderWebhookEffects(
            lifecycle=lifecycle,
            source_call_id=str(call.id),
            source_tenant_id=str(call.tenant_id),
            source_campaign_id=str(call.campaign_id) if call.campaign_id else None,
            process_call_id=str(call.id) if is_terminal else None,
            process_tenant_id=str(call.tenant_id) if is_terminal else None,
            process_revision=(
                f"twilio:{call_sid}:{call_status}:{call_duration}:{recording_url}"
                if is_terminal
                else None
            ),
            process_event_type="call.completed" if is_terminal else None,
        )
        outbox_ids = await _persist_webhook_effects(db, status_effects)
        await db.commit()
    _kick_provider_outbox(outbox_ids)
    return {"status": "ok"}
