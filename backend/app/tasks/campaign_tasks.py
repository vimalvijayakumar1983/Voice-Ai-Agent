"""Campaign execution tasks."""

import asyncio
import uuid

import structlog
from celery import shared_task
from sqlalchemy import select

from app.core.config import settings

logger = structlog.get_logger()


def _run_async(coro):
    """Helper to run async code in sync Celery tasks."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(name="app.tasks.campaign_tasks.run_campaign", bind=True, max_retries=3)
def run_campaign(self, campaign_id: str, tenant_id: str):
    """Execute an outbound calling campaign."""
    _run_async(_run_campaign_async(campaign_id, tenant_id))


async def _run_campaign_async(campaign_id: str, tenant_id: str):
    from app.core.database import async_session_factory
    from app.models.call import Call
    from app.models.campaign import Campaign, CampaignContact
    from app.models.compliance import DncEntry
    from app.telephony.base import CallRequest
    from app.telephony.twilio_provider import get_telephony_provider

    campaign_uuid = uuid.UUID(campaign_id)
    tenant_uuid = uuid.UUID(tenant_id)

    async with async_session_factory() as db:
        # Get campaign
        result = await db.execute(select(Campaign).where(Campaign.id == campaign_uuid))
        campaign = result.scalar_one_or_none()
        if not campaign or campaign.status != "running":
            logger.info("campaign_skip", campaign_id=campaign_id, reason="not running")
            return

        # Get pending contacts
        contacts_result = await db.execute(
            select(CampaignContact)
            .where(
                CampaignContact.campaign_id == campaign_uuid,
                CampaignContact.status == "pending",
            )
            .limit(campaign.max_concurrent_calls)
        )
        contacts = contacts_result.scalars().all()

        if not contacts:
            campaign.status = "completed"
            await db.commit()
            logger.info("campaign_completed", campaign_id=campaign_id)
            return

        provider = get_telephony_provider()

        for contact in contacts:
            # Check DNC
            dnc_check = await db.execute(
                select(DncEntry).where(
                    DncEntry.tenant_id == tenant_uuid,
                    DncEntry.phone_number == contact.phone_number,
                )
            )
            if dnc_check.scalar_one_or_none():
                contact.status = "dnc"
                campaign.completed_contacts += 1
                continue

            # Create call record
            call = Call(
                tenant_id=tenant_uuid,
                agent_id=campaign.agent_id,
                campaign_id=campaign_uuid,
                direction="outbound",
                status="initiated",
                from_number=settings.twilio_default_from_number,
                to_number=contact.phone_number,
                call_metadata=contact.context_data,
            )
            db.add(call)
            await db.flush()

            # Initiate call
            try:
                webhook_url = f"{settings.base_url}/api/v1/webhooks/twilio/voice/{call.id}"
                status_url = f"{settings.base_url}/api/v1/webhooks/twilio/status/{call.id}"

                call_result = await provider.make_call(
                    CallRequest(
                        to_number=contact.phone_number,
                        from_number=settings.twilio_default_from_number,
                        webhook_url=webhook_url,
                        status_callback_url=status_url,
                    )
                )
                call.provider_call_sid = call_result.provider_call_sid
                call.status = "ringing"
                contact.status = "calling"
                contact.attempts += 1
                contact.last_call_id = call.id
            except Exception as e:
                call.status = "failed"
                contact.status = "failed"
                logger.error("campaign_call_failed", error=str(e), contact_id=str(contact.id))

        await db.commit()

    logger.info(
        "campaign_batch_processed",
        campaign_id=campaign_id,
        contacts_processed=len(contacts),
    )
