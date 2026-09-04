"""Outbound call compliance tests."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.api.v1.endpoints import calls as calls_endpoint
from app.models.agent import Agent, AgentKnowledgeBinding
from app.models.audit import AuditEvent
from app.models.call import Call
from app.models.compliance import DncEntry
from app.services.phone_numbers import normalize_e164, tenant_phone_dnc_lock
from app.tasks import call_tasks
from tests.conftest import test_session_factory as session_factory


def test_e164_normalization_handles_common_import_formats():
    assert normalize_e164("+971 (50) 123-4567") == "+971501234567"
    assert normalize_e164("00971 50 123 4567") == "+971501234567"
    assert normalize_e164("0501234567") is None


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
