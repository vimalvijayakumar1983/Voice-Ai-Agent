"""Post-call processing tasks."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog

from app.tasks.worker import celery_app

logger = structlog.get_logger()
DISPATCH_RECONCILE_DELAY_SECONDS = 15 * 60
DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS = 120
DIRECT_CALL_WATCHDOG_STATUSES = frozenset({"ringing", "in_progress"})
DIRECT_CALL_UNKNOWN_STATUS = "terminal_unknown"


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded_call_duration(value: int | None) -> int:
    return max(int(value or 600), 30)


def arm_direct_call_terminal_watchdog(
    call,
    max_call_duration_seconds: int | None,
) -> datetime:
    """Persist the fixed deadline captured when a direct call is accepted."""
    accepted_at = _as_utc(call.started_at) or datetime.now(UTC)
    max_duration = _bounded_call_duration(max_call_duration_seconds)
    deadline = accepted_at + timedelta(
        seconds=max_duration + DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS
    )
    metadata = dict(call.call_metadata or {})
    metadata["terminal_watchdog"] = {
        "accepted_at": accepted_at.isoformat(),
        "deadline": deadline.isoformat(),
        "max_call_duration_seconds": max_duration,
        "grace_seconds": DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS,
        "status": "armed",
    }
    call.call_metadata = metadata
    return deadline


def _stored_watchdog_deadline(call) -> datetime | None:
    metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
    watchdog = metadata.get("terminal_watchdog")
    if not isinstance(watchdog, dict):
        return None
    raw_deadline = watchdog.get("deadline")
    if not isinstance(raw_deadline, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw_deadline.replace("Z", "+00:00")))
    except ValueError:
        return None


def _direct_call_terminal_deadline(call, max_call_duration_seconds: int | None) -> datetime:
    stored_deadline = _stored_watchdog_deadline(call)
    if stored_deadline is not None:
        return stored_deadline
    accepted_at = _as_utc(call.started_at) or _as_utc(call.created_at) or datetime.now(UTC)
    return accepted_at + timedelta(
        seconds=(
            _bounded_call_duration(max_call_duration_seconds)
            + DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS
        )
    )


def _mark_direct_call_terminal_unknown(call, now: datetime) -> None:
    """Surface a lost terminal callback without erasing provider identity."""
    metadata = dict(call.call_metadata or {})
    watchdog = dict(metadata.get("terminal_watchdog") or {})
    watchdog.update(
        {
            "status": "operator_review",
            "timed_out_at": now.isoformat(),
        }
    )
    metadata.update(
        {
            "terminal_watchdog": watchdog,
            "lifecycle_error": "terminal_callback_timeout",
            "operator_review_required": True,
            "automatic_redial_disabled": True,
        }
    )
    call.status = DIRECT_CALL_UNKNOWN_STATUS
    call.call_metadata = metadata


@celery_app.task(name="app.tasks.call_tasks.process_completed_call", bind=True, max_retries=3)
def process_completed_call(
    self,
    call_id: str,
    tenant_id: str,
    source_event_id: str | None = None,
    event_type: str = "call.completed",
):
    """Idempotently process a completed call and publish its stable event."""
    try:
        webhook_payload = _run_async(_process_completed_call_async(call_id, tenant_id))
        if webhook_payload is not None:
            from app.tasks.webhook_tasks import fire_webhook_event

            fire_webhook_event.delay(
                tenant_id,
                event_type,
                webhook_payload,
                source_event_id or f"call.completed:{call_id}",
            )
    except (TypeError, ValueError):
        # Task identities are generated internally. Malformed identities are
        # terminal and must not consume the retry budget indefinitely.
        logger.error("call_processing_invalid_identity", call_id=call_id)
        return "invalid_identity"
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "call_processing_exhausted",
                call_id=call_id,
                error_type=type(exc).__name__,
            )
            raise RuntimeError("call_processing_failed") from None
        countdown = min(30 * (2**self.request.retries), 15 * 60)
        logger.warning(
            "call_processing_retry_scheduled",
            call_id=call_id,
            error_type=type(exc).__name__,
            retry_number=self.request.retries + 1,
        )
        raise self.retry(
            exc=RuntimeError("call_processing_failed"),
            countdown=countdown,
        ) from None
    return "processed" if webhook_payload is not None else "missing"


@celery_app.task(
    name="app.tasks.call_tasks.reconcile_call_dispatch",
    bind=True,
    max_retries=3,
)
def reconcile_call_dispatch(self, call_id: str, tenant_id: str):
    """Terminally surface a provider dispatch whose result was never recorded."""
    try:
        return _run_async(_reconcile_call_dispatch_async(call_id, tenant_id))
    except (TypeError, ValueError):
        return "invalid_identity"
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "call_dispatch_reconciliation_exhausted",
                call_id=call_id,
                error_type=type(exc).__name__,
            )
            raise RuntimeError("call_dispatch_reconciliation_failed") from None
        raise self.retry(
            exc=RuntimeError("call_dispatch_reconciliation_failed"),
            countdown=min(30 * (2**self.request.retries), 15 * 60),
        ) from None


async def _reconcile_call_dispatch_async(call_id: str, tenant_id: str) -> str:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.call import Call

    call_uuid = uuid.UUID(call_id)
    tenant_uuid = uuid.UUID(tenant_id)
    async with async_session_factory() as db:
        result = await db.execute(
            select(Call)
            .where(Call.id == call_uuid, Call.tenant_id == tenant_uuid)
            .with_for_update()
        )
        call = result.scalar_one_or_none()
        if call is None:
            return "missing"
        if call.status != "dispatching":
            return "resolved"

        call.status = "dispatch_unknown"
        call.call_metadata = {
            **(call.call_metadata or {}),
            "dispatch_error": "provider_result_unknown",
        }
        await db.commit()
        return "dispatch_unknown"


@celery_app.task(name="app.tasks.call_tasks.sweep_stale_call_dispatches")
def sweep_stale_call_dispatches():
    """Periodic safety net for claims stranded before per-call task enqueue."""
    return _run_async(_sweep_stale_call_dispatches_async())


async def _sweep_stale_call_dispatches_async(limit: int = 500) -> int:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.call import Call

    cutoff = datetime.now(UTC) - timedelta(seconds=DISPATCH_RECONCILE_DELAY_SECONDS)
    async with async_session_factory() as db:
        result = await db.execute(
            select(Call)
            .where(Call.status == "dispatching", Call.created_at <= cutoff)
            .order_by(Call.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        calls = result.scalars().all()
        for call in calls:
            call.status = "dispatch_unknown"
            call.call_metadata = {
                **(call.call_metadata or {}),
                "dispatch_error": "provider_result_unknown",
            }
        await db.commit()
        return len(calls)


@celery_app.task(
    name="app.tasks.call_tasks.reconcile_direct_call_terminal",
    bind=True,
    max_retries=3,
)
def reconcile_direct_call_terminal(self, call_id: str, tenant_id: str):
    """Reconcile one accepted direct call whose terminal callback may be lost."""
    try:
        return _run_async(_reconcile_direct_call_terminal_async(call_id, tenant_id))
    except (TypeError, ValueError):
        return "invalid_identity"
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "direct_call_terminal_watchdog_exhausted",
                call_id=call_id,
                error_type=type(exc).__name__,
            )
            raise RuntimeError("direct_call_terminal_watchdog_failed") from None
        raise self.retry(
            exc=RuntimeError("direct_call_terminal_watchdog_failed"),
            countdown=min(30 * (2**self.request.retries), 15 * 60),
        ) from None


async def _reconcile_direct_call_terminal_async(
    call_id: str,
    tenant_id: str,
    *,
    now: datetime | None = None,
) -> str:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.agent import Agent
    from app.models.call import Call

    call_uuid = uuid.UUID(call_id)
    tenant_uuid = uuid.UUID(tenant_id)
    observed_at = _as_utc(now) or datetime.now(UTC)
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(Call, Agent.max_call_duration_seconds)
                .outerjoin(
                    Agent,
                    (Agent.id == Call.agent_id) & (Agent.tenant_id == Call.tenant_id),
                )
                .where(Call.id == call_uuid, Call.tenant_id == tenant_uuid)
                .with_for_update(of=Call)
            )
        ).one_or_none()
        if row is None:
            return "missing"

        call, max_duration = row
        if call.direction != "outbound" or call.campaign_id is not None:
            return "not_direct"
        if call.status not in DIRECT_CALL_WATCHDOG_STATUSES or not call.provider_call_sid:
            return "resolved"
        if _direct_call_terminal_deadline(call, max_duration) > observed_at:
            return "not_due"

        _mark_direct_call_terminal_unknown(call, observed_at)
        await db.commit()
        return DIRECT_CALL_UNKNOWN_STATUS


@celery_app.task(name="app.tasks.call_tasks.sweep_stale_direct_calls")
def sweep_stale_direct_calls():
    """Periodic recovery scan for accepted direct calls missing a terminal event."""
    return _run_async(_sweep_stale_direct_calls_async())


async def _sweep_stale_direct_calls_async(
    limit: int = 500,
    *,
    now: datetime | None = None,
) -> int:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.agent import Agent
    from app.models.call import Call

    bounded_limit = min(max(int(limit), 1), 1000)
    observed_at = _as_utc(now) or datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Call, Agent.max_call_duration_seconds)
                .outerjoin(
                    Agent,
                    (Agent.id == Call.agent_id) & (Agent.tenant_id == Call.tenant_id),
                )
                .where(
                    Call.direction == "outbound",
                    Call.campaign_id.is_(None),
                    Call.status.in_(DIRECT_CALL_WATCHDOG_STATUSES),
                    Call.provider_call_sid.is_not(None),
                )
                .order_by(Call.started_at, Call.created_at)
                .limit(bounded_limit)
                .with_for_update(skip_locked=True, of=Call)
            )
        ).all()

        timed_out = 0
        for call, max_duration in rows:
            if _direct_call_terminal_deadline(call, max_duration) > observed_at:
                continue
            _mark_direct_call_terminal_unknown(call, observed_at)
            timed_out += 1

        await db.commit()
        return timed_out


async def _process_completed_call_async(call_id: str, tenant_id: str):
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.core.database import async_session_factory
    from app.models.billing import UsageRecord
    from app.models.call import Call, CallSummary, CallTranscript

    call_uuid = uuid.UUID(call_id)
    tenant_uuid = uuid.UUID(tenant_id)
    transcript_text: str | None = None
    should_generate_summary = False

    async with async_session_factory() as db:
        # Persist billing and capture optional summary input under a short row
        # lock. No network operation is allowed inside this transaction: a
        # slow LLM must never block terminal provider analytics/callbacks.
        result = await db.execute(
            select(Call)
            .where(Call.id == call_uuid, Call.tenant_id == tenant_uuid)
            .with_for_update()
        )
        call = result.scalar_one_or_none()
        if not call:
            return None

        transcript_result = await db.execute(
            select(CallTranscript).where(
                CallTranscript.call_id == call_uuid,
                CallTranscript.tenant_id == tenant_uuid,
            )
        )
        transcript = transcript_result.scalar_one_or_none()
        if transcript and transcript.full_text:
            transcript_text = transcript.full_text
            summary_result = await db.execute(
                select(CallSummary).where(
                    CallSummary.call_id == call_uuid,
                    CallSummary.tenant_id == tenant_uuid,
                )
            )
            summary = summary_result.scalar_one_or_none()
            has_provider_analytics = bool(
                isinstance(call.call_metadata, dict)
                and call.call_metadata.get("smallest_analytics") is not None
            )
            should_generate_summary = summary is None and not has_provider_analytics

        # Record usage
        if call.duration_seconds and call.duration_seconds > 0:
            now = datetime.now(UTC)
            usage_result = await db.execute(
                select(UsageRecord).where(
                    UsageRecord.tenant_id == tenant_uuid,
                    UsageRecord.call_id == call_uuid,
                    UsageRecord.usage_type == "call_minutes",
                )
            )
            usage = usage_result.scalar_one_or_none()
            quantity = call.duration_seconds / 60.0
            cost_cents = int(quantity * 5)  # $0.05/min default
            if usage is None:
                usage = UsageRecord(
                    tenant_id=tenant_uuid,
                    call_id=call_uuid,
                    usage_type="call_minutes",
                    quantity=quantity,
                    unit="minutes",
                    cost_cents=cost_cents,
                    period_start=now,
                    period_end=now,
                )
                db.add(usage)
            else:
                # A provider may correct the final duration in a later callback.
                usage.quantity = quantity
                usage.cost_cents = cost_cents
                usage.period_end = now

        await db.commit()

    summary_data = None
    if should_generate_summary and transcript_text:
        try:
            from app.ai.conversation import conversation_engine

            # Deliberately outside every database transaction/row lock.
            summary_data = await conversation_engine.generate_call_summary(transcript_text)
        except Exception as exc:
            # Billing and lifecycle delivery should still complete when an
            # optional LLM summary provider is unavailable.
            logger.error(
                "summary_generation_failed",
                call_id=call_id,
                error_type=type(exc).__name__,
            )

    if summary_data is not None:
        async with async_session_factory() as db:
            call = (
                await db.execute(
                    select(Call)
                    .where(Call.id == call_uuid, Call.tenant_id == tenant_uuid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if call is not None:
                summary = (
                    await db.execute(
                        select(CallSummary)
                        .where(
                            CallSummary.call_id == call_uuid,
                            CallSummary.tenant_id == tenant_uuid,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                has_provider_analytics = bool(
                    isinstance(call.call_metadata, dict)
                    and call.call_metadata.get("smallest_analytics") is not None
                )
                if summary is None and not has_provider_analytics:
                    try:
                        async with db.begin_nested():
                            db.add(
                                CallSummary(
                                    tenant_id=tenant_uuid,
                                    call_id=call_uuid,
                                    summary=summary_data.get("summary", ""),
                                    key_topics=summary_data.get("key_topics", []),
                                    action_items=summary_data.get("action_items", []),
                                    sentiment=summary_data.get("sentiment"),
                                )
                            )
                            await db.flush()
                    except IntegrityError:
                        # A provider analytics callback won the conditional
                        # insert; its authoritative summary is preserved.
                        pass
                    else:
                        if summary_data.get("disposition") and not call.disposition:
                            call.disposition = summary_data["disposition"]
                await db.commit()

    async with async_session_factory() as db:
        call = await db.scalar(
            select(Call).where(Call.id == call_uuid, Call.tenant_id == tenant_uuid)
        )
    if call is None:
        return None

    logger.info("call_processed", call_id=call_id)
    return {
        "call_id": call_id,
        "status": call.status,
        "duration_seconds": call.duration_seconds,
        "disposition": call.disposition,
        "direction": call.direction,
    }
