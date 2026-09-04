"""Outbound call compliance tests."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import calls as calls_endpoint
from app.api.v1.endpoints import webhooks
from app.models.agent import Agent, AgentKnowledgeBinding, AgentRuntimeProfile
from app.models.audit import AuditEvent
from app.models.call import Call
from app.models.compliance import DncEntry
from app.services.knowledge_serving import (
    KNOWLEDGE_ADMISSION_STATE,
    knowledge_admission_is_durable,
)
from app.services.phone_numbers import normalize_e164, tenant_phone_dnc_lock
from app.services.provider_credentials import store_provider_config
from app.services.twilio_callback_claim import TWILIO_CALLBACK_CLAIM_METADATA_KEY
from app.services.twilio_route_security import (
    load_workspace_twilio_route_credential,
    mark_twilio_route_verified,
)
from app.tasks import call_tasks
from tests.conftest import test_session_factory as session_factory
from tests.knowledge_test_utils import publish_test_knowledge

TEST_TWILIO_ACCOUNT_SID = "AC" + "5" * 32
TEST_TWILIO_AUTH_TOKEN = "workspace-native-outbound-token"


async def _mark_verified_twilio_route(db, tenant_id, profile: AgentRuntimeProfile) -> None:
    await store_provider_config(
        db,
        tenant_id,
        "twilio",
        {
            "account_sid": TEST_TWILIO_ACCOUNT_SID,
            "auth_token": TEST_TWILIO_AUTH_TOKEN,
            "default_from_number": profile.assigned_numbers[0],
        },
    )
    credential = await load_workspace_twilio_route_credential(db, tenant_id)
    assert credential is not None
    mark_twilio_route_verified(
        profile,
        credential,
        expected_voice_url=(
            f"{calls_endpoint.settings.base_url.rstrip('/')}/api/v1/webhooks/twilio/voice/inbound"
        ),
    )


def test_e164_normalization_handles_common_import_formats():
    assert normalize_e164("+971 (50) 123-4567") == "+971501234567"
    assert normalize_e164("00971 50 123 4567") == "+971501234567"
    assert normalize_e164("0501234567") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voice_provider", "voice_id"),
    [("sarvam", "sarvam:ishita"), ("elevenlabs", "elevenlabs:voice-1")],
)
async def test_native_outbound_call_is_immutably_admitted_before_provider_io(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
    voice_provider,
    voice_id,
):
    agent = Agent(
        tenant_id=tenant.id,
        name=f"{voice_provider} immutable outbound",
        system_prompt="Answer from approved evidence only.",
        voice_provider=voice_provider,
        voice_id=voice_id,
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    from_number = "+15550100002"
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider=voice_provider,
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en",
        assigned_numbers=[from_number],
        max_concurrent_calls=3,
        daily_call_limit=100,
        monthly_budget_cents=10000,
    )
    db.add(profile)
    await _mark_verified_twilio_route(db, tenant.id, profile)
    knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label=f"{voice_provider} outbound",
    )
    await db.commit()

    observed = {}

    async def make_call(request):
        async with session_factory() as inspect_db:
            admitted = await inspect_db.scalar(
                select(Call).where(
                    Call.agent_id == agent.id,
                    Call.to_number == "+15550100001",
                )
            )
            assert admitted is not None
            observed["metadata"] = admitted.call_metadata
            observed["status"] = admitted.status
            observed["provider_request"] = request
        return SimpleNamespace(provider_call_sid=f"CA-{voice_provider}-outbound")

    provider_credentials = []

    def provider_factory(**kwargs):
        provider_credentials.append(kwargs)
        return SimpleNamespace(make_call=make_call)

    monkeypatch.setattr(
        calls_endpoint,
        "get_telephony_provider",
        provider_factory,
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())
    monkeypatch.setattr(call_tasks.reconcile_direct_call_terminal, "apply_async", Mock())

    response = await client.post(
        "/api/v1/calls",
        headers={
            **auth_headers,
            "Idempotency-Key": f"native-{voice_provider}-immutable-outbound-0001",
        },
        json={
            "agent_id": str(agent.id),
            "to_number": "+15550100001",
            "from_number": from_number,
        },
    )

    assert response.status_code == 201
    assert observed["status"] == "dispatching"
    assert knowledge_admission_is_durable(observed["metadata"])
    runtime = observed["metadata"]["runtime"]
    assert runtime["knowledge_admission_state"] == KNOWLEDGE_ADMISSION_STATE
    assert runtime["knowledge_serving_revision_id"] == str(revision.id)
    assert runtime["knowledge_serving_knowledge_base_id"] == str(knowledge.id)
    assert runtime["knowledge_serving_content_sha256"] == revision.content_sha256
    assert runtime["knowledge_source_revision_sha256"] == revision.source_revision_sha256
    assert runtime["knowledge_serving_revocation_generation"] == 0
    assert runtime["media_stream_started"] is False
    callback_binding = observed["metadata"]["telephony_credential_binding"]
    assert callback_binding["source"] == "workspace"
    assert callback_binding["account_sid"] == TEST_TWILIO_ACCOUNT_SID
    assert len(callback_binding["credential_fingerprint"]) == 64
    assert TEST_TWILIO_AUTH_TOKEN not in str(callback_binding)
    provider_request = observed["provider_request"]
    voice_claim = parse_qs(urlsplit(provider_request.webhook_url).query)["vav_callback_claim"]
    status_claim = parse_qs(urlsplit(provider_request.status_callback_url).query)[
        "vav_callback_claim"
    ]
    assert len(voice_claim) == 1
    assert voice_claim == status_claim
    assert len(voice_claim[0]) >= 40
    assert voice_claim[0] not in str(observed["metadata"])
    persisted_claim = observed["metadata"][TWILIO_CALLBACK_CLAIM_METADATA_KEY]
    assert persisted_claim["version"] == 1
    assert persisted_claim["state"] == "pending"
    assert len(persisted_claim["sha256"]) == 64
    assert provider_credentials == [
        {
            "account_sid": TEST_TWILIO_ACCOUNT_SID,
            "auth_token": TEST_TWILIO_AUTH_TOKEN,
        }
    ]


@pytest.mark.asyncio
async def test_native_make_call_response_cannot_overwrite_callback_bound_sid(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(calls_endpoint.settings, "base_url", "http://test")
    agent = Agent(
        tenant_id=tenant.id,
        name="Callback wins provider response race",
        system_prompt="Answer from approved evidence only.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    from_number = "+15550100002"
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider="sarvam",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en",
        assigned_numbers=[from_number],
        max_concurrent_calls=3,
        daily_call_limit=100,
        monthly_budget_cents=10000,
    )
    db.add(profile)
    await _mark_verified_twilio_route(db, tenant.id, profile)
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Callback response race",
    )
    await db.commit()

    callback_sid = "CA-callback-bound-before-response"
    callback_task: asyncio.Task | None = None

    async def make_call(request):
        nonlocal callback_task
        callback_url = urlsplit(request.webhook_url)
        callback_target = callback_url.path
        if callback_url.query:
            callback_target = f"{callback_target}?{callback_url.query}"
        payload = {
            "AccountSid": TEST_TWILIO_ACCOUNT_SID,
            "CallSid": callback_sid,
        }
        signature = RequestValidator(TEST_TWILIO_AUTH_TOKEN).compute_signature(
            request.webhook_url,
            payload,
        )
        # A real Twilio REST response does not wait for its voice webhook to
        # finish. Start the callback concurrently so PostgreSQL can correctly
        # hold the dispatch safety locks until make_call() returns.
        callback_task = asyncio.create_task(
            client.post(
                callback_target,
                data=payload,
                headers={"X-Twilio-Signature": signature},
            )
        )
        return SimpleNamespace(provider_call_sid="CA-conflicting-late-provider-response")

    real_call_relock = calls_endpoint._lock_call_after_provider_dispatch

    async def wait_for_callback_before_provider_merge(*args, **kwargs):
        # initiate_outbound_call commits and releases its dispatch locks before
        # this merge hook. Let the already-started callback bind its SID first,
        # without asking it to defeat any production row or advisory lock.
        assert callback_task is not None
        callback_response = await asyncio.wait_for(callback_task, timeout=5)
        assert callback_response.status_code == 200
        return await real_call_relock(*args, **kwargs)

    monkeypatch.setattr(
        calls_endpoint,
        "get_telephony_provider",
        lambda **_kwargs: SimpleNamespace(make_call=make_call),
    )
    monkeypatch.setattr(
        calls_endpoint,
        "_lock_call_after_provider_dispatch",
        wait_for_callback_before_provider_merge,
    )
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(webhooks, "_kick_provider_outbox", lambda _ids: None)
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())
    monkeypatch.setattr(call_tasks.reconcile_direct_call_terminal, "apply_async", Mock())

    try:
        response = await asyncio.wait_for(
            client.post(
                "/api/v1/calls",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "native-callback-provider-response-race-0001",
                },
                json={
                    "agent_id": str(agent.id),
                    "to_number": "+15550100001",
                    "from_number": from_number,
                },
            ),
            timeout=5,
        )
    finally:
        if callback_task is not None and not callback_task.done():
            callback_task.cancel()
        if callback_task is not None:
            await asyncio.gather(callback_task, return_exceptions=True)

    assert response.status_code == 201
    assert response.json()["provider_call_sid"] == callback_sid
    assert response.json()["status"] == "in_progress"
    db.expire_all()
    stored = await db.get(Call, UUID(response.json()["id"]))
    assert stored.provider_call_sid == callback_sid
    assert stored.call_metadata["dispatch_error"] == "provider_id_conflict"
    claim = stored.call_metadata[TWILIO_CALLBACK_CLAIM_METADATA_KEY]
    assert claim["state"] == "bound"
    assert claim["bound_via"] == "provider_callback"


@pytest.mark.asyncio
async def test_native_outbound_call_without_immutable_knowledge_fails_before_provider_io(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Unpublished native outbound",
        system_prompt="Never serve mutable knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
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
        assigned_numbers=["+15550100002"],
    )
    db.add(profile)
    await _mark_verified_twilio_route(db, tenant.id, profile)
    await db.commit()
    provider_call = AsyncMock()
    monkeypatch.setattr(
        calls_endpoint,
        "get_telephony_provider",
        lambda **_kwargs: SimpleNamespace(make_call=provider_call),
    )

    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "native-missing-release-0001"},
        json={
            "agent_id": str(agent.id),
            "to_number": "+15550100001",
            "from_number": "+15550100002",
        },
    )

    assert response.status_code == 409
    assert "Approve and publish" in response.json()["detail"]
    provider_call.assert_not_awaited()
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voice_provider", "voice_id"),
    [("sarvam", "sarvam:ishita"), ("elevenlabs", "elevenlabs:voice-1")],
)
async def test_native_outbound_never_inherits_platform_twilio_credentials(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
    voice_provider,
    voice_id,
):
    monkeypatch.setattr(calls_endpoint.settings, "twilio_account_sid", "AC" + "9" * 32)
    monkeypatch.setattr(
        calls_endpoint.settings,
        "twilio_auth_token",
        "platform_twilio_token_must_not_be_used",
    )
    monkeypatch.setattr(
        calls_endpoint.settings,
        "twilio_default_from_number",
        "+15550100002",
    )
    provider_call = AsyncMock()
    monkeypatch.setattr(
        calls_endpoint,
        "get_telephony_provider",
        lambda **_kwargs: SimpleNamespace(make_call=provider_call),
    )
    agent = Agent(
        tenant_id=tenant.id,
        name=f"{voice_provider} platform credential isolation",
        system_prompt="Use tenant-owned telephony only.",
        voice_provider=voice_provider,
        voice_id=voice_id,
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider=voice_provider,
            assigned_numbers=["+15550100002"],
        )
    )
    await db.commit()

    response = await client.post(
        "/api/v1/calls",
        headers={
            **auth_headers,
            "Idempotency-Key": f"native-{voice_provider}-no-platform-twilio-0001",
        },
        json={
            "agent_id": str(agent.id),
            "to_number": "+15550100001",
            "from_number": "+15550100002",
        },
    )

    assert response.status_code == 409
    assert "workspace's own Twilio" in response.json()["detail"]
    provider_call.assert_not_awaited()
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_terminal_unknown_call_does_not_consume_native_outbound_concurrency(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Released watchdog slot",
        system_prompt="Answer from approved evidence only.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    from_number = "+15550100002"
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider="sarvam",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en",
        assigned_numbers=[from_number],
        max_concurrent_calls=1,
        daily_call_limit=100,
        monthly_budget_cents=10000,
    )
    db.add(profile)
    await _mark_verified_twilio_route(db, tenant.id, profile)
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Released watchdog slot",
    )
    db.add(
        Call(
            tenant_id=tenant.id,
            agent_id=agent.id,
            direction="outbound",
            status="terminal_unknown",
            from_number=from_number,
            to_number="+15550100003",
            provider="twilio",
            provider_call_sid="CA-terminal-unknown-released-slot",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
        )
    )
    await db.commit()

    provider_call = AsyncMock(
        return_value=SimpleNamespace(provider_call_sid="CA-after-terminal-unknown")
    )
    monkeypatch.setattr(
        calls_endpoint,
        "get_telephony_provider",
        lambda **_kwargs: SimpleNamespace(make_call=provider_call),
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())
    monkeypatch.setattr(call_tasks.reconcile_direct_call_terminal, "apply_async", Mock())

    response = await client.post(
        "/api/v1/calls",
        headers={
            **auth_headers,
            "Idempotency-Key": "terminal-unknown-released-slot-0001",
        },
        json={
            "agent_id": str(agent.id),
            "to_number": "+15550100001",
            "from_number": from_number,
        },
    )

    assert response.status_code == 201
    provider_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_outbound_call_with_corrupt_lexicon_release_fails_before_provider_io(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Corrupt release outbound",
        system_prompt="Never serve an unverifiable release.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
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
        assigned_numbers=["+15550100002"],
    )
    db.add(profile)
    await _mark_verified_twilio_route(db, tenant.id, profile)
    _knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Corrupt release",
    )
    revision.manifest = {
        key: value for key, value in revision.manifest.items() if key != "speech_lexicon"
    }
    await db.commit()

    provider_call = AsyncMock(return_value="must-not-be-called")
    monkeypatch.setattr(
        calls_endpoint,
        "get_telephony_provider",
        lambda **_kwargs: SimpleNamespace(make_call=provider_call),
    )

    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "native-corrupt-release-0001"},
        json={
            "agent_id": str(agent.id),
            "to_number": "+15550100001",
            "from_number": "+15550100002",
        },
    )

    assert response.status_code == 409
    assert "integrity validation" in response.json()["detail"]
    provider_call.assert_not_awaited()
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_direct_outbound_call_rejects_dirty_agent_before_dispatch(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Dirty direct-call agent",
        system_prompt="Do not run a stale provider revision.",
        voice_provider="smallest",
        provider_agent_id="smallest-dirty-direct-agent",
        provider_revision_id="smallest-dirty-direct-revision",
        last_synced_at=datetime.now(UTC),
        sync_status="dirty",
    )
    db.add(agent)
    await db.commit()

    provider_call = AsyncMock(return_value="must-not-be-called")
    monkeypatch.setattr(
        calls_endpoint,
        "get_smallest_client",
        lambda: SimpleNamespace(start_outbound_call=provider_call),
    )
    reconcile = Mock()
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", reconcile)

    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "dirty-direct-preliminary-0001"},
        json={"agent_id": str(agent.id), "to_number": "+971501234567"},
    )

    assert response.status_code == 409
    assert "current changes" in response.json()["detail"]
    provider_call.assert_not_awaited()
    reconcile.assert_not_called()
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_direct_outbound_call_is_blocked_by_tenant_dnc_after_normalization(
    client: AsyncClient,
    auth_headers,
    db,
):
    agent_response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Outbound Agent",
            "system_prompt": "Call customers and be concise.",
        },
    )
    assert agent_response.status_code == 201

    dnc_response = await client.post(
        "/api/v1/compliance/dnc",
        headers=auth_headers,
        json={
            "phone_number": "+971 (50) 123-4567",
            "reason": "customer_request",
        },
    )
    assert dnc_response.status_code == 201

    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "test-dnc-call-0001"},
        json={
            "agent_id": agent_response.json()["id"],
            "to_number": "+971501234567",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Phone number is on this workspace's Do Not Call list"
    call_count = await db.scalar(select(func.count()).select_from(Call))
    assert call_count == 0


@pytest.mark.asyncio
async def test_committed_dnc_addition_wins_final_direct_dispatch_lock(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="DNC race agent",
        system_prompt="Never call after a suppression request commits.",
        voice_provider="smallest",
        provider_agent_id="smallest-dnc-lock-agent",
        provider_revision_id="smallest-dnc-lock-revision",
        last_synced_at=datetime.now(UTC),
        sync_status="synced",
    )
    db.add(agent)
    await db.commit()

    provider_call = AsyncMock(return_value="must-not-be-called")
    monkeypatch.setattr(
        calls_endpoint,
        "get_smallest_client",
        lambda: SimpleNamespace(start_outbound_call=provider_call),
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())

    final_check_waiting = asyncio.Event()
    real_lock = calls_endpoint.tenant_phone_dnc_lock

    @asynccontextmanager
    async def observed_final_lock(session, tenant_id, phone_number):
        final_check_waiting.set()
        async with real_lock(session, tenant_id, phone_number) as canonical:
            yield canonical

    monkeypatch.setattr(calls_endpoint, "tenant_phone_dnc_lock", observed_final_lock)

    async with session_factory() as dnc_db:
        # Formatting differences still resolve to the same tenant+phone lock.
        async with tenant_phone_dnc_lock(dnc_db, tenant.id, "00971 50 123 4567"):
            call_task = asyncio.create_task(
                client.post(
                    "/api/v1/calls",
                    headers={**auth_headers, "Idempotency-Key": "dnc-race-direct-0001"},
                    json={"agent_id": str(agent.id), "to_number": "+971501234567"},
                )
            )
            await asyncio.wait_for(final_check_waiting.wait(), timeout=2)
            dnc_db.add(
                DncEntry(
                    tenant_id=tenant.id,
                    phone_number="+971501234567",
                    reason="customer_request",
                )
            )
            await dnc_db.commit()

        response = await asyncio.wait_for(call_task, timeout=2)

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    provider_call.assert_not_awaited()
    stored_call = await db.scalar(select(Call))
    assert stored_call.call_metadata["dispatch_error"] == "dnc"


@pytest.mark.asyncio
async def test_committed_agent_deactivation_wins_final_direct_dispatch_lock(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Deactivation race agent",
        system_prompt="Never call after an administrator deactivates this agent.",
        voice_provider="smallest",
        provider_agent_id="smallest-deactivation-agent",
        provider_revision_id="smallest-deactivation-revision",
        last_synced_at=datetime.now(UTC),
        sync_status="synced",
    )
    db.add(agent)
    await db.commit()

    provider_call = AsyncMock(return_value="must-not-be-called")
    monkeypatch.setattr(
        calls_endpoint,
        "get_smallest_client",
        lambda: SimpleNamespace(start_outbound_call=provider_call),
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())

    final_check_waiting = asyncio.Event()
    real_lock = calls_endpoint.tenant_phone_dnc_lock

    @asynccontextmanager
    async def observed_final_lock(session, tenant_id, phone_number):
        final_check_waiting.set()
        async with real_lock(session, tenant_id, phone_number) as canonical:
            yield canonical

    monkeypatch.setattr(calls_endpoint, "tenant_phone_dnc_lock", observed_final_lock)

    async with session_factory() as guard_db:
        async with tenant_phone_dnc_lock(guard_db, tenant.id, "+971501234567"):
            call_task = asyncio.create_task(
                client.post(
                    "/api/v1/calls",
                    headers={**auth_headers, "Idempotency-Key": "agent-race-direct-0001"},
                    json={"agent_id": str(agent.id), "to_number": "+971501234567"},
                )
            )
            await asyncio.wait_for(final_check_waiting.wait(), timeout=2)
            deactivated = await client.patch(
                f"/api/v1/agents/{agent.id}",
                headers=auth_headers,
                json={"is_active": False},
            )
            assert deactivated.status_code == 200

        response = await asyncio.wait_for(call_task, timeout=2)

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    provider_call.assert_not_awaited()
    db.expire_all()
    stored_call = await db.scalar(select(Call))
    assert stored_call.call_metadata["dispatch_error"] == "agent_inactive"


@pytest.mark.asyncio
async def test_committed_dirty_agent_wins_final_direct_dispatch_lock(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Dirty race agent",
        system_prompt="Never dial with unpublished provider changes.",
        voice_provider="smallest",
        provider_agent_id="smallest-dirty-race-agent",
        provider_revision_id="smallest-dirty-race-revision",
        last_synced_at=datetime.now(UTC),
        sync_status="synced",
    )
    db.add(agent)
    await db.commit()

    provider_call = AsyncMock(return_value="must-not-be-called")
    monkeypatch.setattr(
        calls_endpoint,
        "get_smallest_client",
        lambda: SimpleNamespace(start_outbound_call=provider_call),
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())

    final_check_waiting = asyncio.Event()
    real_lock = calls_endpoint.tenant_phone_dnc_lock

    @asynccontextmanager
    async def observed_final_lock(session, tenant_id, phone_number):
        final_check_waiting.set()
        async with real_lock(session, tenant_id, phone_number) as canonical:
            yield canonical

    monkeypatch.setattr(calls_endpoint, "tenant_phone_dnc_lock", observed_final_lock)

    async with session_factory() as guard_db:
        async with tenant_phone_dnc_lock(guard_db, tenant.id, "+971501234567"):
            call_task = asyncio.create_task(
                client.post(
                    "/api/v1/calls",
                    headers={
                        **auth_headers,
                        "Idempotency-Key": "dirty-agent-race-direct-0001",
                    },
                    json={"agent_id": str(agent.id), "to_number": "+971501234567"},
                )
            )
            await asyncio.wait_for(final_check_waiting.wait(), timeout=2)
            current_agent = await guard_db.get(Agent, agent.id)
            current_agent.sync_status = "dirty"
            await guard_db.commit()

        response = await asyncio.wait_for(call_task, timeout=2)

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    provider_call.assert_not_awaited()
    db.expire_all()
    stored_call = await db.scalar(select(Call))
    assert stored_call.call_metadata["dispatch_error"] == "agent_not_synced"


@pytest.mark.asyncio
async def test_committed_new_revision_wins_final_direct_dispatch_lock(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    original_revision = "smallest-revision-before-direct-dispatch"
    agent = Agent(
        tenant_id=tenant.id,
        name="Revision race agent",
        system_prompt="Never dial under a revision different from the call snapshot.",
        voice_provider="smallest",
        provider_agent_id="smallest-revision-race-agent",
        provider_revision_id=original_revision,
        last_synced_at=datetime.now(UTC),
        sync_status="synced",
    )
    db.add(agent)
    await db.commit()

    provider_call = AsyncMock(return_value="must-not-be-called")
    monkeypatch.setattr(
        calls_endpoint,
        "get_smallest_client",
        lambda: SimpleNamespace(start_outbound_call=provider_call),
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", Mock())

    final_check_waiting = asyncio.Event()
    real_lock = calls_endpoint.tenant_phone_dnc_lock

    @asynccontextmanager
    async def observed_final_lock(session, tenant_id, phone_number):
        final_check_waiting.set()
        async with real_lock(session, tenant_id, phone_number) as canonical:
            yield canonical

    monkeypatch.setattr(calls_endpoint, "tenant_phone_dnc_lock", observed_final_lock)

    async with session_factory() as guard_db:
        async with tenant_phone_dnc_lock(guard_db, tenant.id, "+971501234567"):
            call_task = asyncio.create_task(
                client.post(
                    "/api/v1/calls",
                    headers={
                        **auth_headers,
                        "Idempotency-Key": "revision-race-direct-0001",
                    },
                    json={"agent_id": str(agent.id), "to_number": "+971501234567"},
                )
            )
            await asyncio.wait_for(final_check_waiting.wait(), timeout=2)
            current_agent = await guard_db.get(Agent, agent.id)
            current_agent.provider_revision_id = "smallest-revision-after-direct-dispatch"
            current_agent.sync_status = "synced"
            current_agent.last_synced_at = datetime.now(UTC)
            await guard_db.commit()

        response = await asyncio.wait_for(call_task, timeout=2)

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    provider_call.assert_not_awaited()
    db.expire_all()
    stored_call = await db.scalar(select(Call))
    assert stored_call.call_metadata["dispatch_error"] == "agent_provider_changed"
    assert (
        stored_call.call_metadata["agent_configuration"]["provider_revision_id"]
        == original_revision
    )


@pytest.mark.asyncio
async def test_dnc_delete_commits_before_number_can_be_dispatched(
    client: AsyncClient,
    auth_headers,
    db,
):
    created = await client.post(
        "/api/v1/compliance/dnc",
        headers=auth_headers,
        json={"phone_number": "00971 50 123 4567"},
    )
    assert created.status_code == 201

    removed = await client.delete(
        f"/api/v1/compliance/dnc/{created.json()['id']}",
        headers=auth_headers,
    )
    assert removed.status_code == 204

    checked = await client.get(
        "/api/v1/compliance/dnc/check",
        headers=auth_headers,
        params={"phone_number": "+971501234567"},
    )
    assert checked.status_code == 200
    assert checked.json()["is_on_dnc"] is False

    audit_events = (
        (
            await db.execute(
                select(AuditEvent)
                .where(AuditEvent.action.in_(["compliance.dnc_added", "compliance.dnc_removed"]))
                .order_by(AuditEvent.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [event.action for event in audit_events] == [
        "compliance.dnc_added",
        "compliance.dnc_removed",
    ]
    assert (
        audit_events[0].details["phone_fingerprint"] == audit_events[1].details["phone_fingerprint"]
    )
    assert "+971501234567" not in str(audit_events[0].details)


@pytest.mark.asyncio
async def test_no_knowledge_smallest_call_is_allowed_and_arms_terminal_watchdog(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Accepted direct call agent",
        system_prompt="Keep accepted calls bounded.",
        voice_provider="smallest",
        provider_agent_id="accepted-direct-agent",
        provider_revision_id="accepted-direct-revision",
        last_synced_at=datetime.now(UTC),
        sync_status="synced",
        max_call_duration_seconds=30,
    )
    db.add(agent)
    await db.commit()

    provider_call = AsyncMock(return_value="accepted-direct-provider-sid")
    dispatch_watchdog = Mock()
    terminal_watchdog = Mock()
    monkeypatch.setattr(
        calls_endpoint,
        "get_smallest_client",
        lambda: SimpleNamespace(start_outbound_call=provider_call),
    )
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", dispatch_watchdog)
    monkeypatch.setattr(
        call_tasks.reconcile_direct_call_terminal,
        "apply_async",
        terminal_watchdog,
    )
    headers = {**auth_headers, "Idempotency-Key": "direct-terminal-watchdog-0001"}
    payload = {"agent_id": str(agent.id), "to_number": "+971501234567"}

    assert (
        await db.scalar(
            select(func.count())
            .select_from(AgentKnowledgeBinding)
            .where(AgentKnowledgeBinding.agent_id == agent.id)
        )
        == 0
    )
    response = await client.post("/api/v1/calls", headers=headers, json=payload)
    replay = await client.post("/api/v1/calls", headers=headers, json=payload)

    assert response.status_code == 201
    assert replay.status_code == 201
    assert response.json()["status"] == "ringing"
    assert replay.json()["id"] == response.json()["id"]
    provider_call.assert_awaited_once()
    assert provider_call.await_args.kwargs["version_id"] == "accepted-direct-revision"
    terminal_watchdog.assert_called_once()
    scheduled = terminal_watchdog.call_args.kwargs
    assert scheduled["args"] == (response.json()["id"], str(tenant.id))

    db.expire_all()
    stored = await db.get(Call, UUID(response.json()["id"]))
    watchdog = stored.call_metadata["terminal_watchdog"]
    assert watchdog["status"] == "armed"
    assert watchdog["max_call_duration_seconds"] == 30
    assert watchdog["grace_seconds"] == call_tasks.DIRECT_TERMINAL_CALLBACK_GRACE_SECONDS
    assert scheduled["eta"] == datetime.fromisoformat(watchdog["deadline"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phone_number",
    ["+0123456789", "+1234567", "+1234567890123456", "971501234567"],
)
async def test_direct_outbound_call_rejects_invalid_e164(
    client: AsyncClient,
    auth_headers,
    phone_number,
):
    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "invalid-e164-test-0001"},
        json={
            "agent_id": "00000000-0000-0000-0000-000000000001",
            "to_number": phone_number,
        },
    )
    assert response.status_code == 422
