"""Post-call processing tasks."""

import asyncio
import uuid
from datetime import UTC

import structlog
from celery import shared_task

logger = structlog.get_logger()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(name="app.tasks.call_tasks.process_completed_call", bind=True, max_retries=3)
def process_completed_call(self, call_id: str, tenant_id: str):
    """Process a completed call - generate summary, fire webhooks, record usage."""
    _run_async(_process_completed_call_async(call_id, tenant_id))


async def _process_completed_call_async(call_id: str, tenant_id: str):
    from datetime import datetime

    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.billing import UsageRecord
    from app.models.call import Call, CallSummary, CallTranscript

    call_uuid = uuid.UUID(call_id)
    tenant_uuid = uuid.UUID(tenant_id)

    async with async_session_factory() as db:
        result = await db.execute(select(Call).where(Call.id == call_uuid))
        call = result.scalar_one_or_none()
        if not call:
            return

        # Generate summary if transcript exists
        transcript_result = await db.execute(
            select(CallTranscript).where(CallTranscript.call_id == call_uuid)
        )
        transcript = transcript_result.scalar_one_or_none()

        if transcript and transcript.full_text:
            try:
                from app.ai.conversation import conversation_engine

                summary_data = await conversation_engine.generate_call_summary(transcript.full_text)

                summary = CallSummary(
                    tenant_id=tenant_uuid,
                    call_id=call_uuid,
                    summary=summary_data.get("summary", ""),
                    key_topics=summary_data.get("key_topics", []),
                    action_items=summary_data.get("action_items", []),
                    sentiment=summary_data.get("sentiment"),
                )
                db.add(summary)

                # Update call disposition from AI analysis
                if summary_data.get("disposition"):
                    call.disposition = summary_data["disposition"]

            except Exception as e:
                logger.error("summary_generation_failed", call_id=call_id, error=str(e))

        # Record usage
        if call.duration_seconds and call.duration_seconds > 0:
            now = datetime.now(UTC)
            usage = UsageRecord(
                tenant_id=tenant_uuid,
                call_id=call_uuid,
                usage_type="call_minutes",
                quantity=call.duration_seconds / 60.0,
                unit="minutes",
                cost_cents=int((call.duration_seconds / 60.0) * 5),  # $0.05/min default
                period_start=now,
                period_end=now,
            )
            db.add(usage)

        # Fire webhooks
        from app.tasks.webhook_tasks import fire_webhook_event

        fire_webhook_event.delay(
            tenant_id,
            "call.completed",
            {
                "call_id": call_id,
                "status": call.status,
                "duration_seconds": call.duration_seconds,
                "disposition": call.disposition,
                "direction": call.direction,
            },
        )

        await db.commit()

    logger.info("call_processed", call_id=call_id)
