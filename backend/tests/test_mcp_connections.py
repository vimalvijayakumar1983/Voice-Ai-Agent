import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.types import Tool, ToolAnnotations
from sqlalchemy import select

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.integration import Integration
from app.services import mcp_connections as mcp
from app.services.integration_security import IntegrationConfigError, decrypt_integration_config


def config():
    return {
        "url": "https://mcp.example.com/mcp",
        "auth_type": "bearer",
        "credential": "private-test-credential",
        "company_label": "Test company",
    }


def read_tool(**kwargs):
    return Tool(
        name="opening_hours",
        description="Read public opening hours",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=ToolAnnotations(readOnlyHint=True),
        **kwargs,
    )


async def create(client, auth_headers):
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={"name": "ERP MCP", "integration_type": "mcp", "config": config()},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def discover(client, auth_headers, connection, monkeypatch):
    monkeypatch.setattr(
        mcp,
        "discover_tools",
        AsyncMock(
            return_value={
                "tools": [
                    mcp.tool_descriptor(read_tool()),
                    mcp.tool_descriptor(Tool(name="book", inputSchema={"type": "object"})),
                ],
                "latency_ms": 120,
            }
        ),
    )
    response = await client.post(
        f"/api/v1/integrations/{connection['id']}/mcp/test", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_mcp_credentials_catalog_and_no_default_grants(client, auth_headers, db, monkeypatch):
    connection = await create(client, auth_headers)
    assert "private-test-credential" not in json.dumps(connection)
    assert "mcp.example.com" not in json.dumps(connection)
    stored = await db.scalar(select(Integration).where(Integration.id == UUID(connection["id"])))
    assert "private-test-credential" not in json.dumps(stored.config)
    assert (
        decrypt_integration_config(stored.encrypted_config)["credential"] == config()["credential"]
    )
    tested = await discover(client, auth_headers, connection, monkeypatch)
    assert tested["config"]["last_test"]["status"] == "connected"
    assert tested["config"]["allowed_tools"] == []
    assert tested["config"]["agent_ids"] == []
    assert tested["config"]["tools"][0]["read_only"] is True
    assert tested["config"]["tools"][1]["read_only"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", []),
        ("last_test", {"status": "connected"}),
        ("write_enabled", True),
    ],
)
async def test_cannot_forge_mcp_catalog(client, auth_headers, field, value):
    data = config() | {field: value}
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={"name": "test", "integration_type": "mcp", "config": data},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://169.254.169.254/mcp",
        "https://mcp.example.com/mcp?token=secret",
        "https://user:pass@mcp.example.com/mcp",
    ],
)
def test_unsafe_mcp_urls(url):
    with pytest.raises(IntegrationConfigError):
        mcp.validate_mcp_config(config() | {"url": url})


REVIEWED_URL = "https://mcp-trading.13-232-147-135.sslip.io/mcp"


@pytest.mark.parametrize(
    "url",
    [
        REVIEWED_URL + "/",
        REVIEWED_URL + "?token=anything",
        REVIEWED_URL + "#fragment",
        REVIEWED_URL.replace("/mcp", "/other"),
        REVIEWED_URL.replace(".io/", ".io:443/"),
        REVIEWED_URL.replace(".io/", ".io:8443/"),
        REVIEWED_URL.replace("135", "136"),
        REVIEWED_URL.replace("mcp-trading.", "other."),
        REVIEWED_URL.replace("https://", "http://"),
        REVIEWED_URL.replace("https://", "https://user:pass@"),
    ],
)
def test_reviewed_mcp_exception_is_exact(url):
    with pytest.raises(IntegrationConfigError):
        mcp.validate_mcp_config(config() | {"url": url})


async def test_reviewed_url_only_accepted_for_mcp(client, auth_headers):
    from app.services.integration_security import validate_integration_config_urls

    mcp.validate_mcp_config(config() | {"url": REVIEWED_URL})
    with pytest.raises(IntegrationConfigError):
        validate_integration_config_urls({"url": REVIEWED_URL})
    with pytest.raises(IntegrationConfigError):
        validate_integration_config_urls({"nested": {"url": REVIEWED_URL}}, mcp_endpoint=True)
    for kind, expected in (("mcp", 201), ("webhook", 422), ("his_api", 422), ("vav_crm", 422)):
        response = await client.post(
            "/api/v1/integrations",
            headers=auth_headers,
            json={
                "name": "Reviewed endpoint",
                "integration_type": kind,
                "config": config() | {"url": REVIEWED_URL},
            },
        )
        assert response.status_code == expected, response.text


@pytest.mark.parametrize("addresses", [("93.184.216.34",), ("13.232.147.135", "1.1.1.1")])
async def test_reviewed_endpoint_rejects_dns_change(monkeypatch, addresses):
    from app.tasks import webhook_tasks

    monkeypatch.setattr(
        webhook_tasks, "_resolve_public_destination", AsyncMock(return_value=addresses)
    )
    transport = AsyncMock()
    monkeypatch.setattr(webhook_tasks, "_PinnedAsyncHTTPTransport", transport)
    with pytest.raises(mcp.MCPError):
        async with mcp.mcp_session(config() | {"url": REVIEWED_URL}):
            pytest.fail("DNS change must not open a session")
    transport.assert_not_called()


async def test_reviewed_endpoint_still_checks_public_dns_and_tls_transport(monkeypatch):
    from app.tasks import webhook_tasks

    calls = []

    def transport(host, address):
        calls.append((host, address))
        return httpx.MockTransport(lambda request: httpx.Response(401))

    resolver = AsyncMock(return_value=("13.232.147.135",))
    monkeypatch.setattr(webhook_tasks, "_resolve_public_destination", resolver)
    monkeypatch.setattr(webhook_tasks, "_PinnedAsyncHTTPTransport", transport)
    with pytest.raises(mcp.MCPError):
        await mcp.discover_tools(config() | {"url": REVIEWED_URL})
    resolver.assert_awaited_once_with(REVIEWED_URL)
    assert calls == [("mcp-trading.13-232-147-135.sslip.io", "13.232.147.135")]
    resolver.side_effect = IntegrationConfigError("non-public DNS")
    calls.clear()
    with pytest.raises(mcp.MCPError):
        await mcp.discover_tools(config() | {"url": REVIEWED_URL})
    assert not calls


async def test_permissions_binding_and_revocation(client, auth_headers, tenant, db, monkeypatch):
    agent = Agent(tenant_id=tenant.id, name="Tool-loop QA", system_prompt="Test")
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        runtime_config={"voice_runtime": "inworld_realtime"},
    )
    db.add(profile)
    await db.commit()
    connection = await discover(
        client, auth_headers, await create(client, auth_headers), monkeypatch
    )
    path = f"/api/v1/integrations/{connection['id']}"
    grants = {
        "allowed_tools": ["opening_hours"],
        "agent_ids": [str(agent.id)],
        "public_data_approved": True,
    }
    response = await client.patch(path, headers=auth_headers, json={"config": grants})
    assert response.status_code == 200, response.text
    await db.refresh(profile)
    profile.runtime_config = {"voice_runtime": "inworld_realtime", "inworld_single_pass": True}
    await db.commit()
    response = await client.patch(path, headers=auth_headers, json={"config": grants})
    assert response.status_code == 422
    response = await client.patch(
        path, headers=auth_headers, json={"config": {"credential": "replacement-token"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["config"]["agent_ids"] == []
    assert response.json()["config"]["last_test"]["status"] == "untested"


async def test_unknown_write_tool_and_foreign_agent_rejected(client, auth_headers, monkeypatch):
    connection = await discover(
        client, auth_headers, await create(client, auth_headers), monkeypatch
    )
    for grant in (
        {"allowed_tools": ["book"]},
        {"allowed_tools": ["missing"]},
        {"allowed_tools": ["opening_hours"], "agent_ids": [str(uuid4())]},
    ):
        response = await client.patch(
            f"/api/v1/integrations/{connection['id']}",
            headers=auth_headers,
            json={"config": grant | {"public_data_approved": True}},
        )
        assert response.status_code == 422


async def test_failed_discovery_clears_grants_without_leaking_error(
    client, auth_headers, monkeypatch
):
    connection = await create(client, auth_headers)
    monkeypatch.setattr(mcp, "discover_tools", AsyncMock(side_effect=mcp.MCPError("Safe failure")))
    response = await client.post(
        f"/api/v1/integrations/{connection['id']}/mcp/test", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["config"]["last_test"]["status"] == "failed"
    assert "private-test-credential" not in response.text


async def test_discovery_authenticated_and_tenant_scoped(client, auth_headers):
    connection = await create(client, auth_headers)
    response = await client.post(f"/api/v1/integrations/{connection['id']}/mcp/test")
    assert response.status_code == 401
    response = await client.post(f"/api/v1/integrations/{uuid4()}/mcp/test", headers=auth_headers)
    assert response.status_code == 404


async def test_sdk_handshake_discovery_and_read_tool_use_pinned_transport(monkeypatch):
    from app.tasks import webhook_tasks

    methods = []

    async def handler(request):
        assert request.headers["authorization"] == "Bearer private-test-credential"
        assert request.url.host == "mcp.example.com"
        if request.method in {"GET", "DELETE"}:
            return httpx.Response(405)
        body = json.loads(request.content)
        methods.append(body["method"])
        if "id" not in body:
            return httpx.Response(202)
        result = {}
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif body["method"] == "tools/list":
            result = {"tools": [read_tool().model_dump(exclude_none=True)]}
        elif body["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "Open 9am to 5pm"}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    resolver = AsyncMock(return_value=("93.184.216.34",))
    monkeypatch.setattr(webhook_tasks, "_resolve_public_destination", resolver)
    monkeypatch.setattr(
        webhook_tasks,
        "_PinnedAsyncHTTPTransport",
        lambda hostname, address: httpx.MockTransport(handler),
    )
    discovered = await mcp.discover_tools(config())
    assert len(discovered["tools"]) == 1
    assert "tools/call" not in methods
    result = await mcp.call_read_tool(
        config()
        | {
            "allowed_tools": ["opening_hours"],
            "tools": discovered["tools"],
            "public_data_approved": True,
            "last_test": {"status": "connected"},
        },
        discovered["tools"][0],
        {},
    )
    assert result["status"] == "ok"
    assert "tools/call" in methods
    assert resolver.await_count == 2


async def test_changed_schema_and_invalid_arguments_never_execute(monkeypatch):
    session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[read_tool()], nextCursor=None)),
        call_tool=AsyncMock(),
    )

    @asynccontextmanager
    async def session_context(_config):
        yield session

    monkeypatch.setattr(mcp, "mcp_session", session_context)
    descriptor = mcp.tool_descriptor(read_tool())
    cfg = config() | {"allowed_tools": ["opening_hours"]}
    with pytest.raises(mcp.MCPError):
        await mcp.call_read_tool(cfg, descriptor, {"unexpected": "value"})
    with pytest.raises(mcp.MCPError):
        await mcp.call_read_tool(cfg, descriptor | {"schema_hash": "outdated"}, {})
    session.call_tool.assert_not_awaited()


def test_safety_annotations_and_external_schema_references():
    assert not mcp.tool_descriptor(Tool(name="unknown", inputSchema={"type": "object"}))[
        "read_only"
    ]
    with pytest.raises(mcp.MCPError):
        mcp.tool_descriptor(
            Tool(
                name="external",
                inputSchema={
                    "type": "object",
                    "properties": {"x": {"$ref": "https://internal.example/schema"}},
                },
            )
        )


async def test_mcp_member_cannot_discover_or_edit(client, auth_headers, user, db):
    connection = await create(client, auth_headers)
    user.role = "member"
    await db.commit()
    response = await client.post(
        f"/api/v1/integrations/{connection['id']}/mcp/test", headers=auth_headers
    )
    assert response.status_code == 403
    response = await client.patch(
        f"/api/v1/integrations/{connection['id']}",
        headers=auth_headers,
        json={"config": {"public_data_approved": True}},
    )
    assert response.status_code == 403


async def test_discovery_concurrent_edit_is_not_overwritten(client, auth_headers, monkeypatch):
    connection = await create(client, auth_headers)

    async def concurrent_edit(_config):
        updated = await client.patch(
            f"/api/v1/integrations/{connection['id']}",
            headers=auth_headers,
            json={"config": {"credential": "concurrent-credential"}},
        )
        assert updated.status_code == 200
        return {"tools": [mcp.tool_descriptor(read_tool())], "latency_ms": 1}

    monkeypatch.setattr(mcp, "discover_tools", concurrent_edit)
    response = await client.post(
        f"/api/v1/integrations/{connection['id']}/mcp/test", headers=auth_headers
    )
    assert response.status_code == 409


@pytest.mark.parametrize("kind", ["redirect", "oversize", "compressed", "private_dns"])
async def test_sdk_network_failures_are_bounded_and_safe(monkeypatch, kind):
    from app.tasks import webhook_tasks

    requests = []

    async def handler(request):
        requests.append(str(request.url))
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://evil.example.com/mcp"})
        if kind == "compressed":
            # Stream raw bytes so HTTPX does not eagerly attempt decompression.
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not-allowed")
            )
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"x" * (mcp.MAX_BYTES + 1)
        )

    resolver = AsyncMock(return_value=("93.184.216.34",))
    if kind == "private_dns":
        resolver.side_effect = IntegrationConfigError("private-test-credential must not leak")
    monkeypatch.setattr(webhook_tasks, "_resolve_public_destination", resolver)
    monkeypatch.setattr(
        webhook_tasks, "_PinnedAsyncHTTPTransport", lambda *args: httpx.MockTransport(handler)
    )
    with pytest.raises(mcp.MCPError) as failure:
        await mcp.discover_tools(config())
    assert "private-test-credential" not in str(failure.value)
    assert all("evil.example.com" not in url for url in requests)
    if kind == "private_dns":
        assert not requests


async def test_runtime_adapter_revocation_and_content_free_metrics(monkeypatch):
    from app.livekit_runtime import mcp_tools

    descriptor = mcp.tool_descriptor(read_tool())
    cfg = config() | {"tools": [descriptor], "allowed_tools": ["opening_hours"]}

    @asynccontextmanager
    async def fake_db():
        yield object()

    monkeypatch.setattr(mcp_tools, "async_session_factory", fake_db)
    auth = AsyncMock(return_value=cfg)
    call = AsyncMock(return_value={"data": "sensitive-result", "status": "ok"})
    monkeypatch.setattr(mcp_tools, "authorized_runtime_config", auth)
    monkeypatch.setattr(mcp_tools, "call_read_tool", call)
    metrics = {}
    tool = mcp_tools._make_tool(uuid4(), uuid4(), uuid4(), "Test", descriptor, metrics)
    result = json.loads(await tool({}))
    assert result["untrusted_tool_data"]["status"] == "ok"
    assert "sensitive-result" not in json.dumps(metrics)
    assert auth.await_count == 2
    auth.side_effect = mcp.MCPError("revoked")
    result = json.loads(await tool({}))
    assert result["status"] == "unavailable"
    assert call.await_count == 1
    metrics["mcp_lookup_count"] = 50
    assert json.loads(await tool({}))["status"] == "unavailable"
    assert call.await_count == 1
