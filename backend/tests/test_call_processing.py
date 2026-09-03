"""Idempotency and tenant-boundary tests for post-call processing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.ai.conversation import conversation_engine
from app.core import database
from app.models.agent import Agent
from app.models.billing import UsageRecord
from app.models.call import Call, CallSummary, CallTranscript
from app.models.campaign import Campaign
from app.tasks.call_tasks import (
    DIRECT_CALL_UNKNOWN_STATUS,
    DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS,
    _process_completed_call_async,
    _reconcile_call_dispatch_async,
    _reconcile_direct_call_terminal_async,
    _sweep_stale_call_dispatches_async,
    _sweep_stale_direct_calls_async,
    _sweep_stale_realtime_calls_async,
    arm_direct_call_terminal_watchdog,
)
from tests.conftest import test_session_factory as session_factory


@pytest.mark.asyncio
async def test_completed_call_processing_is_tenant_scoped_and_idempotent(
    db,
    tenant,
    monkeypatch,
):
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="completed",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
        duration_seconds=120,
    )
    db.add(call)
    await db.flush()
    db.add(
        CallTranscript(
            tenant_id=tenant.id,
            call_id=call.id,
            turns=[{"role": "user", "content": "Please call me tomorrow."}],
            full_text="User: Please call me tomorrow.",
        )
    )
    await db.commit()

    summarize = AsyncMock(
        return_value={
            "summary": "The customer requested a callback.",
            "key_topics": ["callback"],
            "action_items": ["Call tomorrow"],
            "sentiment": "neutral",
            "disposition": "callback",
        }
    )
    monkeypatch.setattr(conversation_engine, "generate_call_summary", summarize)
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    first_payload = await _process_completed_call_async(str(call.id), str(tenant.id))
    second_payload = await _process_completed_call_async(str(call.id), str(tenant.id))

    assert first_payload == second_payload
    assert first_payload["call_id"] == str(call.id)
    assert summarize.await_count == 1

    async with session_factory() as session:
        summary_count = await session.scalar(
            select(func.count()).select_from(CallSummary).where(CallSummary.call_id == call.id)
        )
        usage_count = await session.scalar(
            select(func.count())
            .select_from(UsageRecord)
            .where(
                UsageRecord.call_id == call.id,
                UsageRecord.usage_type == "call_minutes",
            )
        )
        usage = await session.scalar(
            select(UsageRecord).where(
                UsageRecord.call_id == call.id,
                UsageRecord.usage_type == "call_minutes",
            )
        )
        processed_call = await session.get(Call, call.id)

    assert summary_count == 1
    assert usage_count == 1
    assert usage is not None
    assert usage.quantity == 2.0
    assert processed_call is not None
    assert processed_call.disposition == "callback"
    assert processed_call.summary.disposition_details["primary"] == "callback"

    wrong_tenant_payload = await _process_completed_call_async(str(call.id), str(uuid4()))
    assert wrong_tenant_payload is None


@pytest.mark.parametrize(
    ("turns", "full_text"),
    [
        (
            [
                {
                    "role": "assistant",
                    "content": "Hello, how may I help you today?",
                },
                {"role": "user", "content": "   "},
            ],
            "Assistant: Hello, how may I help you today?",
        ),
        ([], ""),
    ],
)
@pytest.mark.asyncio
async def test_assistant_only_transcript_gets_evidence_safe_unknown_outcome(
    db,
    tenant,
    monkeypatch,
    turns,
    full_text,
):
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="inbound",
        status="completed",
        from_number="browser",
        to_number="voice-agent",
        provider="livekit_webrtc",
        duration_seconds=3,
    )
    db.add(call)
    await db.flush()
    db.add(
        CallTranscript(
            tenant_id=tenant.id,
            call_id=call.id,
            turns=turns,
            full_text=full_text,
        )
    )
    await db.commit()

    summarize = AsyncMock(
        return_value={
            "summary": "The caller requested an email.",
            "key_topics": ["email"],
            "action_items": ["Send an email"],
            "sentiment": "positive",
            "disposition": "interested",
        }
    )
    monkeypatch.setattr(conversation_engine, "generate_call_summary", summarize)
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    payload = await _process_completed_call_async(str(call.id), str(tenant.id))

    async with session_factory() as session:
        summary = await session.scalar(select(CallSummary).where(CallSummary.call_id == call.id))
        processed_call = await session.get(Call, call.id)

    summarize.assert_not_awaited()
    assert payload["disposition"] == "unknown"
    assert processed_call.disposition == "unknown"
    assert summary.summary == (
        "No substantive caller speech was captured, so no call outcome or follow-up "
        "could be determined."
    )
    assert summary.key_topics == []
    assert summary.action_items == []
    assert summary.sentiment == "neutral"
    assert summary.disposition_details["needs_review"] is True


@pytest.mark.asyncio
async def test_provider_analytics_wins_while_local_summary_runs_without_db_lock(
    db,
    tenant,
    monkeypatch,
):
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="completed",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
        duration_seconds=30,
        call_metadata={},
    )
    db.add(call)
    await db.flush()
    db.add(
        CallTranscript(
            tenant_id=tenant.id,
            call_id=call.id,
            turns=[{"role": "user", "content": "Provider summary should win."}],
            full_text="User: Provider summary should win.",
        )
    )
    await db.commit()
    call_id = call.id
    tenant_id = tenant.id

    async def provider_analytics_arrives(_transcript, **_kwargs):
        # This separate transaction must be able to commit while the external
        # summary await is in flight.
        async with session_factory() as callback_db:
            callback_call = await callback_db.get(Call, call_id)
            callback_call.call_metadata = {"smallest_analytics": {"summary": "Provider"}}
            callback_db.add(
                CallSummary(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    summary="Authoritative provider summary",
                    key_topics=["provider"],
                    action_items=[],
                    sentiment="positive",
                )
            )
            await callback_db.commit()
        return {
            "summary": "Late local summary",
            "key_topics": ["local"],
            "action_items": [],
            "sentiment": "neutral",
            "disposition": "local_disposition",
        }

    summarize = AsyncMock(side_effect=provider_analytics_arrives)
    monkeypatch.setattr(conversation_engine, "generate_call_summary", summarize)
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    await _process_completed_call_async(str(call_id), str(tenant_id))

    async with session_factory() as session:
        summary = await session.scalar(select(CallSummary).where(CallSummary.call_id == call_id))
        stored_call = await session.get(Call, call_id)
    assert summarize.await_count == 1
    assert summary.summary == "Authoritative provider summary"
    assert stored_call.disposition == "unknown"
    assert summary.disposition_details["needs_review"] is True


@pytest.mark.asyncio
async def test_provider_first_normalizes_sufficient_analytics_without_openai(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Provider-first receptionist",
        system_prompt="Welcome callers and answer approved questions.",
        voice_provider="smallest",
        disposition_profile="receptionist",
        post_call_analysis_mode="provider_first",
    )
    db.add(agent)
    await db.flush()
    analytics = {
        "summary": "The caller received the office number.",
        "keyTopics": ["contact details"],
        "actionItems": [],
        "sentiment": "positive",
        "dispositionMetrics": [{"value": "answered", "confidence": 0.94}],
    }
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="completed",
        from_number="browser",
        to_number="voice-agent",
        provider="smallest",
        duration_seconds=30,
        call_metadata={"smallest_analytics": analytics},
    )
    db.add(call)
    await db.flush()
    db.add_all(
        [
            CallTranscript(
                tenant_id=tenant.id,
                call_id=call.id,
                turns=[{"role": "user", "content": "What is the office number?"}],
                full_text="User: What is the office number?",
            ),
            CallSummary(
                tenant_id=tenant.id,
                call_id=call.id,
                summary=analytics["summary"],
                key_topics=analytics["keyTopics"],
                action_items=[],
                sentiment="positive",
            ),
        ]
    )
    await db.commit()

    summarize = AsyncMock()
    monkeypatch.setattr(conversation_engine, "generate_call_summary", summarize)
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    await _process_completed_call_async(str(call.id), str(tenant.id))

    async with session_factory() as session:
        stored_call = await session.get(Call, call.id)
        summary = await session.scalar(select(CallSummary).where(CallSummary.call_id == call.id))
    summarize.assert_not_awaited()
    assert stored_call.disposition == "information_provided"
    assert summary.summary == analytics["summary"]
    assert summary.disposition_details["analysis_source"] == "provider_analytics"


@pytest.mark.asyncio
async def test_vav_ai_mode_analyzes_even_when_provider_analytics_exist(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="VAV AI receptionist",
        system_prompt="Welcome callers and answer approved questions.",
        voice_provider="smallest",
        disposition_profile="receptionist",
        post_call_analysis_mode="vav_ai",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="completed",
        from_number="browser",
        to_number="voice-agent",
        provider="smallest",
        duration_seconds=30,
        call_metadata={
            "smallest_analytics": {
                "summary": "Provider summary",
                "dispositionMetrics": [{"value": "answered"}],
            },
            "runtime": {
                "turn_diagnostics": [{"grounding_outcome": "no_match_unverified_response"}]
            },
        },
    )
    db.add(call)
    await db.flush()
    db.add_all(
        [
            CallTranscript(
                tenant_id=tenant.id,
                call_id=call.id,
                turns=[{"role": "user", "content": "Please call me back."}],
                full_text="User: Please call me back.",
            ),
            CallSummary(
                tenant_id=tenant.id,
                call_id=call.id,
                summary="Provider summary",
                key_topics=[],
                action_items=[],
                sentiment="neutral",
            ),
        ]
    )
    await db.commit()

    summarize = AsyncMock(
        return_value={
            "summary": "The caller requested a callback.",
            "disposition": "callback",
            "resolution": "unresolved",
            "confidence": 0.9,
            "follow_up": {"required": True, "action": "Return the call"},
        }
    )
    monkeypatch.setattr(conversation_engine, "generate_call_summary", summarize)
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    await _process_completed_call_async(str(call.id), str(tenant.id))

    async with session_factory() as session:
        stored_call = await session.get(Call, call.id)
        summary = await session.scalar(select(CallSummary).where(CallSummary.call_id == call.id))
    summarize.assert_awaited_once()
    assert stored_call.disposition == "callback"
    assert summary.summary == "Provider summary"
    assert summary.disposition_details["analysis_source"] == "vav_ai"
    assert summary.disposition_details["resolution"] == "unresolved"
    assert summary.disposition_details["needs_review"] is True
    assert summary.disposition_details["grounding"]["no_match_unverified_response"] == 1


@pytest.mark.asyncio
async def test_stale_dispatch_claim_becomes_visible_unknown_state(
    db,
    tenant,
    monkeypatch,
):
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="dispatching",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
        call_metadata={"request": {}},
    )
    db.add(call)
    await db.commit()
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    result = await _reconcile_call_dispatch_async(str(call.id), str(tenant.id))
    assert result == "dispatch_unknown"

    async with session_factory() as session:
        reconciled = await session.get(Call, call.id)
    assert reconciled is not None
    assert reconciled.status == "dispatch_unknown"
    assert reconciled.call_metadata["dispatch_error"] == "provider_result_unknown"


@pytest.mark.asyncio
async def test_periodic_sweeper_recovers_claim_enqueue_crash(
    db,
    tenant,
    monkeypatch,
):
    stale = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="dispatching",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    fresh = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="dispatching",
        from_number="provider-managed",
        to_number="+971501234568",
        provider="smallest",
    )
    db.add_all([stale, fresh])
    await db.commit()
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    assert await _sweep_stale_call_dispatches_async() == 1
    async with session_factory() as session:
        stale_result = await session.get(Call, stale.id)
        fresh_result = await session.get(Call, fresh.id)
    assert stale_result.status == "dispatch_unknown"
    assert fresh_result.status == "dispatching"


@pytest.mark.asyncio
async def test_direct_call_watchdog_marks_lost_terminal_for_operator_review(
    db,
    tenant,
    monkeypatch,
):
    accepted_at = datetime.now(UTC) - timedelta(
        seconds=30 + DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS + 1
    )
    agent = Agent(
        tenant_id=tenant.id,
        name="Direct watchdog agent",
        system_prompt="Keep accepted calls bounded.",
        max_call_duration_seconds=30,
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="ringing",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
        provider_call_sid="accepted-direct-call",
        started_at=accepted_at,
        call_metadata={"request": {"idempotency_key": "never-redial"}},
    )
    db.add(call)
    await db.flush()
    deadline = arm_direct_call_terminal_watchdog(call, agent.max_call_duration_seconds)
    completed_before_watchdog = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="ringing",
        from_number="provider-managed",
        to_number="+971501234568",
        provider="smallest",
        provider_call_sid="terminal-callback-won",
        started_at=accepted_at,
        call_metadata={},
    )
    db.add(completed_before_watchdog)
    await db.flush()
    arm_direct_call_terminal_watchdog(
        completed_before_watchdog,
        agent.max_call_duration_seconds,
    )
    completed_before_watchdog.status = "completed"
    completed_before_watchdog.ended_at = accepted_at + timedelta(seconds=10)
    # The deadline is acceptance-time policy. Later agent edits must not extend
    # an already accepted provider call indefinitely.
    agent.max_call_duration_seconds = 7200
    await db.commit()
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    result = await _reconcile_direct_call_terminal_async(
        str(call.id),
        str(tenant.id),
        now=deadline + timedelta(seconds=1),
    )

    assert result == DIRECT_CALL_UNKNOWN_STATUS
    async with session_factory() as session:
        reconciled = await session.get(Call, call.id)
    assert reconciled.status == "terminal_unknown"
    assert reconciled.provider_call_sid == "accepted-direct-call"
    assert reconciled.ended_at is None
    assert reconciled.call_metadata["lifecycle_error"] == "terminal_callback_timeout"
    assert reconciled.call_metadata["operator_review_required"] is True
    assert reconciled.call_metadata["automatic_redial_disabled"] is True
    assert reconciled.call_metadata["terminal_watchdog"]["status"] == "operator_review"

    duplicate = await _reconcile_direct_call_terminal_async(
        str(call.id),
        str(tenant.id),
        now=deadline + timedelta(minutes=1),
    )
    assert duplicate == "resolved"

    terminal_result = await _reconcile_direct_call_terminal_async(
        str(completed_before_watchdog.id),
        str(tenant.id),
        now=deadline + timedelta(minutes=1),
    )
    assert terminal_result == "resolved"
    async with session_factory() as session:
        terminal_call = await session.get(Call, completed_before_watchdog.id)
    assert terminal_call.status == "completed"
    assert terminal_call.provider_call_sid == "terminal-callback-won"
    assert terminal_call.call_metadata["terminal_watchdog"]["status"] == "armed"
    assert "operator_review_required" not in terminal_call.call_metadata


@pytest.mark.asyncio
async def test_direct_call_watchdog_sweep_is_recovery_only_and_never_touches_campaigns(
    db,
    tenant,
    monkeypatch,
):
    now = datetime.now(UTC)
    stale_started_at = now - timedelta(seconds=30 + DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS + 1)
    agent = Agent(
        tenant_id=tenant.id,
        name="Sweep watchdog agent",
        system_prompt="Bound recovery scans to direct accepted calls.",
        max_call_duration_seconds=30,
    )
    db.add(agent)
    await db.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        agent_id=agent.id,
        name="Campaign watchdog exclusion",
    )
    db.add(campaign)
    await db.flush()

    def call(status, sid, **overrides):
        values = {
            "tenant_id": tenant.id,
            "agent_id": agent.id,
            "campaign_id": None,
            "direction": "outbound",
            "status": status,
            "from_number": "provider-managed",
            "to_number": "+971501234567",
            "provider": "smallest",
            "provider_call_sid": sid,
            "started_at": stale_started_at,
        }
        values.update(overrides)
        return Call(**values)

    stale_ringing = call("ringing", "stale-ringing")
    stale_in_progress = call("in_progress", "stale-in-progress")
    fresh_ringing = call(
        "ringing",
        "fresh-ringing",
        started_at=now - timedelta(seconds=10),
    )
    campaign_ringing = call("ringing", "campaign-ringing", campaign_id=campaign.id)
    inbound_ringing = call("ringing", "inbound-ringing", direction="inbound")
    sidless_ringing = call("ringing", None)
    dispatching = call("dispatching", "dispatching-direct")
    completed = call("completed", "completed-direct")
    calls = [
        stale_ringing,
        stale_in_progress,
        fresh_ringing,
        campaign_ringing,
        inbound_ringing,
        sidless_ringing,
        dispatching,
        completed,
    ]
    db.add_all(calls)
    await db.commit()
    call_ids = {item.provider_call_sid: item.id for item in calls}
    monkeypatch.setattr(database, "async_session_factory", session_factory)

    assert await _sweep_stale_direct_calls_async(now=now) == 2

    async with session_factory() as session:
        statuses = {
            sid: (await session.get(Call, call_id)).status for sid, call_id in call_ids.items()
        }
    assert statuses["stale-ringing"] == "terminal_unknown"
    assert statuses["stale-in-progress"] == "terminal_unknown"
    assert statuses["fresh-ringing"] == "ringing"
    assert statuses["campaign-ringing"] == "ringing"
    assert statuses["inbound-ringing"] == "ringing"
    assert statuses[None] == "ringing"
    assert statuses["dispatching-direct"] == "dispatching"
    assert statuses["completed-direct"] == "completed"


@pytest.mark.asyncio
async def test_realtime_watchdog_recovers_only_abandoned_inbound_media_sessions(
    db,
    tenant,
    monkeypatch,
):
    now = datetime.now(UTC)
    agent = Agent(
        tenant_id=tenant.id,
        name="Inbound recovery agent",
        system_prompt="Recover abandoned realtime sessions safely.",
        max_call_duration_seconds=30,
    )
    edited_duration_agent = Agent(
        tenant_id=tenant.id,
        name="Edited duration recovery agent",
        system_prompt="Honor the duration reserved by each browser call.",
        max_call_duration_seconds=7200,
    )
    db.add_all([agent, edited_duration_agent])
    await db.flush()

    def inbound_call(name: str, **overrides):
        values = {
            "tenant_id": tenant.id,
            "agent_id": agent.id,
            "direction": "inbound",
            "status": "in_progress",
            "from_number": "+971501234567",
            "to_number": "+14142934703",
            "provider": "twilio",
            "provider_call_sid": name,
            "started_at": now - timedelta(minutes=10),
            "answered_at": now - timedelta(minutes=10),
            "call_metadata": {
                "conversation_type": "telephonyInbound",
                "channel": "phone",
                "speech_provider": "elevenlabs",
            },
        }
        values.update(overrides)
        return Call(**values)

    stale = inbound_call("stale-runtime")
    stale_livekit = inbound_call(
        "stale-livekit-runtime",
        provider="livekit_sip",
        call_metadata={"runtime": {"transport": "livekit_sip", "speech_provider": "inworld"}},
    )
    stale_browser = inbound_call(
        "stale-livekit-browser",
        provider="livekit_webrtc",
        status="initiated",
        started_at=None,
        answered_at=None,
        from_number="browser",
        to_number="voice-agent",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "join_expires_at": (now - timedelta(minutes=5)).isoformat(),
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
    )
    edited_duration_browser = inbound_call(
        "edited-duration-livekit-browser",
        agent_id=edited_duration_agent.id,
        provider="livekit_webrtc",
        status="in_progress",
        started_at=now - timedelta(minutes=3),
        answered_at=now - timedelta(minutes=3),
        from_number="browser",
        to_number="voice-agent",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "reserved_max_duration_seconds": 30,
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
    )
    fresh_browser = inbound_call(
        "fresh-livekit-browser",
        provider="livekit_webrtc",
        status="initiated",
        started_at=None,
        answered_at=None,
        from_number="browser",
        to_number="voice-agent",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "join_expires_at": (now + timedelta(minutes=1)).isoformat(),
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
    )
    missing_expiry_browser = inbound_call(
        "missing-expiry-livekit-browser",
        provider="livekit_webrtc",
        status="initiated",
        started_at=None,
        answered_at=None,
        created_at=now - timedelta(minutes=10),
        from_number="browser",
        to_number="voice-agent",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
    )
    malformed_expiry_browser = inbound_call(
        "malformed-expiry-livekit-browser",
        provider="livekit_webrtc",
        status="initiated",
        started_at=None,
        answered_at=None,
        created_at=now - timedelta(minutes=10),
        from_number="browser",
        to_number="voice-agent",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "join_expires_at": "not-a-timestamp",
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
    )
    future_expiry_browser = inbound_call(
        "future-expiry-livekit-browser",
        provider="livekit_webrtc",
        status="initiated",
        started_at=None,
        answered_at=None,
        created_at=now - timedelta(minutes=10),
        from_number="browser",
        to_number="voice-agent",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "join_expires_at": (now + timedelta(days=30)).isoformat(),
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
    )
    fresh = inbound_call(
        "fresh-runtime",
        started_at=now - timedelta(seconds=10),
        answered_at=now - timedelta(seconds=10),
    )
    non_runtime = inbound_call(
        "non-runtime",
        call_metadata={"conversation_type": "telephonyInbound", "channel": "phone"},
    )
    completed = inbound_call("completed-runtime", status="completed")
    db.add_all(
        [
            stale,
            stale_livekit,
            stale_browser,
            edited_duration_browser,
            fresh_browser,
            missing_expiry_browser,
            malformed_expiry_browser,
            future_expiry_browser,
            fresh,
            non_runtime,
            completed,
        ]
    )
    await db.commit()
    ids = {
        call.provider_call_sid: call.id
        for call in (
            stale,
            stale_livekit,
            stale_browser,
            edited_duration_browser,
            fresh_browser,
            missing_expiry_browser,
            malformed_expiry_browser,
            future_expiry_browser,
            fresh,
            non_runtime,
            completed,
        )
    }
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    delete_room = AsyncMock(return_value=True)
    monkeypatch.setattr("app.tasks.call_tasks.delete_browser_room", delete_room)

    assert await _sweep_stale_realtime_calls_async(now=now) == 7

    async with session_factory() as session:
        recovered = await session.get(Call, ids["stale-runtime"])
        recovered_livekit = await session.get(Call, ids["stale-livekit-runtime"])
        recovered_browser = await session.get(Call, ids["stale-livekit-browser"])
        edited_duration_result = await session.get(Call, ids["edited-duration-livekit-browser"])
        fresh_browser_result = await session.get(Call, ids["fresh-livekit-browser"])
        missing_expiry_result = await session.get(Call, ids["missing-expiry-livekit-browser"])
        malformed_expiry_result = await session.get(Call, ids["malformed-expiry-livekit-browser"])
        future_expiry_result = await session.get(Call, ids["future-expiry-livekit-browser"])
        fresh_result = await session.get(Call, ids["fresh-runtime"])
        non_runtime_result = await session.get(Call, ids["non-runtime"])
        completed_result = await session.get(Call, ids["completed-runtime"])
    assert recovered.status == "terminal_unknown"
    assert recovered.ended_at.replace(tzinfo=UTC) == now
    assert recovered.call_metadata["lifecycle_error"] == "realtime_session_timeout"
    assert recovered.call_metadata["operator_review_required"] is True
    assert recovered_livekit.status == "terminal_unknown"
    assert recovered_livekit.call_metadata["lifecycle_error"] == "realtime_session_timeout"
    assert recovered_browser.status == "failed"
    assert recovered_browser.answered_at is None
    assert recovered_browser.duration_seconds == 0
    assert recovered_browser.call_metadata["lifecycle_error"] == ("livekit_browser_join_timeout")
    assert recovered_browser.call_metadata["operator_review_required"] is False
    assert edited_duration_result.status == "terminal_unknown"
    assert fresh_browser_result.status == "initiated"
    assert missing_expiry_result.status == "failed"
    assert malformed_expiry_result.status == "failed"
    assert future_expiry_result.status == "failed"
    assert fresh_result.status == "in_progress"
    assert non_runtime_result.status == "in_progress"
    assert completed_result.status == "completed"
    assert delete_room.await_count == 5


@pytest.mark.asyncio
async def test_realtime_watchdog_handles_an_empty_candidate_set(db, monkeypatch):
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    delete_room = AsyncMock(return_value=True)
    monkeypatch.setattr("app.tasks.call_tasks.delete_browser_room", delete_room)

    assert await _sweep_stale_realtime_calls_async() == 0
    delete_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_realtime_watchdog_invalid_old_rows_cannot_starve_browser_recovery(
    db,
    tenant,
    monkeypatch,
):
    now = datetime.now(UTC)
    agent = Agent(
        tenant_id=tenant.id,
        name="Watchdog route filtering",
        system_prompt="Recover only governed realtime routes.",
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    invalid_calls = [
        Call(
            tenant_id=tenant.id,
            agent_id=agent.id,
            direction="inbound",
            status="initiated",
            from_number="browser",
            to_number="voice-agent",
            provider="livekit_webrtc",
            provider_call_sid=f"invalid-old-browser-{index}",
            call_metadata={},
            created_at=now - timedelta(minutes=20),
        )
        for index in range(500)
    ]
    expired = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="initiated",
        from_number="browser",
        to_number="voice-agent",
        provider="livekit_webrtc",
        provider_call_sid="vav-browser-after-invalid-page",
        call_metadata={
            "conversation_type": "webcall",
            "channel": "browser",
            "join_expires_at": (now - timedelta(minutes=5)).isoformat(),
            "reserved_max_duration_seconds": 60,
            "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
        },
        created_at=now - timedelta(minutes=10),
    )
    db.add_all([*invalid_calls, expired])
    await db.commit()
    expired_id = expired.id
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    delete_room = AsyncMock(return_value=True)
    monkeypatch.setattr("app.tasks.call_tasks.delete_browser_room", delete_room)

    assert await _sweep_stale_realtime_calls_async(limit=500, now=now) == 1
    async with session_factory() as session:
        recovered = await session.get(Call, expired_id)
    assert recovered.status == "failed"
    delete_room.assert_awaited_once()
