"""Post-call processing tasks."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import settings
from app.livekit_runtime.browser_session import delete_browser_room
from app.livekit_runtime.constants import BROWSER_TOKEN_TTL_SECONDS
from app.services.call_disposition import (
    apply_grounding_quality_guard,
    disposition_catalog,
    infer_disposition_profile,
    normalize_call_analysis,
    normalize_provider_call_analysis,
    summarize_runtime_grounding,
)
from app.tasks.async_runner import run_async as _run_async
from app.tasks.worker import celery_app

logger = structlog.get_logger()
DISPATCH_RECONCILE_DELAY_SECONDS = 15 * 60
DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS = 120
DIRECT_CALL_WATCHDOG_STATUSES = frozenset({"ringing", "in_progress"})
DIRECT_CALL_UNKNOWN_STATUS = "terminal_unknown"
REALTIME_CALL_WATCHDOG_PROVIDERS = frozenset({"sarvam", "elevenlabs", "inworld"})


def _has_substantive_caller_input(turns: object) -> bool:
    """Return whether the persisted transcript contains actual caller speech."""
    if not isinstance(turns, list):
        return False
    return any(
        isinstance(turn, dict)
        and str(turn.get("role") or "").strip().lower() == "user"
        and isinstance(turn.get("content"), str)
        and bool(turn["content"].strip())
        for turn in turns
    )


def _insufficient_transcript_summary(existing_disposition: str | None = None) -> dict:
    """Build an evidence-safe outcome without asking an LLM to infer one."""
    return {
        "summary": (
            "No substantive caller speech was captured, so no call outcome or follow-up "
            "could be determined."
        ),
        "key_topics": [],
        "action_items": [],
        "sentiment": "neutral",
        "disposition": existing_disposition or "unknown",
        "resolution": "unknown",
        "confidence": 0.0,
        "evidence": [],
        "needs_review": True,
        "follow_up": {"required": False},
        "analysis_source": "rules",
    }


def _analysis_unavailable_summary(existing_disposition: str | None = None) -> dict:
    """Persist a visible review state when optional post-call AI is unavailable."""
    return {
        "summary": "Automated call analysis was unavailable. Review the transcript before action.",
        "key_topics": [],
        "action_items": ["Review the call transcript"],
        "sentiment": "neutral",
        "disposition": existing_disposition or "unknown",
        "resolution": "unknown",
        "confidence": 0.0,
        "evidence": [],
        "needs_review": True,
        "follow_up": {"required": True, "action": "Review the call transcript", "owner": None},
        "analysis_source": "unavailable",
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded_call_duration(value: int | None) -> int:
    return max(int(value or 600), 30)


def _browser_watchdog_duration(metadata: dict, current_agent_duration: int | None) -> int:
    """Honor the immutable per-call cap even if the agent is later expanded."""
    reserved_duration = metadata.get("reserved_max_duration_seconds")
    if (
        isinstance(reserved_duration, bool)
        or not isinstance(reserved_duration, int)
        or not 30 <= reserved_duration <= 7200
    ):
        return 30
    return min(reserved_duration, _bounded_call_duration(current_agent_duration))


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


def _mark_realtime_call_terminal_unknown(call, now: datetime) -> None:
    """Close an abandoned inbound media session without inventing a duration."""
    metadata = dict(call.call_metadata or {})
    metadata.update(
        {
            "lifecycle_error": "realtime_session_timeout",
            "operator_review_required": True,
            "recovered_at": now.isoformat(),
        }
    )
    call.status = DIRECT_CALL_UNKNOWN_STATUS
    call.ended_at = call.ended_at or now
    call.call_metadata = metadata


def _mark_browser_join_timeout(call, now: datetime) -> None:
    """Release a known never-connected browser reservation without ambiguity."""
    metadata = dict(call.call_metadata or {})
    metadata.update(
        {
            "lifecycle_error": "livekit_browser_join_timeout",
            "operator_review_required": False,
            "automatic_redial_disabled": True,
            "recovered_at": now.isoformat(),
            "session_issuance": "expired",
        }
    )
    call.status = "failed"
    call.started_at = None
    call.answered_at = None
    call.ended_at = call.ended_at or now
    call.duration_seconds = 0
    call.call_metadata = metadata


async def _cleanup_expired_browser_rooms(room_names: list[str]) -> None:
    """Best-effort bounded cleanup after the database transaction is committed."""
    if not room_names:
        return
    semaphore = asyncio.Semaphore(10)

    async def cleanup(room_name: str) -> None:
        async with semaphore:
            try:
                removed = await asyncio.wait_for(
                    delete_browser_room(
                        url=settings.livekit_url,
                        api_key=settings.livekit_api_key,
                        api_secret=settings.livekit_api_secret,
                        room_name=room_name,
                    ),
                    timeout=5,
                )
                if not removed:
                    logger.warning(
                        "livekit_browser_join_timeout_cleanup_unconfirmed",
                        room_name=room_name,
                    )
            except Exception as exc:
                logger.warning(
                    "livekit_browser_join_timeout_cleanup_failed",
                    room_name=room_name,
                    error_type=type(exc).__name__,
                )

    await asyncio.gather(*(cleanup(room_name) for room_name in set(room_names)))


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
    name="app.tasks.call_tasks.reprocess_call_disposition",
    bind=True,
    max_retries=2,
)
def reprocess_call_disposition(self, call_id: str, tenant_id: str):
    """Rebuild a stored call outcome without replaying lifecycle webhooks or billing."""
    try:
        result = _run_async(_process_completed_call_async(call_id, tenant_id, force_analysis=True))
    except (TypeError, ValueError):
        return "invalid_identity"
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "call_disposition_reprocessing_exhausted",
                call_id=call_id,
                error_type=type(exc).__name__,
            )
            raise RuntimeError("call_disposition_reprocessing_failed") from None
        raise self.retry(exc=RuntimeError("call_disposition_reprocessing_failed"), countdown=30)
    return "processed" if result is not None else "missing"


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


@celery_app.task(name="app.tasks.call_tasks.sweep_stale_realtime_calls")
def sweep_stale_realtime_calls():
    """Recover inbound realtime sessions stranded by provider handshake failures."""
    return _run_async(_sweep_stale_realtime_calls_async())


async def _sweep_stale_realtime_calls_async(
    limit: int = 500,
    *,
    now: datetime | None = None,
) -> int:
    from sqlalchemy import and_, func, or_, select

    from app.core.database import async_session_factory
    from app.models.agent import Agent
    from app.models.call import Call

    bounded_limit = min(max(int(limit), 1), 1000)
    observed_at = _as_utc(now) or datetime.now(UTC)
    expired_browser_rooms: list[str] = []
    async with async_session_factory() as db:
        metadata = Call.call_metadata
        runtime = metadata["runtime"]
        runtime_route = metadata["runtime_route"]
        route_filter = or_(
            and_(
                Call.provider == "twilio",
                metadata["conversation_type"].as_string() == "telephonyInbound",
                metadata["channel"].as_string() == "phone",
            ),
            and_(
                Call.provider == "livekit_sip",
                runtime["transport"].as_string() == "livekit_sip",
            ),
            and_(
                Call.provider == "livekit_webrtc",
                metadata["conversation_type"].as_string() == "webcall",
                metadata["channel"].as_string() == "browser",
                runtime["transport"].as_string() == "livekit_webrtc",
            ),
        )
        speech_provider = func.coalesce(
            runtime["speech_provider"].as_string(),
            runtime_route["speech_provider"].as_string(),
            metadata["speech_provider"].as_string(),
        )
        rows = (
            await db.execute(
                select(Call, Agent.max_call_duration_seconds)
                .outerjoin(
                    Agent,
                    (Agent.id == Call.agent_id) & (Agent.tenant_id == Call.tenant_id),
                )
                .where(
                    Call.direction == "inbound",
                    Call.provider.in_(("twilio", "livekit_sip", "livekit_webrtc")),
                    Call.status.in_(("initiated", "in_progress")),
                    route_filter,
                    speech_provider.in_(tuple(REALTIME_CALL_WATCHDOG_PROVIDERS)),
                )
                # Oldest reservation first prevents a large set of connected
                # calls (whose answered_at is non-null) from starving expired
                # browser rows on databases that sort NULL timestamps last.
                .order_by(Call.created_at)
                .limit(bounded_limit)
                .with_for_update(skip_locked=True, of=Call)
            )
        ).all()

        recovered = 0
        for call, max_duration in rows:
            metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
            runtime_route = metadata.get("runtime_route")
            runtime = metadata.get("runtime")
            speech_provider = metadata.get("speech_provider")
            if isinstance(runtime_route, dict):
                speech_provider = runtime_route.get("speech_provider") or speech_provider
            if isinstance(runtime, dict):
                speech_provider = runtime.get("speech_provider") or speech_provider
            is_twilio_realtime = (
                call.provider == "twilio"
                and metadata.get("conversation_type") == "telephonyInbound"
                and metadata.get("channel") == "phone"
            )
            is_livekit_realtime = (
                call.provider == "livekit_sip"
                and isinstance(runtime, dict)
                and runtime.get("transport") == "livekit_sip"
            )
            is_livekit_browser = (
                call.provider == "livekit_webrtc"
                and metadata.get("conversation_type") == "webcall"
                and metadata.get("channel") == "browser"
                and isinstance(runtime, dict)
                and runtime.get("transport") == "livekit_webrtc"
            )
            if not (
                (is_twilio_realtime or is_livekit_realtime or is_livekit_browser)
                and speech_provider in REALTIME_CALL_WATCHDOG_PROVIDERS
            ):
                continue
            if is_livekit_browser and call.status == "initiated":
                issued_at = _as_utc(call.created_at)
                if issued_at is None:
                    # TimestampMixin makes this unreachable for normal rows,
                    # but an unreadable legacy row must still fail closed.
                    issued_at = observed_at - timedelta(
                        seconds=(BROWSER_TOKEN_TTL_SECONDS + DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS)
                    )
                fallback_deadline = issued_at + timedelta(seconds=BROWSER_TOKEN_TTL_SECONDS)
                join_expires_at = metadata.get("join_expires_at")
                token_deadline = None
                if isinstance(join_expires_at, str):
                    try:
                        token_deadline = _as_utc(
                            datetime.fromisoformat(join_expires_at.replace("Z", "+00:00"))
                        )
                    except ValueError:
                        token_deadline = None
                # Metadata is durable server state, but corruption or a future
                # migration must never be able to reserve capacity forever.
                # Permit only small timestamp skew beyond the shared token TTL.
                if token_deadline is None or token_deadline > (
                    fallback_deadline + timedelta(seconds=30)
                ):
                    token_deadline = fallback_deadline
                if (
                    token_deadline + timedelta(seconds=DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS)
                    > observed_at
                ):
                    continue
                _mark_browser_join_timeout(call, observed_at)
                if call.provider_call_sid:
                    expired_browser_rooms.append(str(call.provider_call_sid))
                recovered += 1
                continue
            if call.status != "in_progress":
                continue
            answered_at = _as_utc(call.answered_at) or _as_utc(call.started_at)
            if answered_at is None:
                continue
            watchdog_duration = (
                _browser_watchdog_duration(metadata, max_duration)
                if is_livekit_browser
                else _bounded_call_duration(max_duration)
            )
            deadline = answered_at + timedelta(
                seconds=(watchdog_duration + DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS)
            )
            if deadline > observed_at:
                continue
            _mark_realtime_call_terminal_unknown(call, observed_at)
            if is_livekit_browser and call.provider_call_sid:
                expired_browser_rooms.append(str(call.provider_call_sid))
            recovered += 1

        await db.commit()
    await _cleanup_expired_browser_rooms(expired_browser_rooms)
    return recovered


async def _process_completed_call_async(
    call_id: str,
    tenant_id: str,
    *,
    force_analysis: bool = False,
):
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.core.database import async_session_factory
    from app.models.agent import Agent
    from app.models.billing import UsageRecord
    from app.models.call import Call, CallSummary, CallTranscript

    call_uuid = uuid.UUID(call_id)
    tenant_uuid = uuid.UUID(tenant_id)
    transcript_text: str | None = None
    should_generate_summary = False
    has_substantive_caller_input = False
    openai_api_key: str | None = None
    disposition_profile = "general"
    post_call_analysis_mode = "provider_first"
    agent_goal: str | None = None
    existing_disposition: str | None = None
    provider_analysis_data: dict | None = None
    grounding_summary: dict[str, int] = {}

    async with async_session_factory() as db:
        from app.services.provider_credentials import load_provider_config
        from app.services.usage_ledger import (
            lock_agent_runtime_limits,
            metered_call_cost_cents,
        )

        openai_config = await load_provider_config(db, tenant_uuid, "openai")
        openai_api_key = str((openai_config or {}).get("api_key") or "").strip() or None
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
        existing_disposition = call.disposition
        grounding_summary = summarize_runtime_grounding(call.call_metadata)

        agent = (
            await db.scalar(
                select(Agent).where(
                    Agent.id == call.agent_id,
                    Agent.tenant_id == tenant_uuid,
                )
            )
            if call.agent_id
            else None
        )
        disposition_profile = infer_disposition_profile(agent)
        if agent is not None:
            agent_goal = str(agent.description or agent.system_prompt or "")[:1200]
            post_call_analysis_mode = str(
                getattr(agent, "post_call_analysis_mode", "provider_first") or "provider_first"
            )

        transcript_result = await db.execute(
            select(CallTranscript).where(
                CallTranscript.call_id == call_uuid,
                CallTranscript.tenant_id == tenant_uuid,
            )
        )
        transcript = transcript_result.scalar_one_or_none()
        if transcript is not None:
            transcript_text = str(transcript.full_text or "").strip()
            if not transcript_text and isinstance(transcript.turns, list):
                transcript_text = "\n".join(
                    f"{str(turn.get('role') or 'unknown').title()}: "
                    f"{str(turn.get('content') or '').strip()}"
                    for turn in transcript.turns
                    if isinstance(turn, dict) and str(turn.get("content") or "").strip()
                )
            has_substantive_caller_input = _has_substantive_caller_input(transcript.turns)
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
            needs_outcome = summary is None or not bool(summary.disposition_details)
            if force_analysis:
                should_generate_summary = True
            elif post_call_analysis_mode == "disabled":
                should_generate_summary = False
            elif (
                post_call_analysis_mode == "provider_first"
                and has_substantive_caller_input
                and needs_outcome
                and has_provider_analytics
            ):
                provider_analysis_data = normalize_provider_call_analysis(
                    call.call_metadata.get("smallest_analytics"),
                    profile=disposition_profile,
                )
                should_generate_summary = provider_analysis_data is None
            else:
                should_generate_summary = needs_outcome

        # Record usage
        if call.duration_seconds and call.duration_seconds > 0:
            now = datetime.now(UTC)
            if call.agent_id is not None:
                await lock_agent_runtime_limits(
                    db,
                    tenant_id=tenant_uuid,
                    agent_id=call.agent_id,
                )
            usage_result = await db.execute(
                select(UsageRecord).where(
                    UsageRecord.tenant_id == tenant_uuid,
                    UsageRecord.call_id == call_uuid,
                    UsageRecord.usage_type == "call_minutes",
                )
            )
            usage = usage_result.scalar_one_or_none()
            quantity = call.duration_seconds / 60.0
            cost_cents = metered_call_cost_cents(call.duration_seconds)
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

    summary_data = provider_analysis_data
    if should_generate_summary:
        if not has_substantive_caller_input:
            # Agent greetings and prompts are not evidence of caller intent.
            # Bypass the model so it cannot turn a one-sided transcript into a
            # fabricated disposition, contact detail, or follow-up action.
            summary_data = _insufficient_transcript_summary(existing_disposition)
        elif transcript_text:
            try:
                from app.ai.conversation import ConversationEngine, conversation_engine

                # Deliberately outside every database transaction/row lock.
                engine = (
                    ConversationEngine(api_key=openai_api_key)
                    if openai_api_key
                    else conversation_engine
                )
                summary_data = await engine.generate_call_summary(
                    transcript_text,
                    disposition_profile=disposition_profile,
                    allowed_dispositions=disposition_catalog(disposition_profile),
                    agent_goal=agent_goal,
                )
                if isinstance(summary_data, dict):
                    summary_data["analysis_source"] = "vav_ai"
            except Exception as exc:
                # Billing and lifecycle delivery should still complete when an
                # optional LLM summary provider is unavailable.
                logger.error(
                    "summary_generation_failed",
                    call_id=call_id,
                    error_type=type(exc).__name__,
                )
                summary_data = _analysis_unavailable_summary(existing_disposition)

    if summary_data is not None:
        if not isinstance(summary_data.get("disposition_details"), dict):
            summary_data = normalize_call_analysis(summary_data, profile=disposition_profile)
        summary_data = apply_grounding_quality_guard(
            summary_data,
            grounding=grounding_summary,
        )
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
                if summary is None:
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
                                    disposition_details=summary_data.get("disposition_details"),
                                )
                            )
                            await db.flush()
                    except IntegrityError:
                        # A provider analytics callback won the conditional
                        # insert; its authoritative summary is preserved.
                        summary = await db.scalar(
                            select(CallSummary).where(
                                CallSummary.call_id == call_uuid,
                                CallSummary.tenant_id == tenant_uuid,
                            )
                        )
                        if summary is not None and not summary.disposition_details:
                            summary.disposition_details = summary_data["disposition_details"]
                            call.disposition = summary_data["disposition"]
                    else:
                        call.disposition = summary_data["disposition"]
                elif force_analysis or not summary.disposition_details:
                    # Provider summaries remain authoritative, while VAV adds a
                    # provider-neutral outcome contract for consistent reporting.
                    if not has_provider_analytics:
                        summary.summary = summary_data["summary"]
                        summary.key_topics = summary_data["key_topics"]
                        summary.action_items = summary_data["action_items"]
                        summary.sentiment = summary_data["sentiment"]
                    summary.disposition_details = summary_data["disposition_details"]
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
        "disposition_details": (
            call.summary.disposition_details if call.summary is not None else None
        ),
        "direction": call.direction,
    }
