"""Staff browser admission is independent from SIP and cannot confer ERP grants."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.api.v1.endpoints import agents as endpoint
from app.livekit_runtime import worker
from app.livekit_runtime.browser_session import LiveKitBrowserSession, LiveKitBrowserSessionProvider
from app.models.agent import AgentKnowledgeBinding, AgentRuntimeProfile
from app.models.call import Call
from app.schemas.runtime import RuntimeProfileUpdate
from app.services.browser_access import validate_staff_call
from app.services.mcp_connections import runtime_compatible
from app.telephony.livekit_provider import LiveKitSIPProvider
from tests.conftest import test_session_factory as session_factory
from tests.test_livekit_browser_session import _configure_platform, _configured_browser_agent


async def setup_staff(db, tenant, monkeypatch):
    _configure_platform(monkeypatch)
    agent = await _configured_browser_agent(db, tenant)
    await db.execute(
        delete(AgentKnowledgeBinding).where(AgentKnowledgeBinding.agent_id == agent.id)
    )
    profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent.id)
    )
    profile.runtime_config = {
        "staff_browser_only": True,
        "knowledge_source_mode": "tools_only",
        "voice_runtime": "inworld_realtime",
        "inworld_single_pass": False,
    }
    await db.commit()
    return agent, profile


def wire_probes(monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(endpoint, "_verify_native_browser_capability", probe)
    monkeypatch.setattr(LiveKitSIPProvider, "verify_worker", AsyncMock())

    async def create(_self, **kwargs):
        return LiveKitBrowserSession(
            access_token="test-token",
            room_name=f"vav-browser-{kwargs['call_id']}",
            participant_identity=f"browser-{kwargs['call_id']}",
            dispatch_id="AD_test",
            expires_in=120,
        )

    monkeypatch.setattr(LiveKitBrowserSessionProvider, "create_session", create)
    monkeypatch.setattr(worker, "async_session_factory", session_factory)
    return probe


async def test_staff_owner_browser_without_phone_or_kb(
    client, auth_headers, user, tenant, db, monkeypatch
):
    agent, profile = await setup_staff(db, tenant, monkeypatch)
    wire_probes(monkeypatch)
    assert profile.enabled is False and profile.assigned_numbers == []
    assert runtime_compatible(profile)
    response = await client.post(
        f"/api/v1/agents/{agent.id}/livekit/session",
        headers=auth_headers | {"Idempotency-Key": "staff-browser-test-0001"},
        json={"variables": {}},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    call = await db.get(Call, UUID(data["call_id"]))
    assert call.call_metadata["browser_user_id"] == str(user.id)
    assert call.call_metadata["staff_browser_only"] is True
    assert call.call_metadata["runtime"]["knowledge_serving_revision_id"] is None
    loaded = await worker._load_browser_runtime(
        tenant_id=tenant.id,
        agent_id=agent.id,
        call_id=call.id,
        room_name=data["room_name"],
        participant_identity=data["participant_identity"],
    )
    assert loaded[4]["knowledge_source_count"] == 0
    assert loaded[5].revision_id is None
    await worker._open_browser_call(
        model=loaded[0],
        profile=loaded[1],
        call_id=call.id,
        room_name=data["room_name"],
        participant_identity=data["participant_identity"],
        served_configuration=loaded[4],
        knowledge_pin=loaded[5],
    )
    user.role = "member"
    await db.commit()
    with pytest.raises(ValueError, match="revoked"):
        await validate_staff_call(db, tenant_id=tenant.id, agent_id=agent.id, call_id=call.id)
    for suffix in ("", "/transcript", "/summary", "/recording"):
        r = await client.get(f"/api/v1/calls/{call.id}{suffix}", headers=auth_headers)
        assert r.status_code == 404, r.text
    r = await client.get("/api/v1/calls", headers=auth_headers)
    assert r.status_code == 200
    assert all(item["id"] != str(call.id) for item in r.json())


@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_non_staff_cannot_start_browser(
    client, auth_headers, user, tenant, db, monkeypatch, role
):
    agent, _ = await setup_staff(db, tenant, monkeypatch)
    probe = wire_probes(monkeypatch)
    user.role = role
    await db.commit()
    response = await client.post(
        f"/api/v1/agents/{agent.id}/livekit/session",
        headers=auth_headers | {"Idempotency-Key": "staff-browser-test-0002"},
        json={"variables": {"role": "owner"}},
    )
    assert response.status_code == 403, response.text
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"assigned_numbers": ["+14145551234"]},
        {"knowledge_turn_mode": "single_pass_experimental"},
        {"voice_runtime": "pipeline"},
        {"diagnostic_recording_mode": "livekit_egress_explicit_consent"},
        {"staff_browser_only": False},
    ],
)
def test_staff_mode_invalid_combinations(override):
    values = dict(
        staff_browser_only=True,
        knowledge_source_mode="tools_only",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
        voice_runtime="inworld_realtime",
    )
    with pytest.raises(ValidationError):
        RuntimeProfileUpdate(**(values | override))


async def test_staff_mode_still_blocks_phone_readiness(db, tenant, monkeypatch):
    from app.api.v1.endpoints.runtime import runtime_readiness

    agent, profile = await setup_staff(db, tenant, monkeypatch)
    blockers, checks = await runtime_readiness(db, agent, profile)
    assert checks["phone_access_permitted"] is False
    assert any("does not permit phone" in item for item in blockers)


async def test_staff_policy_requires_real_user_and_browser_reservation(db, tenant, monkeypatch):
    agent, _ = await setup_staff(db, tenant, monkeypatch)
    with pytest.raises(ValueError):
        await validate_staff_call(db, tenant_id=tenant.id, agent_id=agent.id, call_id=uuid4())


def test_disabled_staff_runtime_is_not_mcp_eligible():
    profile = SimpleNamespace(
        enabled=False,
        status="inactive",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        runtime_config={"voice_runtime": "inworld_realtime", "staff_browser_only": True},
    )
    assert not runtime_compatible(profile)


async def test_old_client_cannot_silently_remove_staff_policy(
    client, auth_headers, tenant, db, monkeypatch
):
    agent, _ = await setup_staff(db, tenant, monkeypatch)
    response = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "telephony_provider": "livekit_sip",
            "primary_speech_provider": "inworld",
            "llm_provider": "inworld",
            "llm_model": "openai/gpt-4o-mini",
            "voice_runtime": "inworld_realtime",
        },
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("change", ["disabled", "standard", "knowledge", "foreign_actor"])
async def test_reservation_rechecked_before_worker_join(
    client, auth_headers, tenant, db, monkeypatch, change
):
    agent, profile = await setup_staff(db, tenant, monkeypatch)
    wire_probes(monkeypatch)
    response = await client.post(
        f"/api/v1/agents/{agent.id}/livekit/session",
        headers=auth_headers | {"Idempotency-Key": "staff-browser-revocation-0001"},
        json={"variables": {}},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    call = await db.get(Call, UUID(data["call_id"]))
    if change == "disabled":
        profile.status = "inactive"
    elif change == "standard":
        profile.runtime_config = {**profile.runtime_config, "staff_browser_only": False}
    elif change == "knowledge":
        profile.runtime_config = {
            **profile.runtime_config,
            "knowledge_source_mode": "knowledge_base",
        }
    else:
        call.call_metadata = {**call.call_metadata, "browser_user_id": str(uuid4())}
    await db.commit()
    with pytest.raises((ValueError, RuntimeError)):
        await worker._load_browser_runtime(
            tenant_id=tenant.id,
            agent_id=agent.id,
            call_id=call.id,
            room_name=data["room_name"],
            participant_identity=data["participant_identity"],
        )


@pytest.mark.parametrize("revoke_after_lookup", [False, True])
async def test_staff_tool_rechecks_role_before_releasing_result(monkeypatch, revoke_after_lookup):
    import json
    from contextlib import asynccontextmanager

    from app.livekit_runtime import mcp_tools
    from app.services import browser_access
    from app.services.mcp_connections import tool_descriptor
    from tests.test_mcp_connections import config, read_tool

    descriptor = tool_descriptor(read_tool())
    cfg = config() | {"tools": [descriptor], "allowed_tools": [descriptor["name"]]}

    @asynccontextmanager
    async def fake_db():
        yield object()

    monkeypatch.setattr(mcp_tools, "async_session_factory", fake_db)
    auth = AsyncMock(return_value=cfg)
    lookup = AsyncMock(return_value={"data": "must-not-leak"})
    staff = AsyncMock(
        side_effect=[None, ValueError("revoked")] if revoke_after_lookup else ValueError("revoked")
    )
    monkeypatch.setattr(mcp_tools, "authorized_runtime_config", auth)
    monkeypatch.setattr(mcp_tools, "call_read_tool", lookup)
    monkeypatch.setattr(browser_access, "validate_staff_call", staff)
    call_id = uuid4()
    tool = mcp_tools._make_tool(
        uuid4(), uuid4(), uuid4(), "Test", descriptor, {}, staff_call_id=call_id, call_id=call_id
    )
    result = await tool({})
    assert json.loads(result)["status"] == "unavailable"
    assert "must-not-leak" not in result
    assert lookup.await_count == int(revoke_after_lookup)
    if revoke_after_lookup:
        assert auth.await_args.kwargs["call_id"] == call_id
