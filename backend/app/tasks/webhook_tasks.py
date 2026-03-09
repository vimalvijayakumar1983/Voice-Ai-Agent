"""Webhook delivery tasks."""

import asyncio
import uuid

import structlog
from celery import shared_task

logger = structlog.get_logger()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(
    name="app.tasks.webhook_tasks.fire_webhook_event",
    bind=True,
    max_retries=5,
    default_retry_delay=30,
)
def fire_webhook_event(self, tenant_id: str, event_type: str, payload: dict):
    """Deliver a webhook event to all matching integrations."""
    _run_async(_fire_webhook_async(tenant_id, event_type, payload))


async def _fire_webhook_async(tenant_id: str, event_type: str, payload: dict):
    from datetime import datetime, timezone

    import httpx
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.integration import Integration, WebhookEvent

    tenant_uuid = uuid.UUID(tenant_id)

    async with async_session_factory() as db:
        # Find webhook integrations for this tenant/event
        result = await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_uuid,
                Integration.integration_type == "webhook",
                Integration.is_active.is_(True),
            )
        )
        integrations = result.scalars().all()

        for integration in integrations:
            config = integration.config or {}
            subscribed_events = config.get("events", [])
            if event_type not in subscribed_events and "*" not in subscribed_events:
                continue

            webhook_url = config.get("url")
            if not webhook_url:
                continue

            # Create event record
            event = WebhookEvent(
                tenant_id=tenant_uuid,
                integration_id=integration.id,
                event_type=event_type,
                payload=payload,
                status="pending",
            )
            db.add(event)
            await db.flush()

            # Deliver
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        webhook_url,
                        json={
                            "event": event_type,
                            "data": payload,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    response.raise_for_status()

                event.status = "sent"
                event.delivered_at = datetime.now(timezone.utc)
                event.attempts += 1

            except Exception as e:
                event.status = "failed"
                event.last_error = str(e)
                event.attempts += 1
                logger.error(
                    "webhook_delivery_failed",
                    integration_id=str(integration.id),
                    error=str(e),
                )

        await db.commit()
