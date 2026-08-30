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

    wrong_tenant_payload = await _process_completed_call_async(str(call.id), str(uuid4()))
    assert wrong_tenant_payload is None


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

    async def provider_analytics_arrives(_transcript):
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
    assert stored_call.disposition is None


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
