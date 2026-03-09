"""Twilio webhook handlers for call lifecycle events."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.agent import Agent
from app.models.call import Call
from app.telephony.twilio_provider import get_telephony_provider

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


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
        call.answered_at = datetime.now(timezone.utc)

        # Get the agent
        agent_result = await db.execute(select(Agent).where(Agent.id == call.agent_id))
        agent = agent_result.scalar_one_or_none()

        provider = get_telephony_provider()

        if agent and agent.greeting_message:
            twiml = provider.generate_greeting(agent.greeting_message, agent.voice_id)
        else:
            twiml = provider.generate_greeting("Hello, how can I help you today?", "en-US-Standard-A")

        # For MVP, connect to a media stream for real-time AI conversation
        # In production, this would connect to a WebSocket-based AI conversation handler
        # For now, just play the greeting

        await db.commit()

    return Response(content=twiml.xml, media_type="application/xml")


@router.post("/twilio/status/{call_id}")
async def twilio_status_callback(
    call_id: UUID,
    CallSid: str = Form(""),
    CallStatus: str = Form(""),
    CallDuration: str = Form("0"),
    RecordingUrl: str = Form(""),
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
        call.status = status_map.get(CallStatus, CallStatus)
        call.provider_call_sid = CallSid or call.provider_call_sid

        if CallDuration:
            call.duration_seconds = int(CallDuration)

        if RecordingUrl:
            call.provider_recording_url = RecordingUrl

        if CallStatus in ("completed", "busy", "no-answer", "canceled", "failed"):
            call.ended_at = datetime.now(timezone.utc)

            # Trigger post-call processing (summary, webhooks)
            if call.duration_seconds and call.duration_seconds > 0:
                from app.tasks.call_tasks import process_completed_call
                process_completed_call.delay(str(call.id), str(call.tenant_id))

        await db.commit()

    return {"status": "ok"}


@router.post("/twilio/voice/inbound")
async def twilio_inbound_webhook(
    request: Request,
    From: str = Form(""),
    To: str = Form(""),
    CallSid: str = Form(""),
):
    """Handle inbound calls - route to appropriate agent/workflow."""
    # For MVP, look up agent by the called number
    async with async_session_factory() as db:
        # Find agent configured for this number (simple lookup)
        result = await db.execute(
            select(Agent).where(Agent.transfer_number == To, Agent.is_active.is_(True)).limit(1)
        )
        agent = result.scalar_one_or_none()

        # Create call record
        call = Call(
            tenant_id=agent.tenant_id if agent else None,
            agent_id=agent.id if agent else None,
            direction="inbound",
            status="in_progress",
            from_number=From,
            to_number=To,
            provider_call_sid=CallSid,
            started_at=datetime.now(timezone.utc),
            answered_at=datetime.now(timezone.utc),
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
        content='<?xml version="1.0"?><Response><Say>Sorry, this number is not configured. Goodbye.</Say><Hangup/></Response>',
        media_type="application/xml",
    )
