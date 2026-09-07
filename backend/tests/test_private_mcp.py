"""Private MCP requires durable identity, explicit grants and isolated company context."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.audit import AuditEvent
from app.models.call import Call
from app.models.integration import Integration
from app.models.user import User
from app.services import mcp_connections as mcp
from app.services.browser_access import require_call_access
from app.services.integration_security import (
    IntegrationConfigError,
    prepare_integration_config_storage,
)
from tests.conftest import test_session_factory as session_factory
from tests.test_mcp_connections import config, create, discover, read_tool


def private_config(user_id=None, agent_id=None):
    descriptor = mcp.tool_descriptor(read_tool())
    return config() | {
        "data_access_mode": "private_staff",
        "private_data_approved": True,
        "upstream_scope_approved": True,
        "allowed_user_ids": [str(user_id or uuid4())],
        "agent_ids": [str(agent_id or uuid4())],
        "allowed_tools": [descriptor["name"]],
        "tools": [descriptor],
        "last_test": {"status": "connected"},
    }


async def setup_private(db, tenant, user):
    agent = Agent(tenant_id=tenant.id, name="Private ERP", system_prompt="Read authorised tools")
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=False,
        status="draft",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        assigned_numbers=[],
        runtime_config={
            "staff_browser_only": True,
            "knowledge_source_mode": "tools_only",
            "voice_runtime": "inworld_realtime",
        },
    )
    cfg = private_config(user.id, agent.id)
    public, encrypted = prepare_integration_config_storage(cfg, "mcp")
    integration = Integration(
        id=uuid4(),
        tenant_id=tenant.id,
        name="Private ERP",
        integration_type="mcp",
        config=public,
        encrypted_config=encrypted,
    )
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        provider="livekit_webrtc",
        from_number="browser",
        to_number="agent",
        status="in_progress",
        call_metadata={
            "channel": "browser",
            "staff_browser_only": True,
            "browser_user_id": str(user.id),
            "private_mcp": True,
            "private_mcp_integration_id": str(integration.id),
        },
    )
    db.add_all([profile, integration, call])
    await db.commit()
    return agent, profile, integration, call, cfg


@pytest.mark.parametrize(
    "patch",
    [
        {"auth_type": "none"},
        {"allowed_user_ids": []},
        {"private_data_approved": False},
        {"upstream_scope_approved": False},
        {"allowed_user_ids": ["invalid"]},
        {"data_access_mode": "anything"},
        {"private_data_approved": "true"},
    ],
)
def test_private_config_fail_closed(patch):
    with pytest.raises(IntegrationConfigError):
        mcp.validate_mcp_config(private_config() | patch)


def test_mode_change_does_not_reinterpret_grants():
    cfg = private_config()
    updated = mcp.prepare_mcp_update(
        cfg, {"data_access_mode": "public", "public_data_approved": True}
    )
    assert updated["allowed_tools"] == updated["agent_ids"] == updated["allowed_user_ids"] == []
    assert updated["public_data_approved"] is False
    updated = mcp.prepare_mcp_update(cfg, {"company_label": "Other company"})
    assert updated["last_test"]["status"] == "untested"
    assert updated["upstream_scope_approved"] is False


async def test_private_api_grants_and_audit(client, auth_headers, db, tenant, user, monkeypatch):
    agent, _, _, _, _ = await setup_private(db, tenant, user)
    connection = await discover(
        client, auth_headers, await create(client, auth_headers), monkeypatch
    )
    path = f"/api/v1/integrations/{connection['id']}"
    changed = await client.patch(
        path, headers=auth_headers, json={"config": {"data_access_mode": "private_staff"}}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["config"]["allowed_user_ids"] == []
    grants = {
        key: value
        for key, value in private_config(user.id, agent.id).items()
        if key in mcp.CLIENT_FIELDS
        and key not in {"url", "credential", "auth_type", "company_label"}
    }
    result = await client.patch(path, headers=auth_headers, json={"config": grants})
    assert result.status_code == 200, result.text
    assert result.json()["config"]["data_access_mode"] == "private_staff"
    assert "private-test-credential" not in result.text
    events = (
        await db.scalars(select(AuditEvent).where(AuditEvent.action == "mcp.policy_updated"))
    ).all()
    assert events[-1].actor_user_id == user.id
    assert events[-1].details["allowed_user_ids"] == [str(user.id)]
    assert "credential" not in json.dumps([e.details for e in events])
    staff = await client.get("/api/v1/integrations/mcp/staff", headers=auth_headers)
    assert staff.status_code == 200 and staff.json()[0]["id"] == str(user.id)


@pytest.mark.parametrize("change", ["public", "phone", "kb", "recording", "foreign_user"])
async def test_private_grant_rejects_incompatible_profile(db, tenant, user, change):
    agent, profile, _, _, cfg = await setup_private(db, tenant, user)
    if change == "public":
        profile.runtime_config = {**profile.runtime_config, "staff_browser_only": False}
    elif change == "phone":
        profile.enabled = True
    elif change == "kb":
        profile.runtime_config = {
            **profile.runtime_config,
            "knowledge_source_mode": "knowledge_base",
        }
    elif change == "recording":
        profile.runtime_config = {
            **profile.runtime_config,
            "diagnostic_recording_mode": "livekit_egress_explicit_consent",
        }
    else:
        cfg["allowed_user_ids"] = [str(uuid4())]
    await db.commit()
    with pytest.raises(IntegrationConfigError):
        await mcp.validate_agent_grants(db, tenant.id, cfg)


@pytest.mark.parametrize(
    "change",
    [
        "missing_call",
        "phone_call",
        "unlisted",
        "inactive",
        "member",
        "ended",
        "foreign_tenant",
        "unpinned",
        "wrong_pin",
    ],
)
async def test_private_runtime_denies_before_network(db, tenant, user, change):
    agent, _, integration, call, _ = await setup_private(db, tenant, user)
    args = {"call_id": call.id}
    tenant_id = tenant.id
    if change == "missing_call":
        args = {}
    elif change == "phone_call":
        call.provider = "livekit_sip"
    elif change == "unlisted":
        call.call_metadata = {**call.call_metadata, "browser_user_id": str(uuid4())}
    elif change == "inactive":
        user.is_active = False
    elif change == "member":
        user.role = "member"
    elif change == "ended":
        call.status = "completed"
    elif change == "unpinned":
        call.call_metadata = {
            k: v for k, v in call.call_metadata.items() if not k.startswith("private_mcp")
        }
    elif change == "wrong_pin":
        call.call_metadata = {**call.call_metadata, "private_mcp_integration_id": str(uuid4())}
    else:
        tenant_id = uuid4()
    await db.commit()
    with pytest.raises((ValueError, mcp.MCPError)):
        await mcp.authorized_runtime_config(db, tenant_id, agent.id, integration.id, **args)


@pytest.mark.parametrize("revoke", [False, True])
async def test_private_lookup_success_and_midflight_revocation(
    db, tenant, user, monkeypatch, revoke
):
    from app.livekit_runtime import mcp_tools

    agent, profile, integration, call, cfg = await setup_private(db, tenant, user)
    monkeypatch.setattr(mcp_tools, "async_session_factory", session_factory)
    metrics = {}
    tools = await mcp_tools.load_mcp_tools(agent, profile, metrics, call_id=call.id)
    assert len(tools) == 1 and metrics["mcp_private_mode"] is True
    await db.refresh(call)
    assert call.call_metadata["private_mcp_integration_id"] == str(integration.id)

    async def lookup(*_args):
        if revoke:
            new_cfg = {**cfg, "allowed_tools": []}
            integration.config, integration.encrypted_config = prepare_integration_config_storage(
                new_cfg, "mcp"
            )
            await db.commit()
        return {"status": "ok", "data": "confidential-test-answer"}

    monkeypatch.setattr(mcp_tools, "call_read_tool", lookup)
    result = await tools[0]({})
    assert ("confidential-test-answer" in result) is not revoke
    events = (await db.scalars(select(AuditEvent))).all()
    assert "mcp.private_lookup.started" in [e.action for e in events]
    assert ("mcp.private_lookup.unavailable" if revoke else "mcp.private_lookup.succeeded") in [
        e.action for e in events
    ]
    assert "confidential-test-answer" not in json.dumps(metrics)
    assert "confidential-test-answer" not in json.dumps([e.details for e in events])


async def test_private_audit_failure_prevents_network(db, tenant, user, monkeypatch):
    from app.livekit_runtime import mcp_tools

    agent, profile, _, call, _ = await setup_private(db, tenant, user)
    monkeypatch.setattr(mcp_tools, "async_session_factory", session_factory)
    tools = await mcp_tools.load_mcp_tools(agent, profile, {}, call_id=call.id)
    monkeypatch.setattr(
        mcp_tools, "_private_audit", AsyncMock(side_effect=RuntimeError("db offline"))
    )
    remote = AsyncMock()
    monkeypatch.setattr(mcp_tools, "call_read_tool", remote)
    assert json.loads(await tools[0]({}))["status"] == "unavailable"
    remote.assert_not_called()


async def test_private_multi_connection_isolation(db, tenant, user, monkeypatch):
    from app.livekit_runtime import mcp_tools

    agent, profile, _, call, cfg = await setup_private(db, tenant, user)
    public, encrypted = prepare_integration_config_storage(
        {**cfg, "company_label": "Other company"}, "mcp"
    )
    db.add(
        Integration(
            tenant_id=tenant.id,
            name="Other",
            integration_type="mcp",
            config=public,
            encrypted_config=encrypted,
        )
    )
    await db.commit()
    monkeypatch.setattr(mcp_tools, "async_session_factory", session_factory)
    metrics = {}
    assert await mcp_tools.load_mcp_tools(agent, profile, metrics, call_id=call.id) == []
    assert metrics["mcp_setup_unavailable"] is True


async def test_private_history_not_visible_to_other_admin(client, auth_headers, db, tenant, user):
    _, _, _, call, _ = await setup_private(db, tenant, user)
    call.call_metadata = {**call.call_metadata, "private_mcp": True}
    other = User(
        tenant_id=tenant.id,
        email="other@test.com",
        full_name="Other Admin",
        hashed_password="none",
        role="admin",
    )
    db.add(other)
    await db.commit()
    await require_call_access(db, user, call.id)
    with pytest.raises(HTTPException):
        await require_call_access(db, other, call.id)
    from app.core.security import create_access_token

    other_headers = {
        "Authorization": "Bearer " + create_access_token(other.id, tenant.id, other.role)
    }
    for suffix in ("", "/transcript", "/summary"):
        response = await client.get(f"/api/v1/calls/{call.id}{suffix}", headers=other_headers)
        assert response.status_code == 404, response.text
    listed = await client.get("/api/v1/calls", headers=other_headers)
    assert listed.status_code == 200 and not listed.json()


async def test_private_connection_hidden_from_member(client, auth_headers, db, tenant, user):
    await setup_private(db, tenant, user)
    user.role = "member"
    await db.commit()
    assert (await client.get("/api/v1/integrations", headers=auth_headers)).json() == []
    assert (
        await client.get("/api/v1/integrations/mcp/staff", headers=auth_headers)
    ).status_code == 403


async def test_private_admission_pins_connection_and_rejects_unlisted(db, tenant, user):
    agent, _, integration, _, _ = await setup_private(db, tenant, user)
    admitted = await mcp.private_browser_admission(
        db, tenant_id=tenant.id, agent_id=agent.id, user_id=user.id
    )
    assert admitted == {"private_mcp": True, "private_mcp_integration_id": str(integration.id)}
    with pytest.raises(IntegrationConfigError):
        await mcp.private_browser_admission(
            db, tenant_id=tenant.id, agent_id=agent.id, user_id=uuid4()
        )


async def test_private_reanalysis_denied(client, auth_headers, db, tenant, user):
    _, _, _, call, _ = await setup_private(db, tenant, user)
    response = await client.post(f"/api/v1/calls/{call.id}/reanalyze", headers=auth_headers)
    assert response.status_code == 409
    assert "private MCP" in response.json()["detail"]


async def test_private_completion_never_sends_transcript_to_summary_provider(
    db, tenant, user, monkeypatch
):
    from app.ai.conversation import ConversationEngine, conversation_engine
    from app.models.call import CallTranscript
    from app.tasks.call_tasks import _process_completed_call_async

    _, _, _, call, _ = await setup_private(db, tenant, user)
    call.status = "completed"
    db.add(
        CallTranscript(
            tenant_id=tenant.id,
            call_id=call.id,
            full_text="User: What is the balance?\nAssistant: Confidential fixture.",
            turns=[{"role": "user", "content": "What is the balance?"}],
        )
    )
    await db.commit()
    monkeypatch.setattr("app.core.database.async_session_factory", session_factory)
    summary = AsyncMock()
    monkeypatch.setattr(ConversationEngine, "generate_call_summary", summary)
    monkeypatch.setattr(conversation_engine, "generate_call_summary", summary)
    assert (
        await _process_completed_call_async(str(call.id), str(tenant.id), force_analysis=True)
        is None
    )
    summary.assert_not_called()


@pytest.mark.parametrize("allowed", [True, False])
async def test_private_browser_endpoint_pins_or_rejects_before_provider(
    client, auth_headers, db, tenant, user, monkeypatch, allowed
):
    from uuid import UUID

    from tests.test_staff_browser import setup_staff, wire_probes

    agent, _ = await setup_staff(db, tenant, monkeypatch)
    probe = wire_probes(monkeypatch)
    actor = user
    if not allowed:
        actor = User(
            tenant_id=tenant.id,
            email="approved@test.com",
            full_name="Approved",
            hashed_password="none",
            role="admin",
        )
        db.add(actor)
        await db.flush()
    public, encrypted = prepare_integration_config_storage(
        private_config(actor.id, agent.id), "mcp"
    )
    connection = Integration(
        tenant_id=tenant.id,
        name="Private",
        integration_type="mcp",
        config=public,
        encrypted_config=encrypted,
    )
    db.add(connection)
    await db.commit()
    response = await client.post(
        f"/api/v1/agents/{agent.id}/livekit/session",
        headers=auth_headers | {"Idempotency-Key": "private-browser-test-0001"},
        json={"variables": {}},
    )
    if not allowed:
        assert response.status_code == 403, response.text
        probe.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        call = await db.get(Call, UUID(response.json()["call_id"]))
        assert call.call_metadata["private_mcp"] is True
        assert call.call_metadata["private_mcp_integration_id"] == str(connection.id)
