"""Campaign dispatch idempotency, callback, compliance, and RBAC tests."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from starlette.requests import Request
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import webhooks
from app.core import database
from app.core.security import create_access_token, hash_password
from app.models.agent import Agent
from app.models.call import Call, CallSummary, CallTranscript
from app.models.campaign import (
    Campaign,
    CampaignContact,
    CampaignContactAttempt,
    ProviderCallbackOutbox,
)
from app.models.compliance import DncEntry
from app.models.user import User
from app.models.workflow import Workflow
from app.providers.smallest import SmallestAIError
from app.services.campaign_lifecycle import sync_campaign_call_lifecycle
from app.tasks import call_tasks, campaign_tasks
from tests.conftest import engine as test_engine
from tests.conftest import test_session_factory as session_factory


def test_provider_auth_normalization_preserves_definitive_campaign_classification():
    provider_auth_error = SmallestAIError(
        "Smallest.ai rejected the configured server credentials or permissions.",
        status_code=502,
        upstream_status_code=401,
    )

    assert campaign_tasks._is_definitive_provider_rejection(provider_auth_error) is True
    assert (
        campaign_tasks._is_definitive_provider_rejection(
            SmallestAIError("Smallest.ai failed", status_code=502)
        )
        is False
    )


async def _seed_campaign(
    db,
    tenant_id,
    *,
    provider: str = "smallest",
    phone_numbers: tuple[str, ...] = ("+971501234567",),
    max_concurrent_calls: int = 1,
    retry_attempts: int = 0,
):
    agent = Agent(
        tenant_id=tenant_id,
        name=f"{provider.title()} reliability agent",
        system_prompt="Help safely.",
        voice_provider=provider,
        provider_agent_id="smallest-reliability-agent" if provider == "smallest" else None,
        provider_revision_id="smallest-reliability-revision" if provider == "smallest" else None,
        last_synced_at=datetime.now(UTC) if provider == "smallest" else None,
        sync_status="synced" if provider == "smallest" else "local_only",
    )
    db.add(agent)
    await db.flush()
    campaign = Campaign(
        tenant_id=tenant_id,
        agent_id=agent.id,
        name="Reliable campaign",
        status="running",
        calling_hours_start=None,
        calling_hours_end=None,
        timezone="UTC",
        max_concurrent_calls=max_concurrent_calls,
        retry_attempts=retry_attempts,
    )
    db.add(campaign)
    await db.flush()
    contacts = []
    for phone_number in phone_numbers:
        contact = CampaignContact(
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            phone_number=phone_number,
        )
        db.add(contact)
        await db.flush()
        contacts.append(contact)
    campaign.total_contacts = len(contacts)
    await db.commit()
    return agent, campaign, contacts


async def _prepare_workflow_dispatch(db, tenant_id):
    agent, campaign, contacts = await _seed_campaign(db, tenant_id)
    workflow = Workflow(
        tenant_id=tenant_id,
        agent_id=agent.id,
        name="Final readiness workflow",
        trigger_type="campaign",
        is_active=True,
    )
    db.add(workflow)
    await db.flush()
    campaign.workflow_id = workflow.id
    await db.commit()
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign.id,
            tenant_id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant_id,
        )
    assert preparation.payload is not None
    return agent.id, workflow.id, campaign.id, contacts[0].id, preparation.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")]])
async def test_provider_body_limit_enforces_stream_for_missing_or_lying_length(headers):
    messages = [
        {
            "type": "http.request",
            "body": b"x" * (webhooks.MAX_PROVIDER_WEBHOOK_BYTES + 1),
            "more_body": False,
        }
    ]

    async def receive():
        return messages.pop(0)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhook",
            "raw_path": b"/webhook",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        },
        receive,
    )

    with pytest.raises(HTTPException) as exc_info:
        await webhooks._read_bounded_webhook_body(request)
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_public_provider_webhooks_reject_declared_oversize_before_signature(
    client,
    monkeypatch,
):
    declared_size = str(webhooks.MAX_PROVIDER_WEBHOOK_BYTES + 1)
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "configured-for-limit-test")

    smallest_response = await client.post(
        "/api/v1/webhooks/smallest",
        content=b"{}",
        headers={"Content-Length": declared_size},
    )
    twilio_response = await client.post(
        "/api/v1/webhooks/twilio/voice/inbound",
        content=b"",
        headers={"Content-Length": declared_size},
    )

    assert smallest_response.status_code == 413
    assert twilio_response.status_code == 413


@pytest.mark.asyncio
async def test_concurrent_workers_dispatch_one_paid_call(tenant, db, monkeypatch):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    expected_revision = agent.provider_revision_id
    campaign_id = campaign.id
    contact_id = contacts[0].id
    entered_provider = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = 0

    async def blocking_provider_call(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        entered_provider.set()
        await release_provider.wait()
        return "smallest-concurrent-call"

    smallest = SimpleNamespace(start_outbound_call=AsyncMock(side_effect=blocking_provider_call))
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    monkeypatch.setattr(campaign_tasks, "get_smallest_client", lambda: smallest)
    monkeypatch.setattr(campaign_tasks.run_campaign, "apply_async", Mock())

    first = asyncio.create_task(
        campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))
    )
    await asyncio.wait_for(entered_provider.wait(), timeout=2)
    second = asyncio.create_task(
        campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))
    )
    await asyncio.sleep(0)
    assert provider_calls == 1
    release_provider.set()
    await asyncio.gather(first, second)

    db.expire_all()
    assert provider_calls == 1
    smallest.start_outbound_call.assert_awaited_once()
    assert smallest.start_outbound_call.await_args.kwargs["version_id"] == expected_revision
    assert await db.scalar(select(func.count()).select_from(Call)) == 1
    assert await db.scalar(select(func.count()).select_from(CampaignContactAttempt)) == 1
    attempt = await db.scalar(select(CampaignContactAttempt))
    contact = await db.get(CampaignContact, contact_id)
    assert attempt.state == "accepted"
    assert contact.status == "calling"
    assert (await db.get(Campaign, campaign_id)).status == "running"


@pytest.mark.asyncio
async def test_postgres_waiting_workers_refresh_claim_before_provider_call(
    tenant,
    db,
    monkeypatch,
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    _agent, campaign, _contacts = await _seed_campaign(db, tenant.id)
    async with session_factory() as claiming_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            claiming_db,
            campaign.id,
            tenant.id,
        )
    attempt_id = plan.attempt_ids[0]

    # Hold the aggregate lock so both workers read the scalar claim identity
    # before either can transition it. This deterministically exercises the
    # identity-map race that used to return two dispatch preparations.
    blocker = session_factory()
    await blocker.execute(select(Campaign).where(Campaign.id == campaign.id).with_for_update())
    loop = asyncio.get_running_loop()
    both_waiting = asyncio.Event()
    campaign_lock_queries = 0

    def observe_campaign_lock(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal campaign_lock_queries
        normalized = statement.upper()
        if "FROM CAMPAIGNS" in normalized and "FOR UPDATE" in normalized:
            campaign_lock_queries += 1
            if campaign_lock_queries >= 2:
                loop.call_soon_threadsafe(both_waiting.set)

    event.listen(test_engine.sync_engine, "before_cursor_execute", observe_campaign_lock)

    async def prepare_in_worker():
        async with session_factory() as worker_db:
            return await campaign_tasks._prepare_attempt_dispatch(
                worker_db,
                attempt_id,
                tenant.id,
            )

    first = asyncio.create_task(prepare_in_worker())
    second = asyncio.create_task(prepare_in_worker())
    try:
        await asyncio.wait_for(both_waiting.wait(), timeout=5)
        await blocker.commit()
        preparations = await asyncio.gather(first, second)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe_campaign_lock)
        await blocker.close()
        for task in (first, second):
            if not task.done():
                task.cancel()

    payloads = [item.payload for item in preparations if item.payload is not None]
    assert len(payloads) == 1
    provider_call = AsyncMock(return_value="one-provider-call")
    monkeypatch.setattr(campaign_tasks, "_call_provider", provider_call)
    async with session_factory() as dispatch_db:
        assert (
            await campaign_tasks._call_provider_with_final_guard(dispatch_db, payloads[0])
            == "one-provider-call"
        )
    provider_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_provider_success_before_commit_never_redials(
    tenant,
    db,
    monkeypatch,
):
    _agent, campaign, _contacts = await _seed_campaign(db, tenant.id)
    smallest = SimpleNamespace(
        start_outbound_call=AsyncMock(return_value="smallest-accepted-before-crash")
    )
    original_record_acceptance = campaign_tasks._record_provider_acceptance
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    monkeypatch.setattr(campaign_tasks, "get_smallest_client", lambda: smallest)
    monkeypatch.setattr(campaign_tasks.run_campaign, "apply_async", Mock())
    monkeypatch.setattr(
        campaign_tasks,
        "_record_provider_acceptance",
        AsyncMock(side_effect=RuntimeError("simulated worker crash")),
    )

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await campaign_tasks._run_campaign_async(str(campaign.id), str(tenant.id))

    monkeypatch.setattr(
        campaign_tasks,
        "_record_provider_acceptance",
        original_record_acceptance,
    )
    await campaign_tasks._run_campaign_async(str(campaign.id), str(tenant.id))

    attempt = await db.scalar(select(CampaignContactAttempt))
    call = await db.scalar(select(Call))
    assert smallest.start_outbound_call.await_count == 1
    assert attempt.state == "dispatching"
    assert call.status == "dispatching"


@pytest.mark.asyncio
async def test_schedule_end_cancels_unstarted_claim_instead_of_stranding_it(
    tenant,
    db,
):
    _agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign_id = campaign.id
    contact_id = contacts[0].id
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    assert len(plan.attempt_ids) == 1

    stored_campaign = await db.get(Campaign, campaign_id)
    stored_campaign.scheduled_end = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    async with session_factory() as worker_db:
        retry_plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )

    assert retry_plan.attempt_ids == ()
    db.expire_all()
    attempt = await db.scalar(select(CampaignContactAttempt))
    contact = await db.get(CampaignContact, contact_id)
    completed_campaign = await db.get(Campaign, campaign_id)
    assert attempt.state == "cancelled"
    assert contact.status == "skipped"
    assert completed_campaign.status == "completed"
    assert completed_campaign.completed_contacts == 1


def test_running_campaign_sweep_reenqueues_durable_start_requests(monkeypatch):
    campaign_id = "11111111-1111-1111-1111-111111111111"
    tenant_id = "22222222-2222-2222-2222-222222222222"
    run_delay = Mock()

    def fake_run_async(coro):
        coro.close()
        return [(campaign_id, tenant_id)]

    monkeypatch.setattr(campaign_tasks, "_run_async", fake_run_async)
    monkeypatch.setattr(campaign_tasks.run_campaign, "delay", run_delay)

    assert campaign_tasks.sweep_running_campaigns.run() == 1
    run_delay.assert_called_once_with(campaign_id, tenant_id)


@pytest.mark.asyncio
async def test_smallest_outbound_pre_event_does_not_duplicate_direct_call_claim(
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Direct pre-race agent",
        system_prompt="Help safely.",
        voice_provider="smallest",
        provider_agent_id="direct-pre-race-agent",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
    )
    db.add(call)
    await db.commit()
    call_id = call.id

    effects = await webhooks._process_smallest_webhook(
        db,
        {
            "id": "direct-pre-race-delivery",
            "metadata": {
                "agentId": agent.provider_agent_id,
                "callId": "remote-direct-pre-race",
                "eventType": "pre-conversation",
                "conversationType": "telephonyOutbound",
            },
        },
    )
    await db.commit()

    assert effects == webhooks.ProviderWebhookEffects()
    db.expire_all()
    assert await db.scalar(select(func.count()).select_from(Call)) == 1
    stored_call = await db.get(Call, call_id)
    assert stored_call.provider_call_sid is None
    assert stored_call.status == "dispatching"


@pytest.mark.asyncio
async def test_smallest_post_event_binds_reserved_direct_call_identity(
    client,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Direct post-race agent",
        system_prompt="Help safely.",
        voice_provider="smallest",
        provider_agent_id="direct-post-race-agent",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
    )
    db.add(call)
    await db.commit()
    call_id = call.id

    payload = {
        "id": "direct-post-race-delivery",
        "metadata": {
            "agentId": agent.provider_agent_id,
            "callId": "remote-direct-post-race",
            "eventType": "post-conversation",
            "conversationType": "telephonyOutbound",
            "variables": {
                "_vav_call_id": str(call_id),
                "_voice_ai_call_id": "legacy-caller-controlled-value",
            },
            "callData": {"callStatus": "completed", "callDuration": 12},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "direct-post-race-secret"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    outbox_kick = Mock()
    monkeypatch.setattr(webhooks.settings, "smallest_webhook_secret", secret)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(
        campaign_tasks.dispatch_provider_callback_outbox,
        "delay",
        outbox_kick,
    )

    response = await client.post(
        "/api/v1/webhooks/smallest",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )

    assert response.status_code == 204
    db.expire_all()
    stored_call = await db.get(Call, call_id)
    assert await db.scalar(select(func.count()).select_from(Call)) == 1
    assert stored_call.provider_call_sid == "remote-direct-post-race"
    assert stored_call.status == "completed"
    outbox = await db.scalar(select(ProviderCallbackOutbox))
    assert outbox.call_id == call_id
    assert outbox.action == "process_completed_call"
    outbox_kick.assert_called_once_with(str(outbox.id))


@pytest.mark.asyncio
async def test_smallest_outbound_pre_event_does_not_duplicate_campaign_claim(
    tenant,
    db,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    contact_id = contacts[0].id
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign.id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    local_call_id = preparation.payload.call_id

    effects = await webhooks._process_smallest_webhook(
        db,
        {
            "id": "campaign-pre-race-delivery",
            "metadata": {
                "agentId": agent.provider_agent_id,
                "callId": "remote-campaign-pre-race",
                "eventType": "pre-conversation",
                "conversationType": "telephonyOutbound",
            },
        },
    )
    await db.commit()

    assert effects == webhooks.ProviderWebhookEffects()
    db.expire_all()
    assert await db.scalar(select(func.count()).select_from(Call)) == 1
    stored_call = await db.get(Call, local_call_id)
    contact = await db.get(CampaignContact, contact_id)
    attempt = await db.get(CampaignContactAttempt, plan.attempt_ids[0])
    assert stored_call.provider_call_sid is None
    assert stored_call.status == "dispatching"
    assert contact.status == "dispatching"
    assert attempt.state == "dispatching"


@pytest.mark.asyncio
async def test_late_old_attempt_callback_cannot_reset_new_active_attempt(
    tenant,
    db,
):
    _agent, campaign, contacts = await _seed_campaign(
        db,
        tenant.id,
        retry_attempts=1,
    )
    campaign_id = campaign.id
    contact_id = contacts[0].id

    async with session_factory() as worker_db:
        first_plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        first = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            first_plan.attempt_ids[0],
            tenant.id,
        )
        first_call = await worker_db.get(Call, first.payload.call_id)
        first_call.status = "failed"
        first_call.ended_at = datetime.now(UTC)
        first_result = await sync_campaign_call_lifecycle(worker_db, first_call)
        await worker_db.commit()
    assert first_result.should_dispatch is True

    async with session_factory() as worker_db:
        second_plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        second = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            second_plan.attempt_ids[0],
            tenant.id,
        )
    assert second.payload is not None

    async with session_factory() as worker_db:
        old_call = await worker_db.get(Call, first.payload.call_id)
        late_result = await sync_campaign_call_lifecycle(worker_db, old_call)
        await worker_db.commit()
    assert late_result.should_dispatch is False

    db.expire_all()
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    assert contact.status == "dispatching"
    assert contact.last_call_id == second.payload.call_id
    assert contact.attempts == 2
    assert stored_campaign.status == "running"


@pytest.mark.asyncio
async def test_smallest_callback_reconciles_crash_window_and_metrics_once(
    client,
    tenant,
    db,
    monkeypatch,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign_id = campaign.id
    contact_id = contacts[0].id
    contact_phone_number = contacts[0].phone_number
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    variables = campaign_tasks._smallest_variables(preparation.payload)

    payload = {
        "id": "delivery-smallest-reliability-1",
        "metadata": {
            "agentId": agent.provider_agent_id,
            "callId": "smallest-reconciled-conversation",
            "eventType": "post-conversation",
            "conversationType": "telephonyOutbound",
            "variables": variables,
            "callData": {
                "callStatus": "completed",
                "callDuration": 42,
                "toNumber": contact_phone_number,
            },
            "transcript": [{"role": "assistant", "content": "Hello"}],
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "smallest-callback-test-secret"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    process_delay = Mock()
    campaign_delay = Mock()
    outbox_kick = Mock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(webhooks.settings, "smallest_webhook_secret", secret)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(call_tasks.process_completed_call, "delay", process_delay)
    monkeypatch.setattr(campaign_tasks.run_campaign, "delay", campaign_delay)
    monkeypatch.setattr(
        campaign_tasks.dispatch_provider_callback_outbox,
        "delay",
        outbox_kick,
    )

    first = await client.post(
        "/api/v1/webhooks/smallest",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    second = await client.post(
        "/api/v1/webhooks/smallest",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )

    assert first.status_code == 204
    assert second.status_code == 204
    db.expire_all()
    call = await db.scalar(select(Call))
    attempt = await db.scalar(select(CampaignContactAttempt))
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    assert await db.scalar(select(func.count()).select_from(Call)) == 1
    assert call.provider_call_sid == "smallest-reconciled-conversation"
    assert call.status == "completed"
    assert attempt.state == "completed"
    assert contact.status == "completed"
    assert stored_campaign.status == "completed"
    assert stored_campaign.completed_contacts == 1
    assert stored_campaign.successful_contacts == 1
    outbox = await db.scalar(select(ProviderCallbackOutbox))
    assert await db.scalar(select(func.count()).select_from(ProviderCallbackOutbox)) == 1
    assert outbox.status == "pending"
    assert outbox_kick.call_count == 1
    assert process_delay.call_count == 0
    campaign_delay.assert_not_called()
    assert (
        await campaign_tasks._dispatch_provider_callback_outbox_async(str(outbox.id))
        == "dispatched"
    )
    await db.refresh(outbox)
    assert outbox.status == "dispatched"
    assert process_delay.call_count == 1
    process_delay.assert_called_with(str(call.id), str(call.tenant_id))


@pytest.mark.asyncio
async def test_smallest_analytics_recovers_lost_post_conversation_event(
    client,
    tenant,
    db,
    monkeypatch,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign_id = campaign.id
    contact_id = contacts[0].id
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    async with session_factory() as worker_db:
        await campaign_tasks._record_provider_acceptance(
            worker_db,
            preparation.payload,
            "smallest-analytics-recovery",
        )

    payload = {
        "id": "analytics-recovery-delivery",
        "metadata": {
            "agentId": agent.provider_agent_id,
            "callId": "smallest-analytics-recovery",
            "eventType": "analytics-completed",
            "conversationType": "telephonyOutbound",
            "callData": {
                "callStatus": "completed",
                "callDuration": 19,
                "toNumber": contacts[0].phone_number,
            },
            "analytics": {"summary": "Recovered terminal lifecycle."},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "smallest-analytics-recovery-secret"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    outbox_kick = Mock()
    monkeypatch.setattr(webhooks.settings, "smallest_webhook_secret", secret)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(
        campaign_tasks.dispatch_provider_callback_outbox,
        "delay",
        outbox_kick,
    )

    response = await client.post(
        "/api/v1/webhooks/smallest",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    late_post_payload = {
        "id": "late-post-after-analytics",
        "metadata": {
            "agentId": agent.provider_agent_id,
            "callId": "smallest-analytics-recovery",
            "eventType": "post-conversation",
            "conversationType": "telephonyOutbound",
            "callData": {"callStatus": "completed", "callDuration": 7},
            "transcript": [{"role": "assistant", "content": "Late transcript."}],
        },
    }
    late_post_body = json.dumps(late_post_payload, separators=(",", ":")).encode()
    late_post_signature = hmac.new(secret.encode(), late_post_body, hashlib.sha256).hexdigest()
    late_post_response = await client.post(
        "/api/v1/webhooks/smallest",
        content=late_post_body,
        headers={"Content-Type": "application/json", "X-Signature": late_post_signature},
    )

    assert response.status_code == 204
    assert late_post_response.status_code == 204
    db.expire_all()
    call = await db.get(Call, preparation.payload.call_id)
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    outbox = await db.scalar(select(ProviderCallbackOutbox))
    transcript = await db.scalar(
        select(CallTranscript).where(CallTranscript.call_id == preparation.payload.call_id)
    )
    assert call.status == "completed"
    assert call.duration_seconds == 19
    assert contact.status == "completed"
    assert stored_campaign.status == "completed"
    assert transcript.full_text == "Assistant: Late transcript."
    assert await db.scalar(select(func.count()).select_from(ProviderCallbackOutbox)) == 1
    assert outbox.action == "process_completed_call"
    outbox_kick.assert_called_once_with(str(outbox.id))


@pytest.mark.asyncio
async def test_concurrent_smallest_post_and_analytics_serialize_and_reprocess(
    client,
    tenant,
    db,
    monkeypatch,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign.id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    async with session_factory() as worker_db:
        await campaign_tasks._record_provider_acceptance(
            worker_db,
            preparation.payload,
            "smallest-concurrent-lifecycle",
        )

    base_metadata = {
        "agentId": agent.provider_agent_id,
        "callId": "smallest-concurrent-lifecycle",
        "conversationType": "telephonyOutbound",
    }
    post_payload = {
        "id": "concurrent-post-delivery",
        "metadata": {
            **base_metadata,
            "eventType": "post-conversation",
            "callData": {"callStatus": "completed", "callDuration": 10},
            "variables": {"customer_name": "Maya"},
            "transcript": [{"role": "user", "content": "Please follow up."}],
        },
    }
    analytics_payload = {
        "id": "concurrent-analytics-delivery",
        "metadata": {
            **base_metadata,
            "eventType": "analytics-completed",
            "callData": {"callStatus": "completed", "callDuration": 20},
            "analytics": {
                "summary": "Authoritative provider analytics.",
                "sentiment": "positive",
                "dispositionMetrics": [{"value": "callback"}],
            },
        },
    }
    secret = "concurrent-smallest-secret"
    monkeypatch.setattr(webhooks.settings, "smallest_webhook_secret", secret)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    outbox_kick = Mock()
    process_delay = Mock()
    monkeypatch.setattr(
        campaign_tasks.dispatch_provider_callback_outbox,
        "delay",
        outbox_kick,
    )
    monkeypatch.setattr(call_tasks.process_completed_call, "delay", process_delay)

    async def post_signed(payload):
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return await client.post(
            "/api/v1/webhooks/smallest",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )

    post_task = asyncio.create_task(post_signed(post_payload))
    await asyncio.sleep(0)
    analytics_task = asyncio.create_task(post_signed(analytics_payload))
    post_response, analytics_response = await asyncio.gather(post_task, analytics_task)

    assert post_response.status_code == 204
    assert analytics_response.status_code == 204
    db.expire_all()
    call = await db.get(Call, preparation.payload.call_id)
    summary = await db.scalar(select(CallSummary).where(CallSummary.call_id == call.id))
    transcript = await db.scalar(select(CallTranscript).where(CallTranscript.call_id == call.id))
    outboxes = (
        (
            await db.execute(
                select(ProviderCallbackOutbox).order_by(ProviderCallbackOutbox.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert await db.scalar(select(func.count()).select_from(Call)) == 1
    assert call.duration_seconds == 20
    assert call.disposition == "callback"
    assert summary.summary == "Authoritative provider analytics."
    assert transcript.full_text == "User: Please follow up."
    assert set(call.call_metadata["smallest_webhook_deliveries"]) == {
        "concurrent-post-delivery",
        "concurrent-analytics-delivery",
    }
    assert call.call_metadata["smallest_variables"]["customer_name"] == "Maya"
    assert len(outboxes) == 2
    assert {outbox.action for outbox in outboxes} == {
        "process_completed_call",
        "process_analytics_update",
    }

    for outbox in outboxes:
        assert (
            await campaign_tasks._dispatch_provider_callback_outbox_async(str(outbox.id))
            == "dispatched"
        )
    assert process_delay.call_count == 2
    analytics_outbox = next(
        outbox for outbox in outboxes if outbox.action == "process_analytics_update"
    )
    process_delay.assert_any_call(
        str(call.id),
        str(call.tenant_id),
        analytics_outbox.event_key,
        "call.analytics_updated",
    )
    corrected_payload = await call_tasks._process_completed_call_async(
        str(call.id),
        str(call.tenant_id),
    )
    assert corrected_payload["duration_seconds"] == 20
    assert corrected_payload["disposition"] == "callback"


@pytest.mark.asyncio
async def test_postgres_waiting_callbacks_refresh_call_before_state_merge(
    tenant,
    db,
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    agent = Agent(
        tenant_id=tenant.id,
        name="Callback concurrency agent",
        system_prompt="Help safely.",
        voice_provider="twilio",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number="+971501111111",
        to_number="+971502222222",
        provider="twilio",
        provider_call_sid="callback-concurrency-sid",
    )
    db.add(call)
    await db.commit()
    call_id = call.id

    blocker = session_factory()
    await blocker.execute(select(Call).where(Call.id == call_id).with_for_update())
    loop = asyncio.get_running_loop()
    first_waiting = asyncio.Event()
    both_waiting = asyncio.Event()
    call_lock_queries = 0

    def observe_call_lock(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal call_lock_queries
        normalized = statement.upper()
        if "FROM CALLS" in normalized and "FOR UPDATE" in normalized:
            call_lock_queries += 1
            loop.call_soon_threadsafe(first_waiting.set)
            if call_lock_queries >= 2:
                loop.call_soon_threadsafe(both_waiting.set)

    event.listen(test_engine.sync_engine, "before_cursor_execute", observe_call_lock)

    async def merge_callback(*, terminal: bool):
        async with session_factory() as callback_db:
            probe = await callback_db.scalar(select(Call).where(Call.id == call_id))
            locked_call, _attempt = await webhooks._lock_callback_call_graph(callback_db, probe)
            if terminal:
                locked_call.status = "completed"
                locked_call.ended_at = datetime.now(UTC)
            elif locked_call.status not in {"completed", "failed", "busy", "no_answer"}:
                locked_call.status = "in_progress"
            await callback_db.commit()

    terminal_task = asyncio.create_task(merge_callback(terminal=True))
    later_nonterminal_task = None
    try:
        await asyncio.wait_for(first_waiting.wait(), timeout=5)
        later_nonterminal_task = asyncio.create_task(merge_callback(terminal=False))
        await asyncio.wait_for(both_waiting.wait(), timeout=5)
        await blocker.commit()
        await asyncio.gather(terminal_task, later_nonterminal_task)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe_call_lock)
        await blocker.close()
        for task in (terminal_task, later_nonterminal_task):
            if task is not None and not task.done():
                task.cancel()

    db.expire_all()
    assert (await db.get(Call, call_id)).status == "completed"


@pytest.mark.asyncio
async def test_twilio_terminal_callback_updates_campaign_idempotently(
    client,
    tenant,
    db,
    monkeypatch,
):
    _agent, campaign, contacts = await _seed_campaign(db, tenant.id, provider="twilio")
    campaign_id = campaign.id
    contact_id = contacts[0].id
    monkeypatch.setattr(campaign_tasks.settings, "twilio_default_from_number", "+971501111111")
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    call_id = preparation.payload.call_id

    auth_token = "twilio-reliability-auth-token"
    path = f"/api/v1/webhooks/twilio/status/{call_id}"
    url = f"http://test{path}"
    form = {
        "CallSid": "CA-reliable-campaign",
        "CallStatus": "completed",
        "CallDuration": "31",
    }
    signature = RequestValidator(auth_token).compute_signature(url, form)
    process_delay = Mock()
    campaign_delay = Mock()
    outbox_kick = Mock()
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(call_tasks.process_completed_call, "delay", process_delay)
    monkeypatch.setattr(campaign_tasks.run_campaign, "delay", campaign_delay)
    monkeypatch.setattr(
        campaign_tasks.dispatch_provider_callback_outbox,
        "delay",
        outbox_kick,
    )

    first = await client.post(
        path,
        data=form,
        headers={"X-Twilio-Signature": signature},
    )
    second = await client.post(
        path,
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    db.expire_all()
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    attempt = await db.scalar(select(CampaignContactAttempt))
    assert contact.status == "completed"
    assert attempt.state == "completed"
    assert stored_campaign.status == "completed"
    assert stored_campaign.completed_contacts == 1
    assert stored_campaign.successful_contacts == 1
    outbox = await db.scalar(select(ProviderCallbackOutbox))
    assert await db.scalar(select(func.count()).select_from(ProviderCallbackOutbox)) == 1
    assert outbox.status == "pending"
    assert outbox_kick.call_count == 2
    assert (
        await campaign_tasks._dispatch_provider_callback_outbox_async(str(outbox.id))
        == "dispatched"
    )
    assert process_delay.call_count == 1
    campaign_delay.assert_not_called()


@pytest.mark.asyncio
async def test_postgres_waiting_twilio_callback_refreshes_terminal_graph(
    tenant,
    db,
    monkeypatch,
):
    """A callback waiting on another callback must not reuse stale ORM state."""
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    _agent, campaign, contacts = await _seed_campaign(db, tenant.id, provider="twilio")
    monkeypatch.setattr(campaign_tasks.settings, "twilio_default_from_number", "+971501111111")
    async with session_factory() as claiming_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            claiming_db,
            campaign.id,
            tenant.id,
        )
    async with session_factory() as dispatch_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            dispatch_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    call_id = preparation.payload.call_id
    attempt_id = preparation.payload.attempt_id
    contact_id = contacts[0].id

    blocker = session_factory()
    waiter = session_factory()
    waiting_task = None
    loop = asyncio.get_running_loop()
    waiter_reached_campaign_lock = asyncio.Event()

    try:
        await blocker.execute(select(Campaign).where(Campaign.id == campaign.id).with_for_update())

        # Populate the losing callback's identity map before the winning callback
        # commits terminal state. A plain locked SELECT would otherwise reuse
        # these stale objects after it finishes waiting.
        stale_call = await waiter.get(Call, call_id)
        stale_attempt = await waiter.get(CampaignContactAttempt, attempt_id)
        stale_contact = await waiter.get(CampaignContact, contact_id)
        assert stale_call is not None and stale_call.status == "dispatching"
        assert stale_attempt is not None and stale_attempt.state == "dispatching"
        assert stale_contact is not None and stale_contact.status == "dispatching"

        winning_call = await blocker.get(Call, call_id, with_for_update=True)
        winning_attempt = await blocker.get(
            CampaignContactAttempt,
            attempt_id,
            with_for_update=True,
        )
        winning_contact = await blocker.get(
            CampaignContact,
            contact_id,
            with_for_update=True,
        )
        assert winning_call is not None
        assert winning_attempt is not None
        assert winning_contact is not None
        winning_call.status = "completed"
        winning_call.ended_at = datetime.now(UTC)
        winning_attempt.state = "completed"
        winning_attempt.finished_at = datetime.now(UTC)
        winning_contact.status = "completed"
        await blocker.flush()

        def observe_waiter_lock(_conn, _cursor, statement, _parameters, _context, _many):
            normalized = statement.upper()
            if "FROM CAMPAIGNS" in normalized and "FOR UPDATE" in normalized:
                loop.call_soon_threadsafe(waiter_reached_campaign_lock.set)

        event.listen(test_engine.sync_engine, "before_cursor_execute", observe_waiter_lock)
        try:
            waiting_task = asyncio.create_task(
                webhooks._lock_callback_call_graph(waiter, stale_call)
            )
            await asyncio.wait_for(waiter_reached_campaign_lock.wait(), timeout=5)
            await blocker.commit()
            locked_call, locked_attempt = await asyncio.wait_for(waiting_task, timeout=5)
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", observe_waiter_lock)

        assert locked_call.status == "completed"
        assert locked_attempt is not None and locked_attempt.state == "completed"
        assert stale_contact.status == "completed"

        # Mirror the Twilio voice callback's forward-only merge. A refreshed
        # terminal call remains terminal instead of being pushed to in_progress.
        if locked_call.status not in {"completed", "failed", "busy", "no_answer"}:
            locked_call.status = "in_progress"
        await waiter.commit()
        assert locked_call.status == "completed"
    finally:
        if waiting_task is not None and not waiting_task.done():
            waiting_task.cancel()
        await blocker.rollback()
        await blocker.close()
        await waiter.rollback()
        await waiter.close()


@pytest.mark.asyncio
async def test_dnc_is_rechecked_before_each_provider_side_effect(tenant, db, monkeypatch):
    _agent, campaign, contacts = await _seed_campaign(
        db,
        tenant.id,
        phone_numbers=("+971501234567", "+971551234567"),
        max_concurrent_calls=2,
    )
    campaign_id = campaign.id
    first_contact_id = contacts[0].id
    second_contact_id = contacts[1].id
    first_phone_number = contacts[0].phone_number
    second_phone_number = contacts[1].phone_number
    provider_calls: list[str] = []

    async def add_second_contact_to_dnc(*, phone_number, **_kwargs):
        provider_calls.append(phone_number)
        async with session_factory() as dnc_db:
            dnc_db.add(
                DncEntry(
                    tenant_id=tenant.id,
                    phone_number=second_phone_number,
                )
            )
            await dnc_db.commit()
        return "first-provider-call"

    smallest = SimpleNamespace(start_outbound_call=AsyncMock(side_effect=add_second_contact_to_dnc))
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    monkeypatch.setattr(campaign_tasks, "get_smallest_client", lambda: smallest)
    monkeypatch.setattr(campaign_tasks.run_campaign, "apply_async", Mock())

    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))

    db.expire_all()
    first = await db.get(CampaignContact, first_contact_id)
    second = await db.get(CampaignContact, second_contact_id)
    assert provider_calls == [first_phone_number]
    assert first.status == "calling"
    assert second.status == "dnc"
    assert second.attempts == 0


@pytest.mark.asyncio
async def test_dnc_write_winning_final_guard_prevents_campaign_dial(tenant, db, monkeypatch):
    _agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    contact_id = contacts[0].id
    phone_number = contacts[0].phone_number
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign.id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None

    provider_call = AsyncMock(return_value="must-not-be-used")
    monkeypatch.setattr(campaign_tasks, "_call_provider", provider_call)
    async with session_factory() as dnc_db, session_factory() as dispatch_db:
        async with campaign_tasks.tenant_phone_dnc_lock(
            dnc_db,
            tenant.id,
            phone_number,
        ):
            dispatch_task = asyncio.create_task(
                campaign_tasks._call_provider_with_final_guard(
                    dispatch_db,
                    preparation.payload,
                )
            )
            await asyncio.sleep(0)
            provider_call.assert_not_awaited()
            dnc_db.add(
                DncEntry(
                    tenant_id=tenant.id,
                    phone_number=phone_number,
                    reason="customer_request",
                    source="test",
                )
            )
            await dnc_db.commit()
        assert await dispatch_task is None

    provider_call.assert_not_awaited()
    db.expire_all()
    contact = await db.get(CampaignContact, contact_id)
    attempt = await db.get(CampaignContactAttempt, plan.attempt_ids[0])
    call = await db.get(Call, preparation.payload.call_id)
    assert contact.status == "dnc"
    assert attempt.state == "cancelled"
    assert call.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readiness_target",
    ["agent", "agent_sync", "agent_revision", "workflow"],
)
async def test_final_guard_reloads_committed_readiness_change_before_dial(
    readiness_target,
    tenant,
    db,
    monkeypatch,
):
    agent_id, workflow_id, campaign_id, contact_id, payload = await _prepare_workflow_dispatch(
        db, tenant.id
    )
    original_revision = payload.provider_revision_id
    async with session_factory() as update_db:
        if readiness_target in {"agent", "agent_sync", "agent_revision"}:
            resource = await update_db.get(Agent, agent_id)
        else:
            resource = await update_db.get(Workflow, workflow_id)
        if readiness_target == "agent_sync":
            resource.sync_status = "dirty"
        elif readiness_target == "agent_revision":
            resource.provider_revision_id = "new-synced-revision-before-final-guard"
            resource.sync_status = "synced"
            resource.last_synced_at = datetime.now(UTC)
        else:
            resource.is_active = False
        await update_db.commit()

    provider_call = AsyncMock(return_value="must-not-be-used")
    monkeypatch.setattr(campaign_tasks, "_call_provider", provider_call)
    async with session_factory() as dispatch_db:
        assert await campaign_tasks._call_provider_with_final_guard(dispatch_db, payload) is None

    provider_call.assert_not_awaited()
    if readiness_target == "agent_revision":
        assert original_revision == "smallest-reliability-revision"
        assert payload.provider_revision_id == original_revision
    db.expire_all()
    campaign = await db.get(Campaign, campaign_id)
    contact = await db.get(CampaignContact, contact_id)
    attempt = await db.get(CampaignContactAttempt, payload.attempt_id)
    call = await db.get(Call, payload.call_id)
    assert campaign.status == "paused"
    assert contact.status == "pending"
    assert attempt.state == "cancelled"
    assert call.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readiness_target",
    ["agent", "agent_sync", "agent_revision", "workflow"],
)
async def test_postgres_final_guard_waits_for_readiness_update(
    readiness_target,
    tenant,
    db,
    monkeypatch,
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    agent_id, workflow_id, _campaign_id, _contact_id, payload = await _prepare_workflow_dispatch(
        db, tenant.id
    )
    blocker = session_factory()
    dispatch_db = session_factory()
    dispatch_task = None
    readiness_lock_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    agent_target = readiness_target in {"agent", "agent_sync", "agent_revision"}
    model = Agent if agent_target else Workflow
    resource_id = agent_id if agent_target else workflow_id
    table_name = "AGENTS" if agent_target else "WORKFLOWS"

    try:
        resource = await blocker.scalar(
            select(model).where(model.id == resource_id).with_for_update()
        )
        if readiness_target == "agent_sync":
            resource.sync_status = "dirty"
        elif readiness_target == "agent_revision":
            resource.provider_revision_id = "new-synced-revision-before-final-lock"
            resource.sync_status = "synced"
            resource.last_synced_at = datetime.now(UTC)
        else:
            resource.is_active = False
        await blocker.flush()

        def observe_readiness_lock(_conn, _cursor, statement, _parameters, _context, _many):
            normalized = statement.upper()
            if f"FROM {table_name}" in normalized and "FOR UPDATE" in normalized:
                loop.call_soon_threadsafe(readiness_lock_requested.set)

        event.listen(test_engine.sync_engine, "before_cursor_execute", observe_readiness_lock)
        provider_call = AsyncMock(return_value="must-not-be-used")
        monkeypatch.setattr(campaign_tasks, "_call_provider", provider_call)
        try:
            dispatch_task = asyncio.create_task(
                campaign_tasks._call_provider_with_final_guard(dispatch_db, payload)
            )
            await asyncio.wait_for(readiness_lock_requested.wait(), timeout=5)
            provider_call.assert_not_awaited()
            await blocker.commit()
            assert await asyncio.wait_for(dispatch_task, timeout=5) is None
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", observe_readiness_lock)

        provider_call.assert_not_awaited()
    finally:
        if dispatch_task is not None and not dispatch_task.done():
            dispatch_task.cancel()
        await blocker.close()
        await dispatch_db.close()


@pytest.mark.asyncio
async def test_viewer_can_read_but_cannot_mutate_or_trigger_paid_calls(
    client,
    auth_headers,
    tenant,
    db,
):
    agent_response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "RBAC Agent", "system_prompt": "Help the caller."},
    )
    assert agent_response.status_code == 201
    campaign_response = await client.post(
        "/api/v1/campaigns",
        headers=auth_headers,
        json={
            "name": "RBAC campaign",
            "agent_id": agent_response.json()["id"],
            "contacts": [{"phone_number": "+971501234567"}],
        },
    )
    assert campaign_response.status_code == 201
    campaign_id = campaign_response.json()["id"]

    viewer = User(
        tenant_id=tenant.id,
        email="viewer@testcorp.com",
        hashed_password=hash_password("viewer-password"),
        full_name="Read Only Viewer",
        role="viewer",
    )
    db.add(viewer)
    await db.commit()
    viewer_headers = {
        "Authorization": f"Bearer {create_access_token(viewer.id, tenant.id, viewer.role)}"
    }

    assert (await client.get("/api/v1/campaigns", headers=viewer_headers)).status_code == 200
    assert (
        await client.get(
            f"/api/v1/campaigns/{campaign_id}/contacts",
            headers=viewer_headers,
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/campaigns/{campaign_id}/attempts",
            headers=viewer_headers,
        )
    ).status_code == 403

    mutations = [
        await client.post(
            "/api/v1/campaigns",
            headers=viewer_headers,
            json={"name": "Blocked", "agent_id": agent_response.json()["id"]},
        ),
        await client.patch(
            f"/api/v1/campaigns/{campaign_id}",
            headers=viewer_headers,
            json={"name": "Blocked update"},
        ),
        await client.post(
            f"/api/v1/campaigns/{campaign_id}/contacts",
            headers=viewer_headers,
            json=[{"phone_number": "+971551234567"}],
        ),
        await client.post(
            f"/api/v1/campaigns/{campaign_id}/start",
            headers=viewer_headers,
        ),
        await client.post(
            f"/api/v1/campaigns/{campaign_id}/pause",
            headers=viewer_headers,
        ),
        await client.post(
            "/api/v1/calls",
            headers={**viewer_headers, "Idempotency-Key": "viewer-blocked-call-1"},
            json={
                "agent_id": agent_response.json()["id"],
                "to_number": "+971501234567",
            },
        ),
    ]
    assert [response.status_code for response in mutations] == [403] * len(mutations)


@pytest.mark.asyncio
async def test_unpublished_campaign_fails_closed_without_consuming_attempts(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Unpublished campaign agent",
        system_prompt="Help safely.",
        voice_provider="smallest",
        provider_agent_id="partial-provider-agent",
        provider_revision_id="revision-before-failed-security-check",
        last_synced_at=None,
    )
    db.add(agent)
    await db.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        agent_id=agent.id,
        name="Unprovisioned campaign",
        status="draft",
        calling_hours_start=None,
        calling_hours_end=None,
        timezone="UTC",
    )
    db.add(campaign)
    await db.flush()
    contact = CampaignContact(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        phone_number="+971501234567",
    )
    db.add(contact)
    await db.commit()
    campaign_id = campaign.id
    contact_id = contact.id
    enqueue = Mock()
    monkeypatch.setattr(campaign_tasks.run_campaign, "delay", enqueue)

    response = await client.post(
        f"/api/v1/campaigns/{campaign_id}/start",
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert "initial Smallest.ai publish" in response.json()["detail"]
    enqueue.assert_not_called()

    stored_campaign = await db.get(Campaign, campaign_id)
    stored_campaign.status = "running"
    await db.commit()
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))

    db.expire_all()
    stored_campaign = await db.get(Campaign, campaign_id)
    stored_contact = await db.get(CampaignContact, contact_id)
    assert stored_campaign.status == "paused"
    assert "never completed" in stored_campaign.settings["last_dispatch_error"]
    assert stored_contact.status == "pending"
    assert stored_contact.attempts == 0
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_previously_published_dirty_agent_cannot_start_campaign(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    agent.sync_status = "dirty"
    campaign.status = "draft"
    await db.commit()
    enqueue = Mock()
    monkeypatch.setattr(campaign_tasks.run_campaign, "delay", enqueue)

    response = await client.post(
        f"/api/v1/campaigns/{campaign.id}/start",
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert "current changes" in response.json()["detail"]
    enqueue.assert_not_called()

    campaign_id = campaign.id
    contact_id = contacts[0].id
    campaign.status = "running"
    await db.commit()
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))

    db.expire_all()
    stored_campaign = await db.get(Campaign, campaign_id)
    stored_contact = await db.get(CampaignContact, contact_id)
    assert stored_campaign.status == "paused"
    assert "unpublished or unverified" in stored_campaign.settings["last_dispatch_error"]
    assert stored_contact.status == "pending"
    assert stored_contact.attempts == 0
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_unverified_smallest_campaign_resource_is_never_dispatched(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    _agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign.status = "draft"
    campaign.settings = {"from_product_id": "shared-org-resource"}
    await db.commit()
    campaign_id = campaign.id
    contact_id = contacts[0].id

    response = await client.post(
        f"/api/v1/campaigns/{campaign_id}/start",
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert "tenant-owned inventory" in response.json()["detail"]

    campaign.status = "running"
    await db.commit()
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))

    db.expire_all()
    stored_campaign = await db.get(Campaign, campaign_id)
    contact = await db.get(CampaignContact, contact_id)
    assert stored_campaign.status == "paused"
    assert contact.status == "pending"
    assert await db.scalar(select(func.count()).select_from(Call)) == 0
    assert await db.scalar(select(func.count()).select_from(CampaignContactAttempt)) == 0


@pytest.mark.asyncio
async def test_owner_cannot_claim_unverified_provider_identity_for_unknown_attempt(
    client,
    auth_headers,
    tenant,
    db,
):
    _agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign_id = campaign.id
    contact_id = contacts[0].id
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    async with session_factory() as worker_db:
        await campaign_tasks._record_provider_failure(
            worker_db,
            preparation.payload,
            RuntimeError("provider response was lost"),
        )

    blocked_restart = await client.post(
        f"/api/v1/campaigns/{campaign_id}/start",
        headers=auth_headers,
    )
    assert blocked_restart.status_code == 409
    assert "Reconcile unknown" in blocked_restart.json()["detail"]

    listed = await client.get(
        f"/api/v1/campaigns/{campaign_id}/attempts?state=unknown",
        headers=auth_headers,
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(plan.attempt_ids[0])]

    untrusted_binding = await client.post(
        f"/api/v1/campaigns/{campaign_id}/attempts/{plan.attempt_ids[0]}/reconcile",
        headers=auth_headers,
        json={
            "action": "confirm_terminal",
            "provider_call_sid": "other-tenant-unbound-provider-call",
            "outcome": "completed",
            "reason": "Verified in the provider console",
        },
    )
    smuggled_binding = await client.post(
        f"/api/v1/campaigns/{campaign_id}/attempts/{plan.attempt_ids[0]}/reconcile",
        headers=auth_headers,
        json={
            "action": "release_for_retry",
            "provider_call_sid": "other-tenant-unbound-provider-call",
            "reason": "Provider confirms no call was created",
        },
    )

    assert untrusted_binding.status_code == 422
    assert smuggled_binding.status_code == 422
    db.expire_all()
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    call = await db.get(Call, preparation.payload.call_id)
    attempt = await db.get(CampaignContactAttempt, plan.attempt_ids[0])
    assert contact.status == "dispatch_unknown"
    assert stored_campaign.status == "paused"
    assert call.provider_call_sid is None
    assert call.status == "dispatch_unknown"
    assert attempt.state == "unknown"


@pytest.mark.asyncio
async def test_owner_can_release_proven_unaccepted_attempt_for_safe_retry(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    _agent, campaign, contacts = await _seed_campaign(
        db,
        tenant.id,
        retry_attempts=1,
    )
    campaign_id = campaign.id
    contact_id = contacts[0].id
    tenant_id = tenant.id
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    async with session_factory() as worker_db:
        await campaign_tasks._record_provider_failure(
            worker_db,
            preparation.payload,
            TimeoutError("provider outcome unknown"),
        )

    released = await client.post(
        f"/api/v1/campaigns/{campaign_id}/attempts/{plan.attempt_ids[0]}/reconcile",
        headers=auth_headers,
        json={
            "action": "release_for_retry",
            "reason": "Provider confirms no call was created",
        },
    )
    assert released.status_code == 200
    assert released.json()["state"] == "rejected"

    db.expire_all()
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    assert contact.status == "pending"
    assert stored_campaign.status == "paused"

    enqueue = Mock()
    monkeypatch.setattr(campaign_tasks.run_campaign, "delay", enqueue)
    restarted = await client.post(
        f"/api/v1/campaigns/{campaign_id}/start",
        headers=auth_headers,
    )
    assert restarted.status_code == 200
    enqueue.assert_called_once_with(str(campaign_id), str(tenant_id))


@pytest.mark.asyncio
async def test_accepted_call_watchdog_pauses_without_redial_or_tenant_release(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign_id = campaign.id
    contact_id = contacts[0].id
    agent.max_call_duration_seconds = 30
    await db.commit()
    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    async with session_factory() as worker_db:
        await campaign_tasks._record_provider_acceptance(
            worker_db,
            preparation.payload,
            "accepted-call-with-lost-terminal-callback",
        )
    async with session_factory() as worker_db:
        attempt = await worker_db.get(CampaignContactAttempt, plan.attempt_ids[0])
        attempt.accepted_at = datetime.now(UTC) - timedelta(
            seconds=30 + campaign_tasks.ACCEPTED_CALL_GRACE_SECONDS + 1
        )
        await worker_db.commit()

    provider_call = AsyncMock(return_value="must-not-redial")
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    monkeypatch.setattr(campaign_tasks, "_call_provider", provider_call)
    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))

    provider_call.assert_not_awaited()
    db.expire_all()
    attempt = await db.get(CampaignContactAttempt, plan.attempt_ids[0])
    call = await db.get(Call, preparation.payload.call_id)
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    assert attempt.state == "unknown"
    assert attempt.error_code == "terminal_callback_timeout"
    assert attempt.provider_call_sid == "accepted-call-with-lost-terminal-callback"
    assert call.status == "dispatch_unknown"
    assert contact.status == "dispatch_unknown"
    assert stored_campaign.status == "paused"
    assert stored_campaign.completed_contacts == 0

    unsafe_release = await client.post(
        f"/api/v1/campaigns/{campaign_id}/attempts/{attempt.id}/reconcile",
        headers=auth_headers,
        json={
            "action": "release_for_retry",
            "reason": "Do not trust a tenant assertion for a known accepted call",
        },
    )
    assert unsafe_release.status_code == 409
    assert "known accepted provider call" in unsafe_release.json()["detail"]


@pytest.mark.asyncio
async def test_manual_pause_cannot_disable_accepted_call_terminal_watchdog(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent, campaign, contacts = await _seed_campaign(db, tenant.id)
    campaign_id = campaign.id
    contact_id = contacts[0].id
    agent.max_call_duration_seconds = 30
    await db.commit()

    async with session_factory() as worker_db:
        plan = await campaign_tasks._prepare_campaign_batch(
            worker_db,
            campaign_id,
            tenant.id,
        )
    async with session_factory() as worker_db:
        preparation = await campaign_tasks._prepare_attempt_dispatch(
            worker_db,
            plan.attempt_ids[0],
            tenant.id,
        )
    assert preparation.payload is not None
    async with session_factory() as worker_db:
        await campaign_tasks._record_provider_acceptance(
            worker_db,
            preparation.payload,
            "accepted-call-paused-before-lost-terminal-callback",
        )
        attempt = await worker_db.get(CampaignContactAttempt, plan.attempt_ids[0])
        attempt.accepted_at = datetime.now(UTC) - timedelta(
            seconds=30 + campaign_tasks.ACCEPTED_CALL_GRACE_SECONDS + 1
        )
        await worker_db.commit()

    paused = await client.post(
        f"/api/v1/campaigns/{campaign_id}/pause",
        headers=auth_headers,
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    # Beat must continue to select a paused campaign while its accepted call is
    # nonterminal, and running that recovery must never invoke the provider.
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    swept_campaigns = await campaign_tasks._list_running_campaigns()
    assert (str(campaign_id), str(tenant.id)) in swept_campaigns
    provider_call = AsyncMock(return_value="must-not-redial-after-pause")
    monkeypatch.setattr(campaign_tasks, "_call_provider", provider_call)
    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant.id))

    provider_call.assert_not_awaited()
    db.expire_all()
    attempt = await db.get(CampaignContactAttempt, plan.attempt_ids[0])
    call = await db.get(Call, preparation.payload.call_id)
    contact = await db.get(CampaignContact, contact_id)
    stored_campaign = await db.get(Campaign, campaign_id)
    assert attempt.state == "unknown"
    assert attempt.error_code == "terminal_callback_timeout"
    assert attempt.provider_call_sid == "accepted-call-paused-before-lost-terminal-callback"
    assert call.status == "dispatch_unknown"
    assert contact.status == "dispatch_unknown"
    assert stored_campaign.status == "paused"


@pytest.mark.asyncio
async def test_duplicate_contacts_and_oversized_add_batches_are_rejected(
    client,
    auth_headers,
):
    agent_response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Unique Agent", "system_prompt": "Help the caller."},
    )
    agent_id = agent_response.json()["id"]
    duplicate_create = await client.post(
        "/api/v1/campaigns",
        headers=auth_headers,
        json={
            "name": "Duplicate campaign",
            "agent_id": agent_id,
            "contacts": [
                {"phone_number": "+971 50 123 4567"},
                {"phone_number": "+971501234567"},
            ],
        },
    )
    assert duplicate_create.status_code == 422

    created = await client.post(
        "/api/v1/campaigns",
        headers=auth_headers,
        json={
            "name": "Unique campaign",
            "agent_id": agent_id,
            "contacts": [{"phone_number": "+971501234567"}],
        },
    )
    assert created.status_code == 201
    campaign_id = created.json()["id"]
    duplicate_add = await client.post(
        f"/api/v1/campaigns/{campaign_id}/contacts",
        headers=auth_headers,
        json=[{"phone_number": "+971 (50) 123-4567"}],
    )
    assert duplicate_add.status_code == 409

    oversized = await client.post(
        f"/api/v1/campaigns/{campaign_id}/contacts",
        headers=auth_headers,
        json=[{"phone_number": f"+1202555{index:04d}"} for index in range(1_001)],
    )
    assert oversized.status_code == 422

    nested_context = await client.post(
        f"/api/v1/campaigns/{campaign_id}/contacts",
        headers=auth_headers,
        json=[
            {
                "phone_number": "+971551234567",
                "context_data": {"customer": {"tier": "gold"}},
            }
        ],
    )
    reserved_context = await client.post(
        f"/api/v1/campaigns/{campaign_id}/contacts",
        headers=auth_headers,
        json=[
            {
                "phone_number": "+971561234567",
                "context_data": {"_vav_call_id": "tenant-controlled"},
            }
        ],
    )
    assert nested_context.status_code == 422
    assert reserved_context.status_code == 422
