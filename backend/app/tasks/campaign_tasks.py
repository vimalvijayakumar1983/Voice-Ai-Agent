"""Crash-safe, tenant-scoped campaign execution tasks."""

import asyncio
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import Agent
from app.models.call import Call
from app.models.campaign import (
    Campaign,
    CampaignContact,
    CampaignContactAttempt,
    ProviderCallbackOutbox,
)
from app.models.workflow import Workflow
from app.providers.smallest import get_smallest_client
from app.services.call_metadata import agent_configuration_snapshot
from app.services.campaign_lifecycle import (
    ACTIVE_CONTACT_STATUSES,
    TERMINAL_CALL_STATUSES,
    CampaignLifecycleResult,
    refresh_campaign_metrics,
    sync_campaign_call_lifecycle,
)
from app.services.compliance_policy import is_outbound_consent_revoked
from app.services.phone_numbers import (
    is_number_on_tenant_dnc,
    normalize_e164,
    tenant_phone_dnc_lock,
)
from app.tasks.worker import celery_app
from app.telephony.base import CallRequest
from app.telephony.twilio_provider import get_telephony_provider

logger = structlog.get_logger()

MAX_CAMPAIGN_BATCH_SIZE = 100
NEXT_BATCH_DELAY_SECONDS = 1
DISPATCH_UNCERTAINTY_SECONDS = 300
ACCEPTED_CALL_GRACE_SECONDS = 120
_IDEMPOTENCY_NAMESPACE = uuid.UUID("12945b4d-b902-4214-878e-e8d19d44418a")


@dataclass(frozen=True)
class BatchPlan:
    attempt_ids: tuple[uuid.UUID, ...] = ()
    defer_until: datetime | None = None


@dataclass(frozen=True)
class DispatchPreparation:
    payload: "DispatchPayload | None" = None
    defer_until: datetime | None = None


@dataclass(frozen=True)
class DispatchPayload:
    attempt_id: uuid.UUID
    tenant_id: uuid.UUID
    campaign_id: uuid.UUID
    contact_id: uuid.UUID
    call_id: uuid.UUID
    idempotency_key: str
    provider: str
    provider_agent_id: str | None
    provider_revision_id: str | None
    phone_number: str
    from_number: str | None
    context_data: dict


@dataclass(frozen=True)
class FollowUp:
    eta: datetime | None = None
    countdown: int | None = None


class DefinitiveDispatchError(RuntimeError):
    """A local/provider rejection that proves no paid call was accepted."""


def _run_async(coro):
    """Run an async campaign transaction from a synchronous Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.campaign_tasks.run_campaign", bind=True, max_retries=3)
def run_campaign(self, campaign_id: str, tenant_id: str):
    """Execute one bounded, idempotent outbound campaign batch."""
    try:
        return _run_async(_run_campaign_async(campaign_id, tenant_id))
    except Exception as exc:
        logger.exception("campaign_task_failed", campaign_id=campaign_id, tenant_id=tenant_id)
        raise self.retry(exc=exc, countdown=min(2 ** (self.request.retries + 1), 60)) from exc


@celery_app.task(name="app.tasks.campaign_tasks.sweep_running_campaigns")
def sweep_running_campaigns():
    """Recover running work and terminal-watchdog candidates after queue loss."""
    campaigns = _run_async(_list_running_campaigns())
    for campaign_id, tenant_id in campaigns:
        run_campaign.delay(campaign_id, tenant_id)
    return len(campaigns)


async def _list_running_campaigns(limit: int = 500) -> list[tuple[str, str]]:
    from app.core.database import async_session_factory

    # A campaign's dispatch status must not disable terminal-call safety. Keep
    # every campaign with a provider-accepted, nonterminal call in the periodic
    # recovery set even after an operator pauses/cancels it or its aggregate was
    # otherwise marked completed. `_prepare_campaign_batch` still gates every
    # dispatch path on `running`; these extra rows can only run the watchdog.
    accepted_nonterminal_call = (
        select(CampaignContactAttempt.id)
        .join(
            Call,
            (Call.id == CampaignContactAttempt.call_id)
            & (Call.tenant_id == CampaignContactAttempt.tenant_id)
            & (Call.campaign_id == CampaignContactAttempt.campaign_id),
        )
        .where(
            CampaignContactAttempt.tenant_id == Campaign.tenant_id,
            CampaignContactAttempt.campaign_id == Campaign.id,
            CampaignContactAttempt.state == "accepted",
            Call.status.not_in(TERMINAL_CALL_STATUSES),
        )
        .exists()
    )
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Campaign.id, Campaign.tenant_id)
                .where(or_(Campaign.status == "running", accepted_nonterminal_call))
                # Safety candidates take precedence over ordinary running-work
                # recovery when the bounded sweep is full.
                .order_by(accepted_nonterminal_call.desc(), Campaign.updated_at.asc())
                .limit(limit)
            )
        ).all()
    return [(str(campaign_id), str(tenant_id)) for campaign_id, tenant_id in rows]


@celery_app.task(name="app.tasks.campaign_tasks.dispatch_provider_callback_outbox")
def dispatch_provider_callback_outbox(outbox_id: str):
    """Dispatch one persisted callback action; failures remain retryable in DB."""
    try:
        return _run_async(_dispatch_provider_callback_outbox_async(outbox_id))
    except (TypeError, ValueError):
        return "invalid_identity"


@celery_app.task(name="app.tasks.campaign_tasks.sweep_provider_callback_outbox")
def sweep_provider_callback_outbox():
    """Drain callback actions after broker/API crash recovery."""
    return _run_async(_sweep_provider_callback_outbox_async())


async def _dispatch_provider_callback_outbox_async(outbox_id: str) -> str:
    from app.core.database import async_session_factory

    outbox_uuid = uuid.UUID(outbox_id)
    async with async_session_factory() as db:
        record = (
            await db.execute(
                select(ProviderCallbackOutbox)
                .where(ProviderCallbackOutbox.id == outbox_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            return "missing"
        if record.status == "dispatched":
            return "dispatched"
        available_at = _as_utc(record.available_at) or datetime.now(UTC)
        if available_at > datetime.now(UTC):
            return "deferred"

        record.attempts += 1
        try:
            if record.action == "process_completed_call":
                from app.tasks.call_tasks import process_completed_call

                process_completed_call.delay(str(record.call_id), str(record.tenant_id))
            elif record.action == "process_analytics_update":
                from app.tasks.call_tasks import process_completed_call

                process_completed_call.delay(
                    str(record.call_id),
                    str(record.tenant_id),
                    record.event_key,
                    "call.analytics_updated",
                )
            elif record.action == "continue_campaign" and record.campaign_id is not None:
                run_campaign.delay(str(record.campaign_id), str(record.tenant_id))
            else:
                raise ValueError("Unsupported provider callback outbox action")
        except Exception as exc:
            record.status = "pending"
            record.last_error = type(exc).__name__[:200]
            record.available_at = datetime.now(UTC) + timedelta(
                seconds=min(30 * (2 ** min(record.attempts - 1, 5)), 15 * 60)
            )
            await db.commit()
            return "pending"

        record.status = "dispatched"
        record.dispatched_at = datetime.now(UTC)
        record.last_error = None
        await db.commit()
        return "dispatched"


async def _sweep_provider_callback_outbox_async(limit: int = 500) -> int:
    from app.core.database import async_session_factory

    now = datetime.now(UTC)
    async with async_session_factory() as db:
        outbox_ids = (
            (
                await db.execute(
                    select(ProviderCallbackOutbox.id)
                    .where(
                        ProviderCallbackOutbox.status == "pending",
                        ProviderCallbackOutbox.available_at <= now,
                    )
                    .order_by(ProviderCallbackOutbox.available_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    dispatched = 0
    for outbox_id in outbox_ids:
        outcome = await _dispatch_provider_callback_outbox_async(str(outbox_id))
        if outcome == "dispatched":
            dispatched += 1
    return dispatched


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_calling_hour(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _next_dispatch_time(campaign, now: datetime | None = None) -> datetime | None:
    """Return the next UTC dispatch time, or ``None`` after the schedule ends."""
    now_utc = _as_utc(now) or datetime.now(UTC)
    scheduled_start = _as_utc(campaign.scheduled_start)
    scheduled_end = _as_utc(campaign.scheduled_end)
    candidate = max(now_utc, scheduled_start) if scheduled_start else now_utc

    if scheduled_end is not None and candidate >= scheduled_end:
        return None

    start_value = campaign.calling_hours_start
    end_value = campaign.calling_hours_end
    if start_value is None and end_value is None:
        return candidate
    if start_value is None or end_value is None:
        raise ValueError("Calling-hours start and end must both be configured")

    try:
        campaign_zone = ZoneInfo(campaign.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Campaign has an invalid IANA timezone") from exc

    start_hour = _parse_calling_hour(start_value)
    end_hour = _parse_calling_hour(end_value)
    if start_hour == end_hour:
        raise ValueError("Calling-hours start and end cannot be the same")

    local_candidate = candidate.astimezone(campaign_zone)
    local_hour = local_candidate.timetz().replace(tzinfo=None)
    if start_hour < end_hour:
        is_open = start_hour <= local_hour < end_hour
    else:
        is_open = local_hour >= start_hour or local_hour < end_hour

    if is_open:
        return candidate

    next_date = local_candidate.date()
    if local_hour >= start_hour:
        next_date += timedelta(days=1)
    next_local = datetime.combine(next_date, start_hour, tzinfo=campaign_zone)
    next_utc = next_local.astimezone(UTC)
    if scheduled_end is not None and next_utc >= scheduled_end:
        return None
    return next_utc


def _record_campaign_issue(campaign: Campaign, message: str) -> None:
    campaign.settings = {
        **(campaign.settings or {}),
        "last_dispatch_error": message,
        "last_dispatch_error_at": datetime.now(UTC).isoformat(),
    }


def _attempt_idempotency_key(
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    attempt_number: int,
) -> str:
    value = uuid.uuid5(
        _IDEMPOTENCY_NAMESPACE,
        f"{tenant_id}:{campaign_id}:{contact_id}:{attempt_number}",
    )
    return f"vai_campaign_{value.hex}"


def _validated_provider_variables(value: object) -> dict[str, str | int | float | bool]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("Campaign contact variables are invalid")
    variables: dict[str, str | int | float | bool] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 100
            or key == "_vav_call_id"
            or key.startswith("_voice_ai_")
            or not isinstance(item, (str, int, float, bool))
            or isinstance(item, str)
            and len(item) > 1_000
            or isinstance(item, float)
            and not math.isfinite(item)
        ):
            raise ValueError("Campaign contact variables are invalid")
        variables[key] = item
    if len(json.dumps(variables, separators=(",", ":")).encode()) > 16_384:
        raise ValueError("Campaign contact variables are invalid")
    return variables


def _provider_from_number(campaign: Campaign, agent: Agent) -> str | None:
    provider = (agent.voice_provider or "").strip().lower()
    if provider == "smallest":
        return "provider-managed"
    if provider == "twilio":
        configured = (campaign.settings or {}).get("from_number")
        configured_number = configured if isinstance(configured, str) and configured else None
        return configured_number or settings.twilio_default_from_number
    return "provider-managed"


def _has_unverified_smallest_resources(campaign: Campaign) -> bool:
    campaign_settings = campaign.settings or {}
    return any(key in campaign_settings for key in ("from_number", "from_product_id", "version_id"))


async def _mark_remaining_contacts_skipped(
    db: AsyncSession,
    campaign: Campaign,
    tenant_id: uuid.UUID,
) -> None:
    await db.execute(
        update(CampaignContact)
        .where(
            CampaignContact.tenant_id == tenant_id,
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.status == "pending",
        )
        .values(status="skipped")
    )
    counts = await refresh_campaign_metrics(db, campaign)
    if counts.pending == 0 and counts.active == 0 and counts.completed == counts.total:
        campaign.status = "completed"


async def _cancel_claimed_attempts_after_schedule_end(
    db: AsyncSession,
    campaign: Campaign,
    tenant_id: uuid.UUID,
) -> None:
    """Cancel claims that are proven not to have reached a provider."""
    attempts = (
        (
            await db.execute(
                select(CampaignContactAttempt)
                .where(
                    CampaignContactAttempt.tenant_id == tenant_id,
                    CampaignContactAttempt.campaign_id == campaign.id,
                    CampaignContactAttempt.state == "claimed",
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for attempt in attempts:
        attempt.state = "cancelled"
        attempt.finished_at = now
        attempt.error_code = "schedule_ended"
        attempt.error_message = "Schedule ended before provider dispatch"
        contact = await db.get(CampaignContact, attempt.contact_id)
        if contact and contact.tenant_id == tenant_id and contact.status == "dispatching":
            contact.status = "skipped"
        if attempt.call_id:
            call = await db.get(Call, attempt.call_id)
            if call and call.tenant_id == tenant_id and call.status == "dispatch_pending":
                call.status = "cancelled"
                call.ended_at = now


async def _load_valid_campaign_agent(
    db: AsyncSession,
    campaign: Campaign,
    tenant_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Agent | None:
    agent_query = select(Agent).where(
        Agent.id == campaign.agent_id,
        Agent.tenant_id == tenant_id,
    )
    if for_update:
        agent_query = agent_query.with_for_update().execution_options(populate_existing=True)
    agent = (await db.execute(agent_query)).scalar_one_or_none()
    if not agent or not agent.is_active:
        campaign.status = "paused"
        _record_campaign_issue(campaign, "Campaign agent is missing or inactive")
        return None

    provider = (agent.voice_provider or "").strip().lower()
    if provider not in {"smallest", "twilio"}:
        campaign.status = "paused"
        _record_campaign_issue(campaign, "Campaign voice provider is not supported")
        return None
    if provider == "smallest":
        if not agent.provider_agent_id:
            campaign.status = "paused"
            _record_campaign_issue(campaign, "Campaign agent is not provisioned on Smallest.ai")
            return None
        if not agent.provider_revision_id or not agent.last_synced_at:
            campaign.status = "paused"
            _record_campaign_issue(
                campaign,
                "Campaign agent has never completed its initial Smallest.ai publish",
            )
            return None
        if agent.sync_status != "synced":
            campaign.status = "paused"
            _record_campaign_issue(
                campaign,
                "Campaign agent has unpublished or unverified provider changes",
            )
            return None
        if _has_unverified_smallest_resources(campaign):
            campaign.status = "paused"
            _record_campaign_issue(
                campaign,
                "Smallest.ai campaign resources require tenant-owned inventory",
            )
            return None
    if provider == "twilio" and not _provider_from_number(campaign, agent):
        campaign.status = "paused"
        _record_campaign_issue(campaign, "Twilio campaign has no configured from number")
        return None

    if campaign.workflow_id is not None:
        workflow_query = select(Workflow).where(
            Workflow.id == campaign.workflow_id,
            Workflow.tenant_id == tenant_id,
        )
        if for_update:
            workflow_query = workflow_query.with_for_update().execution_options(
                populate_existing=True
            )
        workflow = (await db.execute(workflow_query)).scalar_one_or_none()
        if not workflow or not workflow.is_active:
            campaign.status = "paused"
            _record_campaign_issue(campaign, "Campaign workflow is missing or inactive")
            return None
    return agent


async def _mark_stale_dispatches_unknown(
    db: AsyncSession,
    campaign: Campaign,
    tenant_id: uuid.UUID,
) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=DISPATCH_UNCERTAINTY_SECONDS)
    stale_attempts = (
        (
            await db.execute(
                select(CampaignContactAttempt)
                .where(
                    CampaignContactAttempt.tenant_id == tenant_id,
                    CampaignContactAttempt.campaign_id == campaign.id,
                    CampaignContactAttempt.state == "dispatching",
                    CampaignContactAttempt.dispatch_started_at <= cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not stale_attempts:
        return False

    now = datetime.now(UTC)
    for attempt in stale_attempts:
        attempt.state = "unknown"
        attempt.finished_at = now
        attempt.error_code = "dispatch_outcome_unknown"
        attempt.error_message = (
            "Provider acceptance could not be reconciled; automatic redial is disabled"
        )
        contact = await db.get(CampaignContact, attempt.contact_id)
        if contact and contact.tenant_id == tenant_id and contact.status == "dispatching":
            contact.status = "dispatch_unknown"
        if attempt.call_id:
            call = await db.get(Call, attempt.call_id)
            if call and call.tenant_id == tenant_id and call.status == "dispatching":
                call.status = "dispatch_unknown"

    campaign.status = "paused"
    _record_campaign_issue(
        campaign,
        "A provider dispatch outcome is unknown. Reconcile it before resuming "
        "to avoid a duplicate call.",
    )
    await refresh_campaign_metrics(db, campaign)
    return True


def _accepted_call_deadline(
    attempt: CampaignContactAttempt,
    call: Call,
    max_call_duration_seconds: int | None,
) -> datetime:
    accepted_at = _as_utc(attempt.accepted_at) or _as_utc(call.started_at) or datetime.now(UTC)
    max_duration = max(int(max_call_duration_seconds or 600), 30)
    return accepted_at + timedelta(seconds=max_duration + ACCEPTED_CALL_GRACE_SECONDS)


async def _accepted_call_watchdog_rows(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> list[tuple[CampaignContactAttempt, Call, int]]:
    rows = (
        await db.execute(
            select(
                CampaignContactAttempt,
                Call,
                Agent.max_call_duration_seconds,
            )
            .join(
                Call,
                (Call.id == CampaignContactAttempt.call_id)
                & (Call.tenant_id == CampaignContactAttempt.tenant_id),
            )
            .join(
                Agent,
                (Agent.id == Call.agent_id) & (Agent.tenant_id == Call.tenant_id),
            )
            .where(
                CampaignContactAttempt.tenant_id == tenant_id,
                CampaignContactAttempt.campaign_id == campaign_id,
                CampaignContactAttempt.state == "accepted",
            )
        )
    ).all()
    return [(attempt, call, int(max_duration or 600)) for attempt, call, max_duration in rows]


async def _mark_stale_accepted_calls_unknown(
    db: AsyncSession,
    campaign: Campaign,
    tenant_id: uuid.UUID,
) -> bool:
    """Pause accepted calls whose terminal lifecycle never arrived.

    Provider acceptance is known, so these calls are never released or retried
    automatically. A late signed callback can still authoritatively reconcile
    the attempt; otherwise a trusted platform operator must investigate it.
    """
    now = datetime.now(UTC)
    rows = await _accepted_call_watchdog_rows(db, campaign.id, tenant_id)
    stale_rows = [
        (attempt, call)
        for attempt, call, max_duration in rows
        if _accepted_call_deadline(attempt, call, max_duration) <= now
    ]
    if not stale_rows:
        return False

    for attempt, call in stale_rows:
        attempt.state = "unknown"
        attempt.finished_at = now
        attempt.error_code = "terminal_callback_timeout"
        attempt.error_message = (
            "Provider accepted the call but no terminal signed callback arrived; "
            "automatic retry is disabled"
        )
        contact = await db.get(CampaignContact, attempt.contact_id)
        if contact and contact.tenant_id == tenant_id and contact.status == "calling":
            contact.status = "dispatch_unknown"
        if call.status not in TERMINAL_CALL_STATUSES:
            call.status = "dispatch_unknown"
            call.call_metadata = {
                **(call.call_metadata or {}),
                "dispatch_error": "terminal_callback_timeout",
                "attempt_id": str(attempt.id),
            }

    campaign.status = "paused"
    _record_campaign_issue(
        campaign,
        "An accepted provider call exceeded its maximum duration without a terminal "
        "callback. Keep the campaign paused for trusted reconciliation.",
    )
    await refresh_campaign_metrics(db, campaign)
    return True


async def _prepare_campaign_batch(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> BatchPlan:
    campaign = (
        await db.execute(
            select(Campaign)
            .where(Campaign.id == campaign_id, Campaign.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not campaign:
        await db.commit()
        return BatchPlan()

    # Terminal safety is independent from dispatch state. A manual pause (or a
    # completed/cancelled aggregate) must not strand a provider-accepted call if
    # its signed terminal callback is lost. This path can only mark the attempt
    # unknown and require operator review; it never makes a contact dispatchable.
    if await _mark_stale_accepted_calls_unknown(db, campaign, tenant_id):
        await db.commit()
        return BatchPlan()
    if campaign.status != "running":
        await db.commit()
        return BatchPlan()

    unresolved_attempts = int(
        await db.scalar(
            select(func.count())
            .select_from(CampaignContactAttempt)
            .where(
                CampaignContactAttempt.tenant_id == tenant_id,
                CampaignContactAttempt.campaign_id == campaign_id,
                CampaignContactAttempt.state == "unknown",
            )
        )
        or 0
    )
    if unresolved_attempts:
        campaign.status = "paused"
        _record_campaign_issue(
            campaign,
            "Reconcile unknown provider dispatches before resuming the campaign",
        )
        await db.commit()
        return BatchPlan()

    if await _mark_stale_dispatches_unknown(db, campaign, tenant_id):
        await db.commit()
        return BatchPlan()
    try:
        dispatch_at = _next_dispatch_time(campaign)
    except ValueError as exc:
        campaign.status = "paused"
        _record_campaign_issue(campaign, str(exc))
        await db.commit()
        return BatchPlan()

    if dispatch_at is None:
        await _cancel_claimed_attempts_after_schedule_end(db, campaign, tenant_id)
        await _mark_remaining_contacts_skipped(db, campaign, tenant_id)
        _record_campaign_issue(campaign, "Campaign schedule ended before dispatch completed")
        await db.commit()
        return BatchPlan()
    if dispatch_at > datetime.now(UTC) + timedelta(seconds=1):
        await db.commit()
        return BatchPlan(defer_until=dispatch_at)

    agent = await _load_valid_campaign_agent(db, campaign, tenant_id)
    if agent is None:
        await db.commit()
        return BatchPlan()

    existing_claims = (
        (
            await db.execute(
                select(CampaignContactAttempt.id)
                .where(
                    CampaignContactAttempt.tenant_id == tenant_id,
                    CampaignContactAttempt.campaign_id == campaign_id,
                    CampaignContactAttempt.state == "claimed",
                )
                .order_by(CampaignContactAttempt.created_at.asc())
                .limit(MAX_CAMPAIGN_BATCH_SIZE)
            )
        )
        .scalars()
        .all()
    )

    active_count = int(
        await db.scalar(
            select(func.count())
            .select_from(CampaignContact)
            .where(
                CampaignContact.tenant_id == tenant_id,
                CampaignContact.campaign_id == campaign_id,
                CampaignContact.status.in_(ACTIVE_CONTACT_STATUSES),
            )
        )
        or 0
    )
    capacity = max(campaign.max_concurrent_calls or 1, 1) - active_count
    new_attempt_ids: list[uuid.UUID] = []
    if capacity > 0:
        contacts = (
            (
                await db.execute(
                    select(CampaignContact)
                    .where(
                        CampaignContact.tenant_id == tenant_id,
                        CampaignContact.campaign_id == campaign_id,
                        CampaignContact.status == "pending",
                    )
                    .order_by(CampaignContact.created_at.asc())
                    .limit(min(capacity, MAX_CAMPAIGN_BATCH_SIZE))
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        contact_ids = [contact.id for contact in contacts]
        latest_attempt_number: dict[uuid.UUID, int] = {}
        if contact_ids:
            rows = (
                await db.execute(
                    select(
                        CampaignContactAttempt.contact_id,
                        func.max(CampaignContactAttempt.attempt_number),
                    )
                    .where(CampaignContactAttempt.contact_id.in_(contact_ids))
                    .group_by(CampaignContactAttempt.contact_id)
                )
            ).all()
            latest_attempt_number = {
                contact_id: int(attempt_number or 0) for contact_id, attempt_number in rows
            }

        provider = (agent.voice_provider or "").strip().lower() or "unknown"
        for contact in contacts:
            normalized_number = normalize_e164(contact.phone_number)
            if not normalized_number:
                contact.status = "failed"
                contact.context_data = {
                    **(contact.context_data or {}),
                    "dispatch_error": "Invalid E.164 phone number",
                }
                continue
            contact.phone_number = normalized_number
            attempt_number = latest_attempt_number.get(contact.id, 0) + 1
            attempt_id = uuid.uuid4()
            idempotency_key = _attempt_idempotency_key(
                tenant_id,
                campaign_id,
                contact.id,
                attempt_number,
            )
            attempt = CampaignContactAttempt(
                id=attempt_id,
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                contact_id=contact.id,
                attempt_number=attempt_number,
                idempotency_key=idempotency_key,
                provider=provider,
                state="claimed",
            )
            db.add(attempt)
            contact.status = "dispatching"
            new_attempt_ids.append(attempt_id)

    counts = await refresh_campaign_metrics(db, campaign)
    if counts.pending == 0 and counts.active == 0 and counts.completed == counts.total:
        campaign.status = "completed"
    await db.commit()
    return BatchPlan(attempt_ids=tuple([*existing_claims, *new_attempt_ids]))


async def _cancel_unstarted_attempt(
    db: AsyncSession,
    campaign: Campaign,
    contact: CampaignContact,
    attempt: CampaignContactAttempt,
    call: Call | None,
    *,
    contact_status: str,
    reason: str,
) -> None:
    now = datetime.now(UTC)
    attempt.state = "cancelled"
    attempt.finished_at = now
    attempt.error_code = reason
    attempt.error_message = reason.replace("_", " ").title()
    if call is not None:
        call.status = "failed" if contact_status == "failed" else "cancelled"
        call.ended_at = now
        call.call_metadata = {
            **(call.call_metadata or {}),
            "dispatch_cancelled": reason,
        }
    contact.status = contact_status
    counts = await refresh_campaign_metrics(db, campaign)
    if (
        campaign.status == "running"
        and counts.pending == 0
        and counts.active == 0
        and counts.completed == counts.total
    ):
        campaign.status = "completed"


async def _prepare_attempt_dispatch(
    db: AsyncSession,
    attempt_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> DispatchPreparation:
    # Read only immutable scalar identities before taking the aggregate lock.
    # Loading the ORM entity here would cache a stale `claimed` object while a
    # concurrent worker changes it to `dispatching`; SQLAlchemy could then reuse
    # that identity-map value for the later FOR UPDATE query and place a second
    # provider call.
    attempt_identity = (
        await db.execute(
            select(
                CampaignContactAttempt.campaign_id,
                CampaignContactAttempt.contact_id,
            ).where(
                CampaignContactAttempt.id == attempt_id,
                CampaignContactAttempt.tenant_id == tenant_id,
                CampaignContactAttempt.state == "claimed",
            )
        )
    ).one_or_none()
    if attempt_identity is None:
        await db.commit()
        return DispatchPreparation()
    campaign_id, contact_id = attempt_identity

    campaign = (
        await db.execute(
            select(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    contact = (
        await db.execute(
            select(CampaignContact)
            .where(
                CampaignContact.id == contact_id,
                CampaignContact.tenant_id == tenant_id,
                CampaignContact.campaign_id == campaign_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    attempt = (
        await db.execute(
            select(CampaignContactAttempt)
            .where(
                CampaignContactAttempt.id == attempt_id,
                CampaignContactAttempt.tenant_id == tenant_id,
                CampaignContactAttempt.campaign_id == campaign_id,
                CampaignContactAttempt.contact_id == contact_id,
                CampaignContactAttempt.state == "claimed",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if attempt is None:
        await db.commit()
        return DispatchPreparation()
    call = None
    if attempt.call_id is not None:
        call = (
            await db.execute(
                select(Call)
                .where(
                    Call.id == attempt.call_id,
                    Call.tenant_id == tenant_id,
                    Call.campaign_id == attempt.campaign_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
    if campaign is None or contact is None or (attempt.call_id is not None and call is None):
        attempt.state = "unknown"
        attempt.error_code = "invalid_attempt_mapping"
        attempt.finished_at = datetime.now(UTC)
        await db.commit()
        return DispatchPreparation()

    if campaign.status != "running":
        contact_status = "pending" if campaign.status == "paused" else "skipped"
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status=contact_status,
            reason="campaign_not_running",
        )
        await db.commit()
        return DispatchPreparation()

    try:
        dispatch_at = _next_dispatch_time(campaign)
    except ValueError as exc:
        campaign.status = "paused"
        _record_campaign_issue(campaign, str(exc))
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status="pending",
            reason="invalid_schedule",
        )
        await db.commit()
        return DispatchPreparation()
    if dispatch_at is None:
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status="skipped",
            reason="schedule_ended",
        )
        _record_campaign_issue(campaign, "Campaign schedule ended before dispatch completed")
        await db.commit()
        return DispatchPreparation()
    if dispatch_at > datetime.now(UTC) + timedelta(seconds=1):
        await db.commit()
        return DispatchPreparation(defer_until=dispatch_at)

    agent = await _load_valid_campaign_agent(db, campaign, tenant_id)
    if agent is None:
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status="pending",
            reason="agent_or_workflow_inactive",
        )
        await db.commit()
        return DispatchPreparation()

    try:
        context_variables = _validated_provider_variables(contact.context_data)
    except ValueError:
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status="failed",
            reason="invalid_provider_variables",
        )
        await db.commit()
        return DispatchPreparation()

    # Compliance is checked per contact immediately before the irreversible
    # provider side effect, rather than once for a potentially long batch.
    if await is_number_on_tenant_dnc(db, tenant_id, contact.phone_number):
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status="dnc",
            reason="do_not_call",
        )
        await db.commit()
        return DispatchPreparation()
    if await is_outbound_consent_revoked(db, tenant_id, contact.phone_number):
        await _cancel_unstarted_attempt(
            db,
            campaign,
            contact,
            attempt,
            call,
            contact_status="skipped",
            reason="consent_revoked",
        )
        await db.commit()
        return DispatchPreparation()

    if call is None:
        call_id = uuid.uuid4()
        from_number = _provider_from_number(campaign, agent)
        call = Call(
            id=call_id,
            tenant_id=tenant_id,
            agent_id=campaign.agent_id,
            campaign_id=campaign.id,
            direction="outbound",
            status="dispatch_pending",
            from_number=from_number or "provider-managed",
            to_number=contact.phone_number,
            provider=attempt.provider,
            call_metadata={
                **context_variables,
                "agent_configuration": agent_configuration_snapshot(agent),
                "campaign_dispatch": {
                    "attempt_id": str(attempt.id),
                    "contact_id": str(contact.id),
                    "idempotency_key": attempt.idempotency_key,
                },
            },
        )
        db.add(call)
        attempt.call_id = call_id
        contact.last_call_id = call_id

    now = datetime.now(UTC)
    attempt.state = "dispatching"
    attempt.dispatch_started_at = now
    contact.status = "dispatching"
    contact.attempts += 1
    call.status = "dispatching"
    call.started_at = now
    await db.commit()

    return DispatchPreparation(
        payload=DispatchPayload(
            attempt_id=attempt.id,
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            call_id=call.id,
            idempotency_key=attempt.idempotency_key,
            provider=attempt.provider,
            provider_agent_id=agent.provider_agent_id,
            provider_revision_id=agent.provider_revision_id,
            phone_number=contact.phone_number,
            from_number=call.from_number,
            context_data=context_variables,
        )
    )


def _smallest_variables(payload: DispatchPayload) -> dict:
    variables = dict(payload.context_data)
    # Smallest's current adapter exposes no provider idempotency header. These
    # signed-webhook correlation fields let an accepted call reconcile even if
    # the worker crashes before saving the returned conversation id.
    variables.update(
        {
            "_vav_call_id": str(payload.call_id),
            "_voice_ai_call_id": str(payload.call_id),
            "_voice_ai_campaign_id": str(payload.campaign_id),
            "_voice_ai_contact_id": str(payload.contact_id),
            "_voice_ai_attempt_id": str(payload.attempt_id),
            "_voice_ai_idempotency_key": payload.idempotency_key,
        }
    )
    return variables


async def _call_provider(payload: DispatchPayload) -> str:
    if payload.provider == "smallest":
        if not payload.provider_agent_id:
            raise DefinitiveDispatchError("Smallest.ai agent has not been provisioned")
        if not payload.provider_revision_id:
            raise DefinitiveDispatchError("Smallest.ai agent has no verified provider revision")
        return await get_smallest_client().start_outbound_call(
            agent_id=payload.provider_agent_id,
            phone_number=payload.phone_number,
            variables=_smallest_variables(payload),
            version_id=payload.provider_revision_id,
        )
    if payload.provider == "twilio":
        if not payload.from_number or payload.from_number == "provider-managed":
            raise DefinitiveDispatchError("Twilio campaign has no configured from number")
        # Twilio's existing adapter has no idempotency primitive. The durable
        # local call id in both callback URLs is therefore the reconciliation
        # identity and ambiguous dispatches are never automatically retried.
        result = await get_telephony_provider().make_call(
            CallRequest(
                to_number=payload.phone_number,
                from_number=payload.from_number,
                webhook_url=(f"{settings.base_url}/api/v1/webhooks/twilio/voice/{payload.call_id}"),
                status_callback_url=(
                    f"{settings.base_url}/api/v1/webhooks/twilio/status/{payload.call_id}"
                ),
            )
        )
        return result.provider_call_sid
    raise DefinitiveDispatchError(f"Unsupported voice provider: {payload.provider or 'unset'}")


async def _call_provider_with_final_guard(
    db: AsyncSession,
    payload: DispatchPayload,
) -> str | None:
    """Linearize the last compliance/state check with the provider side effect.

    The tenant+number advisory lock is shared with DNC mutations. Holding the
    campaign row lock at the same time also makes pause vs. dispatch ordering
    deterministic: whichever transaction wins is observed before dialing.
    ``None`` means the durable attempt was safely cancelled or was no longer
    eligible, so the caller must not record provider acceptance/failure.
    """
    async with tenant_phone_dnc_lock(db, payload.tenant_id, payload.phone_number):
        campaign = (
            await db.execute(
                select(Campaign)
                .where(
                    Campaign.id == payload.campaign_id,
                    Campaign.tenant_id == payload.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        contact = (
            await db.execute(
                select(CampaignContact)
                .where(
                    CampaignContact.id == payload.contact_id,
                    CampaignContact.campaign_id == payload.campaign_id,
                    CampaignContact.tenant_id == payload.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        attempt = (
            await db.execute(
                select(CampaignContactAttempt)
                .where(
                    CampaignContactAttempt.id == payload.attempt_id,
                    CampaignContactAttempt.contact_id == payload.contact_id,
                    CampaignContactAttempt.campaign_id == payload.campaign_id,
                    CampaignContactAttempt.tenant_id == payload.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        call = (
            await db.execute(
                select(Call)
                .where(
                    Call.id == payload.call_id,
                    Call.campaign_id == payload.campaign_id,
                    Call.tenant_id == payload.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if campaign is None or contact is None or attempt is None or call is None:
            logger.error(
                "campaign_dispatch_graph_missing",
                campaign_id=str(payload.campaign_id),
                attempt_id=str(payload.attempt_id),
            )
            await db.rollback()
            return None
        if (
            attempt.state != "dispatching"
            or attempt.call_id != call.id
            or contact.last_call_id != call.id
            or contact.status != "dispatching"
            or call.status != "dispatching"
        ):
            await db.commit()
            return None

        if campaign.status != "running":
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="pending" if campaign.status == "paused" else "skipped",
                reason="campaign_not_running",
            )
            await db.commit()
            return None

        try:
            dispatch_at = _next_dispatch_time(campaign)
        except ValueError as exc:
            campaign.status = "paused"
            _record_campaign_issue(campaign, str(exc))
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="pending",
                reason="invalid_schedule",
            )
            await db.commit()
            return None
        if dispatch_at is None:
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="skipped",
                reason="schedule_ended",
            )
            await db.commit()
            return None
        if dispatch_at > datetime.now(UTC) + timedelta(seconds=1):
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="pending",
                reason="outside_calling_window",
            )
            await db.commit()
            return None

        # The final readiness check is part of the same transaction as the
        # provider side effect. Locking and repopulating these rows makes a
        # committed agent edit/deactivation or workflow deactivation win before
        # dialing, while an edit that waits behind this lock linearizes after
        # the already-authorized call.
        agent = await _load_valid_campaign_agent(
            db,
            campaign,
            payload.tenant_id,
            for_update=True,
        )
        current_provider = (
            (agent.voice_provider or "").strip().lower() if agent is not None else None
        )
        current_from_number = _provider_from_number(campaign, agent) if agent else None
        provider_identity_changed = bool(
            agent
            and (
                current_provider != payload.provider
                or current_provider == "smallest"
                and (
                    agent.provider_agent_id != payload.provider_agent_id
                    or agent.provider_revision_id != payload.provider_revision_id
                )
                or current_provider == "twilio"
                and current_from_number != payload.from_number
            )
        )
        if agent is None or provider_identity_changed:
            if provider_identity_changed:
                campaign.status = "paused"
                _record_campaign_issue(
                    campaign,
                    "Campaign provider configuration changed before dispatch",
                )
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="pending",
                reason="agent_or_provider_changed",
            )
            await db.commit()
            return None

        if await is_number_on_tenant_dnc(db, payload.tenant_id, payload.phone_number):
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="dnc",
                reason="do_not_call",
            )
            await db.commit()
            return None
        if await is_outbound_consent_revoked(
            db,
            payload.tenant_id,
            payload.phone_number,
        ):
            await _cancel_unstarted_attempt(
                db,
                campaign,
                contact,
                attempt,
                call,
                contact_status="skipped",
                reason="consent_revoked",
            )
            await db.commit()
            return None

        provider_call_sid = await _call_provider(payload)
        # Release the transaction-scoped advisory and campaign locks before the
        # provider response is reconciled in its own durable transaction.
        await db.commit()
        return provider_call_sid


def _is_definitive_provider_rejection(exc: Exception) -> bool:
    if isinstance(exc, DefinitiveDispatchError):
        return True
    status_code = getattr(exc, "upstream_status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status", None)
    return isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 408


async def _record_provider_acceptance(
    db: AsyncSession,
    payload: DispatchPayload,
    provider_call_sid: str,
) -> None:
    campaign = (
        await db.execute(
            select(Campaign)
            .where(
                Campaign.id == payload.campaign_id,
                Campaign.tenant_id == payload.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    contact = (
        await db.execute(
            select(CampaignContact)
            .where(
                CampaignContact.id == payload.contact_id,
                CampaignContact.tenant_id == payload.tenant_id,
                CampaignContact.campaign_id == payload.campaign_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    attempt = (
        await db.execute(
            select(CampaignContactAttempt)
            .where(
                CampaignContactAttempt.id == payload.attempt_id,
                CampaignContactAttempt.tenant_id == payload.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    call = (
        await db.execute(
            select(Call)
            .where(Call.id == payload.call_id, Call.tenant_id == payload.tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if campaign is None or contact is None or attempt is None or call is None:
        raise RuntimeError("Durable campaign dispatch records are missing")

    if call.provider_call_sid and call.provider_call_sid != provider_call_sid:
        raise RuntimeError("Provider returned conflicting call identities")
    call.provider_call_sid = provider_call_sid
    attempt.provider_call_sid = provider_call_sid

    # A callback can win this race and make the attempt terminal before the
    # provider HTTP response is committed. Never regress that terminal state.
    if attempt.state == "dispatching":
        attempt.state = "accepted"
        attempt.accepted_at = attempt.accepted_at or datetime.now(UTC)
    if call.status not in {"completed", "failed", "busy", "no_answer"}:
        call.status = "ringing"
    if contact.status == "dispatching":
        contact.status = "calling"
    await db.commit()


async def _record_provider_failure(
    db: AsyncSession,
    payload: DispatchPayload,
    exc: Exception,
) -> CampaignLifecycleResult:
    campaign = (
        await db.execute(
            select(Campaign)
            .where(
                Campaign.id == payload.campaign_id,
                Campaign.tenant_id == payload.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    contact = (
        await db.execute(
            select(CampaignContact)
            .where(
                CampaignContact.id == payload.contact_id,
                CampaignContact.tenant_id == payload.tenant_id,
                CampaignContact.campaign_id == payload.campaign_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    attempt = (
        await db.execute(
            select(CampaignContactAttempt)
            .where(
                CampaignContactAttempt.id == payload.attempt_id,
                CampaignContactAttempt.tenant_id == payload.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    call = (
        await db.execute(
            select(Call)
            .where(Call.id == payload.call_id, Call.tenant_id == payload.tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if campaign is None or contact is None or attempt is None or call is None:
        raise RuntimeError("Durable campaign dispatch records are missing")

    # A signed callback is stronger evidence than an HTTP exception observed by
    # the dispatch worker. Preserve its accepted/terminal reconciliation.
    if attempt.state in {"accepted", "completed", "failed"} or call.provider_call_sid:
        await db.commit()
        return CampaignLifecycleResult()

    message = str(exc)[:1000]
    attempt.error_message = message
    attempt.error_code = exc.__class__.__name__[:100]
    now = datetime.now(UTC)
    if _is_definitive_provider_rejection(exc):
        attempt.state = "rejected"
        attempt.finished_at = now
        call.status = "failed"
        call.ended_at = now
        call.call_metadata = {
            **(call.call_metadata or {}),
            "dispatch_error": message,
            "attempt_id": str(attempt.id),
        }
        result = await sync_campaign_call_lifecycle(db, call)
        await db.commit()
        return result

    attempt.state = "unknown"
    attempt.finished_at = now
    call.status = "dispatch_unknown"
    call.call_metadata = {
        **(call.call_metadata or {}),
        "dispatch_error": "Provider acceptance is unknown; automatic retry disabled",
        "attempt_id": str(attempt.id),
    }
    contact.status = "dispatch_unknown"
    campaign.status = "paused"
    _record_campaign_issue(
        campaign,
        "Provider acceptance is unknown. Reconcile this attempt before resuming.",
    )
    await refresh_campaign_metrics(db, campaign)
    await db.commit()
    return CampaignLifecycleResult()


async def _campaign_follow_up(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> FollowUp:
    campaign = (
        await db.execute(
            select(Campaign)
            .where(Campaign.id == campaign_id, Campaign.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not campaign or campaign.status != "running":
        await db.commit()
        return FollowUp()

    if await _mark_stale_dispatches_unknown(db, campaign, tenant_id):
        await db.commit()
        return FollowUp()
    if await _mark_stale_accepted_calls_unknown(db, campaign, tenant_id):
        await db.commit()
        return FollowUp()

    counts = await refresh_campaign_metrics(db, campaign)
    if counts.pending == 0 and counts.active == 0 and counts.completed == counts.total:
        campaign.status = "completed"
        await db.commit()
        return FollowUp()

    try:
        dispatch_at = _next_dispatch_time(campaign)
    except ValueError as exc:
        campaign.status = "paused"
        _record_campaign_issue(campaign, str(exc))
        await db.commit()
        return FollowUp()
    if dispatch_at is None:
        await _cancel_claimed_attempts_after_schedule_end(db, campaign, tenant_id)
        await _mark_remaining_contacts_skipped(db, campaign, tenant_id)
        _record_campaign_issue(campaign, "Campaign schedule ended before dispatch completed")
        await db.commit()
        return FollowUp()

    claimed_count = int(
        await db.scalar(
            select(func.count())
            .select_from(CampaignContactAttempt)
            .where(
                CampaignContactAttempt.tenant_id == tenant_id,
                CampaignContactAttempt.campaign_id == campaign_id,
                CampaignContactAttempt.state == "claimed",
            )
        )
        or 0
    )
    oldest_dispatching = await db.scalar(
        select(func.min(CampaignContactAttempt.dispatch_started_at)).where(
            CampaignContactAttempt.tenant_id == tenant_id,
            CampaignContactAttempt.campaign_id == campaign_id,
            CampaignContactAttempt.state == "dispatching",
        )
    )
    accepted_rows = await _accepted_call_watchdog_rows(db, campaign_id, tenant_id)
    earliest_accepted_deadline = min(
        (
            _accepted_call_deadline(attempt, call, max_duration)
            for attempt, call, max_duration in accepted_rows
        ),
        default=None,
    )
    await db.commit()

    if dispatch_at > datetime.now(UTC) + timedelta(seconds=1) and (
        counts.pending > 0 or claimed_count > 0
    ):
        return FollowUp(eta=dispatch_at)
    if claimed_count > 0 or (
        counts.pending > 0 and counts.active < max(campaign.max_concurrent_calls or 1, 1)
    ):
        return FollowUp(countdown=NEXT_BATCH_DELAY_SECONDS)
    if oldest_dispatching is not None:
        started = _as_utc(oldest_dispatching) or datetime.now(UTC)
        reconcile_at = started + timedelta(seconds=DISPATCH_UNCERTAINTY_SECONDS)
        return FollowUp(eta=max(reconcile_at, datetime.now(UTC) + timedelta(seconds=1)))
    if earliest_accepted_deadline is not None:
        return FollowUp(
            eta=max(earliest_accepted_deadline, datetime.now(UTC) + timedelta(seconds=1))
        )
    # Accepted calls are advanced by signed lifecycle callbacks and bounded by
    # the watchdog above. Do not spin while no active provider call remains.
    return FollowUp()


def _schedule_follow_up(campaign_id: str, tenant_id: str, follow_up: FollowUp) -> None:
    if follow_up.eta is not None:
        run_campaign.apply_async(args=[campaign_id, tenant_id], eta=follow_up.eta)
    elif follow_up.countdown is not None:
        run_campaign.apply_async(
            args=[campaign_id, tenant_id],
            countdown=follow_up.countdown,
        )


async def _run_campaign_async(campaign_id: str, tenant_id: str):
    from app.core.database import async_session_factory

    try:
        campaign_uuid = uuid.UUID(campaign_id)
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        logger.warning("campaign_skip", campaign_id=campaign_id, reason="invalid identifiers")
        return

    async with async_session_factory() as db:
        plan = await _prepare_campaign_batch(db, campaign_uuid, tenant_uuid)

    if plan.defer_until is not None:
        _schedule_follow_up(
            campaign_id,
            tenant_id,
            FollowUp(eta=plan.defer_until),
        )
        return

    earliest_defer: datetime | None = None
    for attempt_id in plan.attempt_ids:
        async with async_session_factory() as db:
            preparation = await _prepare_attempt_dispatch(db, attempt_id, tenant_uuid)
        if preparation.defer_until is not None:
            earliest_defer = min(
                earliest_defer or preparation.defer_until,
                preparation.defer_until,
            )
            continue
        if preparation.payload is None:
            continue

        payload = preparation.payload
        try:
            async with async_session_factory() as db:
                provider_call_sid = await _call_provider_with_final_guard(db, payload)
        except Exception as exc:
            async with async_session_factory() as db:
                await _record_provider_failure(db, payload, exc)
            logger.warning(
                "campaign_dispatch_not_accepted",
                campaign_id=campaign_id,
                attempt_id=str(attempt_id),
                definitive=_is_definitive_provider_rejection(exc),
            )
        else:
            if provider_call_sid is None:
                continue
            # This commit is intentionally separate from the provider request.
            # A crash here leaves state=dispatching, which can be reconciled by
            # callback but can never be automatically redialed.
            async with async_session_factory() as db:
                await _record_provider_acceptance(db, payload, provider_call_sid)

    async with async_session_factory() as db:
        follow_up = await _campaign_follow_up(db, campaign_uuid, tenant_uuid)
    if earliest_defer is not None and (follow_up.eta is None or earliest_defer < follow_up.eta):
        follow_up = FollowUp(eta=earliest_defer)
    _schedule_follow_up(campaign_id, tenant_id, follow_up)

    logger.info(
        "campaign_batch_processed",
        campaign_id=campaign_id,
        contacts_processed=len(plan.attempt_ids),
    )
