"""Runtime monthly budget enforcement against the authoritative usage ledger."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.livekit_runtime import worker as livekit_worker
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.billing import UsageRecord
from app.models.call import Call
from app.services.provider_credentials import store_provider_config
from app.services.twilio_route_security import (
    TwilioRouteCredential,
    mark_twilio_route_verified,
)
from app.services.usage_ledger import (
    lock_agent_runtime_limits,
    monthly_agent_budget_commitment,
)
from tests.knowledge_test_utils import publish_test_knowledge


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_runtime_limit_lock_uses_one_month_independent_agent_key():
    tenant_id = uuid4()
    agent_id = uuid4()
    execute = AsyncMock()
    fake_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=execute,
    )

    await lock_agent_runtime_limits(fake_db, tenant_id=tenant_id, agent_id=agent_id)

    assert execute.await_count == 1
    assert execute.await_args.args[1] == {"lock_key": f"runtime-limits:{tenant_id}:{agent_id}"}


@pytest.mark.asyncio
async def test_inbound_capacity_lock_precedes_every_limit_read(monkeypatch):
    events: list[str] = []

    class FakeDB:
        async def scalar(self, _statement):
            events.append("count")
            return 0

    async def lock_limits(*_args, **_kwargs):
        events.append("lock")

    async def monthly_commitment(*_args, **_kwargs):
        events.append("budget")
        return SimpleNamespace(total_cents=0)

    monkeypatch.setattr(livekit_worker, "lock_agent_runtime_limits", lock_limits)
    monkeypatch.setattr(livekit_worker, "monthly_agent_budget_commitment", monthly_commitment)
    await livekit_worker._enforce_inbound_limits(
        FakeDB(),
        model=SimpleNamespace(
            tenant_id=uuid4(),
            id=uuid4(),
            max_call_duration_seconds=60,
        ),
        profile=SimpleNamespace(
            daily_call_limit=100,
            max_concurrent_calls=2,
            monthly_budget_cents=1000,
        ),
    )

    assert events == ["lock", "count", "count", "budget"]


@pytest.mark.asyncio
async def test_monthly_commitment_uses_usage_ledger_not_stale_call_cost(db, tenant):
    now = datetime.now(UTC)
    agent = Agent(
        tenant_id=tenant.id,
        name="Ledger authority",
        system_prompt="Keep budget accounting authoritative.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        max_call_duration_seconds=30,
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="completed",
        from_number="+97141234567",
        to_number="+971501234567",
        provider="twilio",
        duration_seconds=60,
        cost_cents=9999,
        created_at=now,
    )
    db.add(call)
    await db.flush()
    db.add(
        UsageRecord(
            tenant_id=tenant.id,
            call_id=call.id,
            usage_type="call_minutes",
            quantity=1,
            unit="minutes",
            cost_cents=7,
            period_start=now,
            period_end=now,
        )
    )
    await db.commit()

    commitment = await monthly_agent_budget_commitment(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        month_start=_month_start(now),
        max_call_duration_seconds=agent.max_call_duration_seconds,
        include_prospective_call=False,
    )

    assert commitment.ledger_cents == 7
    assert commitment.unprocessed_reservation_cents == 0
    assert commitment.total_cents == 7


@pytest.mark.asyncio
async def test_browser_budget_uses_reserved_cap_and_ignores_known_join_timeout(db, tenant):
    now = datetime.now(UTC)
    agent = Agent(
        tenant_id=tenant.id,
        name="Browser reservation accounting",
        system_prompt="Keep each browser call within its reserved budget.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        max_call_duration_seconds=7200,
    )
    db.add(agent)
    await db.flush()
    common_metadata = {
        "conversation_type": "webcall",
        "channel": "browser",
        "reserved_max_duration_seconds": 30,
        "runtime": {"transport": "livekit_webrtc", "speech_provider": "inworld"},
    }
    db.add_all(
        [
            Call(
                tenant_id=tenant.id,
                agent_id=agent.id,
                direction="inbound",
                status="initiated",
                from_number="browser",
                to_number="voice-agent",
                provider="livekit_webrtc",
                provider_call_sid="vav-browser-active-budget",
                call_metadata=common_metadata,
                created_at=now,
            ),
            Call(
                tenant_id=tenant.id,
                agent_id=agent.id,
                direction="inbound",
                status="failed",
                from_number="browser",
                to_number="voice-agent",
                provider="livekit_webrtc",
                provider_call_sid="vav-browser-expired-budget",
                duration_seconds=0,
                ended_at=now,
                call_metadata={
                    **common_metadata,
                    "lifecycle_error": "livekit_browser_join_timeout",
                },
                created_at=now,
            ),
        ]
    )
    await db.commit()

    commitment = await monthly_agent_budget_commitment(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        month_start=_month_start(now),
        max_call_duration_seconds=agent.max_call_duration_seconds,
        include_prospective_call=False,
    )

    assert commitment.unprocessed_reservation_cents == 3
    assert commitment.total_cents == 3


@pytest.mark.asyncio
async def test_outbound_budget_guard_reserves_unprocessed_and_prospective_calls(
    client,
    auth_headers,
    db,
    tenant,
):
    now = datetime.now(UTC)
    agent = Agent(
        tenant_id=tenant.id,
        name="Budget-capped concierge",
        system_prompt="Do not exceed the configured monthly budget.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        max_call_duration_seconds=30,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider="sarvam",
        llm_provider="openai",
        assigned_numbers=["+97141234567"],
        max_concurrent_calls=5,
        daily_call_limit=100,
        monthly_budget_cents=100,
    )
    db.add(profile)
    twilio_credential = TwilioRouteCredential(
        account_sid="AC" + "7" * 32,
        auth_token="budget-guard-twilio-auth-token",
    )
    await store_provider_config(
        db,
        tenant.id,
        "twilio",
        {
            "account_sid": twilio_credential.account_sid,
            "auth_token": twilio_credential.auth_token,
            "default_from_number": "+97141234567",
        },
    )
    mark_twilio_route_verified(
        profile,
        twilio_credential,
        expected_voice_url=(
            f"{settings.base_url.rstrip('/')}/api/v1/webhooks/twilio/voice/inbound"
        ),
    )
    processed_call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="completed",
        from_number="+97141234567",
        to_number="+971501111111",
        provider="twilio",
        duration_seconds=60,
        created_at=now - timedelta(minutes=2),
    )
    unprocessed_call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="completed",
        from_number="+97141234567",
        to_number="+971502222222",
        provider="twilio",
        duration_seconds=30,
        created_at=now - timedelta(minutes=1),
    )
    db.add_all([processed_call, unprocessed_call])
    await db.flush()
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Budget concierge",
    )
    db.add(
        UsageRecord(
            tenant_id=tenant.id,
            call_id=processed_call.id,
            usage_type="call_minutes",
            quantity=1,
            unit="minutes",
            cost_cents=95,
            period_start=now,
            period_end=now,
        )
    )
    await db.commit()

    commitment = await monthly_agent_budget_commitment(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        month_start=_month_start(now),
        max_call_duration_seconds=agent.max_call_duration_seconds,
        include_prospective_call=True,
    )
    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "ledger-budget-block-0001"},
        json={"agent_id": str(agent.id), "to_number": "+971503333333"},
    )

    assert commitment.ledger_cents == 95
    assert commitment.unprocessed_reservation_cents == 3
    assert commitment.prospective_reservation_cents == 3
    assert commitment.total_cents == 101
    assert response.status_code == 402
    assert response.json()["detail"] == "Agent monthly call budget has been reached"
    assert await db.scalar(select(func.count()).select_from(Call)) == 2
