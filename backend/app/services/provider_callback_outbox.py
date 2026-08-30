"""Transactional outbox for best-effort provider callback follow-up work."""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import ProviderCallbackOutbox


async def persist_provider_callback_actions(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID | None,
    process_completed_call: bool,
    process_revision: str | None,
    process_event_type: str | None,
    continue_campaign: bool,
) -> tuple[uuid.UUID, ...]:
    """Insert stable action identities in the callback transaction."""
    actions: list[tuple[str, str]] = []
    if process_completed_call:
        revision_digest = hashlib.sha256((process_revision or "terminal").encode()).hexdigest()
        process_action = (
            "process_analytics_update"
            if process_event_type == "call.analytics_updated"
            else "process_completed_call"
        )
        actions.append(
            (
                process_action,
                f"provider:process-call:{call_id}:{revision_digest}",
            )
        )
    if continue_campaign and campaign_id is not None:
        actions.append(("continue_campaign", f"provider:continue-campaign:{call_id}"))

    outbox_ids: list[uuid.UUID] = []
    for action, event_key in actions:
        existing_id = await db.scalar(
            select(ProviderCallbackOutbox.id).where(ProviderCallbackOutbox.event_key == event_key)
        )
        if existing_id is not None:
            outbox_ids.append(existing_id)
            continue

        outbox_id = uuid.uuid4()
        record = ProviderCallbackOutbox(
            id=outbox_id,
            tenant_id=tenant_id,
            call_id=call_id,
            campaign_id=campaign_id,
            event_key=event_key,
            action=action,
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC),
        )
        try:
            # Concurrent duplicate callbacks can race this insert. A savepoint
            # contains the unique violation without rolling back call/contact
            # lifecycle changes in the outer callback transaction.
            async with db.begin_nested():
                db.add(record)
                await db.flush()
        except IntegrityError:
            existing_id = await db.scalar(
                select(ProviderCallbackOutbox.id).where(
                    ProviderCallbackOutbox.event_key == event_key
                )
            )
            if existing_id is None:
                raise
            outbox_ids.append(existing_id)
        else:
            outbox_ids.append(outbox_id)
    return tuple(outbox_ids)
