"""Verified webhook handlers for telephony and voice-provider events."""

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import Agent
from app.models.call import Call, CallSummary, CallTranscript
from app.telephony.twilio_provider import get_telephony_provider

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def verify_smallest_signature(raw_body: bytes, signature: str) -> bool:
    """Validate Smallest.ai's hex-encoded HMAC-SHA256 signature."""
    if not settings.smallest_webhook_secret or not signature:
        return False
    expected = hmac.new(
        settings.smallest_webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _smallest_direction(conversation_type: str) -> str:
    return "inbound" if "Inbound" in conversation_type else "outbound"


async def _process_smallest_webhook(db: AsyncSession, payload: dict) -> None:
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

    agent_result = await db.execute(
        select(Agent).where(Agent.provider_agent_id == provider_agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Smallest.ai agent mapping not found")

    call_result = await db.execute(select(Call).where(Call.provider_call_sid == provider_call_id))
    call = call_result.scalar_one_or_none()
    call_data = metadata.get("callData") or {}
    if not isinstance(call_data, dict):
        call_data = {}
    conversation_type = str(metadata.get("conversationType") or "telephonyOutbound")

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
    deliveries = list(call_metadata.get("smallest_webhook_deliveries", []))
    delivery_id = payload.get("id")
    if delivery_id and delivery_id in deliveries:
        return
    if delivery_id:
        deliveries.append(delivery_id)
        call_metadata["smallest_webhook_deliveries"] = deliveries[-50:]
    call_metadata["smallest_variables"] = metadata.get("variables", {})

    if event_type == "pre-conversation":
        call.status = "ringing"
        call.started_at = call.started_at or datetime.now(UTC)

    elif event_type == "post-conversation":
        status_map = {
            "no-answer": "no_answer",
            "canceled": "failed",
        }
        remote_status = str(call_data.get("callStatus") or "completed")
        call.status = status_map.get(remote_status, remote_status)
        call.from_number = str(call_data.get("fromNumber") or call.from_number)
        call.to_number = str(call_data.get("toNumber") or call.to_number)
        call.answered_at = _parse_timestamp(call_data.get("answerTime")) or call.answered_at
        call.ended_at = _parse_timestamp(call_data.get("endTime")) or datetime.now(UTC)
        duration = call_data.get("callDuration")
        if isinstance(duration, (int, float)):
            call.duration_seconds = round(duration)
        call.provider_recording_url = metadata.get("recordingUrl") or call.provider_recording_url

        turns = metadata.get("transcript") or []
        if isinstance(turns, list):
            transcript_result = await db.execute(
                select(CallTranscript).where(CallTranscript.call_id == call.id)
            )
            transcript = transcript_result.scalar_one_or_none()
            full_text = "\n".join(
                f"{str(turn.get('role', 'unknown')).title()}: {turn.get('content', '')}"
                for turn in turns
                if isinstance(turn, dict)
            )
            if transcript:
                transcript.turns = turns
                transcript.full_text = full_text
            else:
                db.add(
                    CallTranscript(
                        tenant_id=agent.tenant_id,
                        call_id=call.id,
                        turns=turns,
                        full_text=full_text,
                    )
                )

    elif event_type == "analytics-completed":
        analytics = metadata.get("analytics") or {}
        if not isinstance(analytics, dict):
            analytics = {}
        call_metadata["smallest_analytics"] = analytics
        summary_text = str(analytics.get("summary") or "Call analytics completed.")
        summary_result = await db.execute(select(CallSummary).where(CallSummary.call_id == call.id))
        summary = summary_result.scalar_one_or_none()
        if summary:
            summary.summary = summary_text
            summary.key_topics = analytics.get("keyTopics") or []
            summary.action_items = analytics.get("actionItems") or []
            summary.sentiment = analytics.get("sentiment")
        else:
            db.add(
                CallSummary(
                    tenant_id=agent.tenant_id,
                    call_id=call.id,
                    summary=summary_text,
                    key_topics=analytics.get("keyTopics") or [],
                    action_items=analytics.get("actionItems") or [],
                    sentiment=analytics.get("sentiment"),
                )
            )

        dispositions = analytics.get("dispositionMetrics") or []
        if isinstance(dispositions, list) and dispositions:
            first = dispositions[0]
            if isinstance(first, dict):
                call.disposition = (
                    str(first.get("value") or first.get("result") or first.get("name") or "")
                    or call.disposition
                )

    call.call_metadata = call_metadata


@router.post("/smallest", status_code=204)
async def smallest_webhook(request: Request):
    """Receive signed Atoms pre-call, post-call, and analytics events."""
    raw_body = await request.body()
    signature = request.headers.get("X-Signature", "")
    if not verify_smallest_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid Smallest.ai webhook signature")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    async with async_session_factory() as db:
        await _process_smallest_webhook(db, payload)
        await db.commit()
    return Response(status_code=204)


@router.post("/twilio/voice/{call_id}")
async def twilio_voice_webhook(call_id: UUID, request: Request):
    """Handle Twilio voice webhook - called when outbound call connects."""
    async with async_session_factory() as db:
        result = await db.execute(select(Call).where(Call.id == call_id))
        call = result.scalar_one_or_none()
        if not call:
            return Response(
                content='<?xml version="1.0"?><Response><Hangup/></Response>',
                media_type="application/xml",
            )

        call.status = "in_progress"
        call.answered_at = datetime.now(UTC)

        # Get the agent
        agent_result = await db.execute(select(Agent).where(Agent.id == call.agent_id))
        agent = agent_result.scalar_one_or_none()

        provider = get_telephony_provider()

        if agent and agent.greeting_message:
            twiml = provider.generate_greeting(agent.greeting_message, agent.voice_id)
        else:
            twiml = provider.generate_greeting(
                "Hello, how can I help you today?", "en-US-Standard-A"
            )

        # For MVP, connect to a media stream for real-time AI conversation
        # In production, this would connect to a WebSocket-based AI conversation handler
        # For now, just play the greeting

        await db.commit()

    return Response(content=twiml.xml, media_type="application/xml")


@router.post("/twilio/status/{call_id}")
async def twilio_status_callback(
    call_id: UUID,
    call_sid: str = Form("", alias="CallSid"),
    call_status: str = Form("", alias="CallStatus"),
    call_duration: str = Form("0", alias="CallDuration"),
    recording_url: str = Form("", alias="RecordingUrl"),
):
    """Handle Twilio status callbacks for call state changes."""
    async with async_session_factory() as db:
        result = await db.execute(select(Call).where(Call.id == call_id))
        call = result.scalar_one_or_none()
        if not call:
            return {"status": "not_found"}

        status_map = {
            "completed": "completed",
            "busy": "busy",
            "no-answer": "no_answer",
            "canceled": "failed",
            "failed": "failed",
        }
        call.status = status_map.get(call_status, call_status)
        call.provider_call_sid = call_sid or call.provider_call_sid

        if call_duration:
            call.duration_seconds = int(call_duration)

        if recording_url:
            call.provider_recording_url = recording_url

        if call_status in ("completed", "busy", "no-answer", "canceled", "failed"):
            call.ended_at = datetime.now(UTC)

            # Trigger post-call processing (summary, webhooks)
            if call.duration_seconds and call.duration_seconds > 0:
                from app.tasks.call_tasks import process_completed_call

                process_completed_call.delay(str(call.id), str(call.tenant_id))

        await db.commit()

    return {"status": "ok"}


@router.post("/twilio/voice/inbound")
async def twilio_inbound_webhook(
    request: Request,
    from_number: str = Form("", alias="From"),
    to_number: str = Form("", alias="To"),
    call_sid: str = Form("", alias="CallSid"),
):
    """Handle inbound calls - route to appropriate agent/workflow."""
    # For MVP, look up agent by the called number
    async with async_session_factory() as db:
        # Find agent configured for this number (simple lookup)
        result = await db.execute(
            select(Agent)
            .where(Agent.transfer_number == to_number, Agent.is_active.is_(True))
            .limit(1)
        )
        agent = result.scalar_one_or_none()

        # Create call record
        call = Call(
            tenant_id=agent.tenant_id if agent else None,
            agent_id=agent.id if agent else None,
            direction="inbound",
            status="in_progress",
            from_number=from_number,
            to_number=to_number,
            provider_call_sid=call_sid,
            started_at=datetime.now(UTC),
            answered_at=datetime.now(UTC),
        )

        if agent:
            db.add(call)
            await db.commit()

            provider = get_telephony_provider()
            greeting = agent.greeting_message or "Hello, how can I help you?"
            twiml = provider.generate_greeting(greeting, agent.voice_id)
            return Response(content=twiml.xml, media_type="application/xml")

    # No agent found - default response
    return Response(
        content=(
            '<?xml version="1.0"?><Response><Say>Sorry, this number is not configured. '
            "Goodbye.</Say><Hangup/></Response>"
        ),
        media_type="application/xml",
    )
