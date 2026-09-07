"""Tenant-owned MCP connections. No remote code, writes, or implicit tool grants.

Use the official MCP SDK over HTTPS Streamable HTTP. Destination pinning reuses
the tested webhook network boundary; redirects and environment proxies stay off.
Server annotations are hints, not authorization: an administrator must additionally
approve each tool under either public-safe or named-staff private read-only policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.integration import Integration
from app.services.integration_security import (
    REVIEWED_MCP_DESTINATIONS,
    IntegrationConfigError,
    load_integration_config,
    validate_mcp_https_url,
)

CLIENT_FIELDS = {
    "url",
    "auth_type",
    "credential",
    "timeout_seconds",
    "company_label",
    "allowed_tools",
    "agent_ids",
    "public_data_approved",
    "data_access_mode",
    "private_data_approved",
    "upstream_scope_approved",
    "allowed_user_ids",
}
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_TOOLS = 100
MAX_BYTES = 1_000_000


def private_mcp(config: dict) -> bool:
    return config.get("data_access_mode", "public") == "private_staff"


class MCPError(ValueError):
    """Safe, content-free error; never expose the provider exception."""


def validate_mcp_config(config: dict) -> None:
    validate_mcp_https_url(config.get("url", ""))
    if urlsplit(config["url"]).query:
        raise IntegrationConfigError("MCP URLs cannot contain query parameters; use bearer auth")
    if config.get("auth_type", "none") not in {"none", "bearer"}:
        raise IntegrationConfigError("MCP authentication must be none or bearer")
    credential = config.get("credential", "")
    if config.get("auth_type") == "bearer" and (
        not isinstance(credential, str)
        or not 8 <= len(credential) <= 4096
        or any(c in credential for c in "\r\n")
    ):
        raise IntegrationConfigError("A valid MCP bearer credential is required")
    timeout = config.get("timeout_seconds", 10)
    if type(timeout) is not int or not 2 <= timeout <= 30:
        raise IntegrationConfigError("MCP timeout must be 2–30 seconds")
    label = config.get("company_label", "")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 160:
        raise IntegrationConfigError("MCP company scope label is required (maximum 160 characters)")
    if config.get("data_access_mode", "public") not in {"public", "private_staff"}:
        raise IntegrationConfigError("Unsupported MCP data access mode")
    for key in ("allowed_tools", "agent_ids", "allowed_user_ids"):
        values = config.get(key, [])
        if (
            not isinstance(values, list)
            or len(values) > MAX_TOOLS
            or any(not isinstance(value, str) for value in values)
            or len(values) != len(set(values))
        ):
            raise IntegrationConfigError(f"Invalid MCP {key}")
    for key in ("public_data_approved", "private_data_approved", "upstream_scope_approved"):
        if type(config.get(key, False)) is not bool:
            raise IntegrationConfigError("MCP approval must be a boolean")
    if private_mcp(config) and config.get("auth_type") != "bearer":
        raise IntegrationConfigError(
            "Private MCP access requires a company-scoped bearer credential"
        )
    if config.get("allowed_tools") or config.get("agent_ids"):
        if config.get("last_test", {}).get("status") != "connected":
            raise IntegrationConfigError("Test and discover MCP tools before granting access")
        if private_mcp(config):
            if not (
                config.get("private_data_approved") is True
                and config.get("upstream_scope_approved") is True
                and config.get("allowed_user_ids")
            ):
                raise IntegrationConfigError(
                    "Private MCP requires named staff, private-data approval and confirmation "
                    "that the upstream credential enforces company scope and read-only access"
                )
        elif config.get("public_data_approved") is not True:
            raise IntegrationConfigError(
                "Only approved public-safe data can be used in voice calls"
            )
        eligible = {tool["name"] for tool in config.get("tools", []) if tool["read_only"]}
        if not set(config.get("allowed_tools", [])).issubset(eligible):
            raise IntegrationConfigError("Only discovered read-only MCP tools may be approved")
    try:
        for agent_id in config.get("agent_ids", []) + config.get("allowed_user_ids", []):
            if str(UUID(agent_id)) != agent_id:
                raise ValueError()
    except ValueError as exc:
        raise IntegrationConfigError("Invalid MCP agent or user ID") from exc


def prepare_mcp_update(previous: dict, updates: dict) -> dict:
    """Prevent clients forging test/catalog state; invalidate grants on credential changes."""
    if set(updates) - CLIENT_FIELDS:
        raise IntegrationConfigError("MCP test results and tool catalogs are server-managed")
    from app.services.integration_security import merge_integration_config

    merged = merge_integration_config(previous, updates)
    if merged.get("data_access_mode", "public") != previous.get("data_access_mode", "public"):
        # Never reinterpret an existing public grant as a private grant, or vice versa.
        merged.update(
            allowed_tools=[],
            agent_ids=[],
            allowed_user_ids=[],
            public_data_approved=False,
            private_data_approved=False,
            upstream_scope_approved=False,
        )
    if any(
        merged.get(key) != previous.get(key)
        for key in ("url", "credential", "auth_type", "company_label")
    ):
        merged.update(tools=[], allowed_tools=[], agent_ids=[], last_test={"status": "untested"})
        merged.update(
            allowed_user_ids=[], private_data_approved=False, upstream_scope_approved=False
        )
    validate_mcp_config(merged)
    return merged


def runtime_compatible(profile: AgentRuntimeProfile | None) -> bool:
    from app.services.browser_access import staff_browser

    config = (profile.runtime_config or {}) if profile else {}
    return bool(
        profile
        and (profile.enabled or (staff_browser(profile) and profile.status != "inactive"))
        and profile.telephony_provider == "livekit_sip"
        and profile.primary_speech_provider == "inworld"
        and config.get("voice_runtime") == "inworld_realtime"
        and config.get("inworld_single_pass") is not True
    )


async def validate_agent_grants(db, tenant_id: UUID, config: dict) -> None:
    from app.models.user import User
    from app.services.browser_access import STAFF_ROLES, tools_only

    user_ids = [UUID(value) for value in config.get("allowed_user_ids", [])]
    if user_ids:
        users = (
            await db.scalars(
                select(User.id).where(
                    User.tenant_id == tenant_id,
                    User.id.in_(user_ids),
                    User.is_active.is_(True),
                    User.role.in_(STAFF_ROLES),
                )
            )
        ).all()
        if len(users) != len(user_ids):
            raise IntegrationConfigError("Choose active workspace owners or administrators")
    ids = [UUID(value) for value in config.get("agent_ids", [])]
    if not ids:
        return
    rows = (
        await db.execute(
            select(Agent.id, AgentRuntimeProfile)
            .outerjoin(
                AgentRuntimeProfile,
                (AgentRuntimeProfile.agent_id == Agent.id)
                & (AgentRuntimeProfile.tenant_id == Agent.tenant_id),
            )
            .where(Agent.tenant_id == tenant_id, Agent.id.in_(ids), Agent.is_active.is_(True))
        )
    ).all()
    if len(rows) != len(ids) or any(not runtime_compatible(profile) for _, profile in rows):
        raise IntegrationConfigError(
            "Choose tenant-owned LiveKit/Inworld tool-loop agents. Single-pass and other "
            "runtimes cannot call MCP tools; their settings have not been changed."
        )
    if private_mcp(config) and any(
        not tools_only(profile)
        or profile.enabled
        or profile.assigned_numbers
        or (profile.runtime_config or {}).get("diagnostic_recording_mode", "off") != "off"
        for _, profile in rows
    ):
        raise IntegrationConfigError(
            "Private MCP requires staff browser-only, MCP-only agents with recording off "
            "and no phone activation"
        )


async def private_browser_admission(db, *, tenant_id, agent_id, user_id) -> dict:
    """Pin private scope before issuing a browser token; never upgrade an existing call."""
    rows = (
        await db.scalars(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.integration_type == "mcp",
                Integration.is_active.is_(True),
            )
        )
    ).all()
    granted = [row for row in rows if str(agent_id) in row.config.get("agent_ids", [])]
    private = [row for row in granted if private_mcp(row.config)]
    if not private:
        return {}
    if len(granted) != 1:
        raise IntegrationConfigError("Private agents must have exactly one MCP company connection")
    connection = private[0]
    cfg = load_integration_config(connection.config, connection.encrypted_config)
    validate_mcp_config(cfg)
    await validate_agent_grants(db, tenant_id, cfg)
    if str(user_id) not in cfg.get("allowed_user_ids", []):
        raise IntegrationConfigError("You are not approved for this private MCP connection")
    return {"private_mcp": True, "private_mcp_integration_id": str(connection.id)}


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > MAX_BYTES:
                raise MCPError("MCP response exceeded the size limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


@asynccontextmanager
async def mcp_session(config: dict):
    # Imported lazily to avoid a Celery dependency at module initialization.
    from app.tasks.webhook_tasks import _PinnedAsyncHTTPTransport, _resolve_public_destination

    validate_mcp_config(config)
    url = config["url"]
    timeout = config.get("timeout_seconds", 10)
    async with asyncio.timeout(timeout):
        addresses = await _resolve_public_destination(url)
        reviewed_address = REVIEWED_MCP_DESTINATIONS.get(url)
        if reviewed_address and set(addresses) != {reviewed_address}:
            raise MCPError("Reviewed MCP destination DNS changed; operator review required")
        transport = _PinnedAsyncHTTPTransport(urlsplit(url).hostname, addresses[0])

        class BoundedTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if str(request.url) != str(httpx.URL(url)):
                    raise MCPError("MCP destination change blocked")
                response = await transport.handle_async_request(request)
                if response.status_code >= 300 and not (
                    request.method in {"GET", "DELETE"} and response.status_code == 405
                ):
                    await response.aclose()
                    raise MCPError(f"MCP HTTP {response.status_code}")
                # Prevent compression bombs before JSON/SSE parsing.
                if response.headers.get("content-encoding", "identity") != "identity":
                    await response.aclose()
                    raise MCPError("Compressed MCP responses are unsupported")
                response.stream = _BoundedStream(response.stream)
                return response

            async def aclose(self):
                await transport.aclose()

        headers = {"Accept-Encoding": "identity"}
        if config.get("auth_type") == "bearer":
            headers["Authorization"] = "Bearer " + config["credential"]
        async with httpx.AsyncClient(
            transport=BoundedTransport(),
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=timeout)
                ) as session:
                    await session.initialize()
                    yield session


def _check_schema_complexity(schema) -> None:
    count = 0

    def walk(value, depth):
        nonlocal count
        count += 1
        if count > 2000 or depth > 20:
            raise MCPError("MCP schema is too complex")
        if isinstance(value, dict):
            if any(key in value for key in ("pattern", "patternProperties")):
                raise MCPError("MCP regular-expression schema validation is unsupported")
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(schema, 0)


def tool_descriptor(tool) -> dict:
    _check_schema_complexity(tool.inputSchema)
    if tool.outputSchema:
        _check_schema_complexity(tool.outputSchema)
    schema = tool.inputSchema
    serialized = json.dumps(schema, sort_keys=True)
    # No external schema fetching, deeply nested schemas, or unbounded model context.
    if not TOOL_NAME.fullmatch(tool.name) or len(serialized) > 16000:
        raise MCPError("MCP tool name or schema exceeds supported limits")
    references = r'"\$(?:ref|dynamicRef|recursiveRef)"'
    if re.search(references, serialized) or schema.get("type") != "object":
        raise MCPError("MCP schemas must be self-contained objects without references")
    if tool.outputSchema and (
        re.search(references, json.dumps(tool.outputSchema))
        or len(json.dumps(tool.outputSchema)) > 16000
    ):
        raise MCPError("MCP output schemas must be bounded and self-contained")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise MCPError("MCP tool schema is invalid") from exc
    annotations = tool.annotations
    read_only = bool(
        annotations and annotations.readOnlyHint is True and annotations.destructiveHint is not True
    )
    descriptor = {
        "name": tool.name,
        "description": (tool.description or "")[:1000],
        "input_schema": schema,
        "read_only": read_only,
        "output_schema": tool.outputSchema,
        "annotations": annotations.model_dump() if annotations else None,
    }
    descriptor["schema_hash"] = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True).encode()
    ).hexdigest()
    return descriptor


async def list_tools(session) -> list[dict]:
    tools, cursors = [], set()
    cursor = None
    for _ in range(10):
        page = await session.list_tools(cursor=cursor)
        tools.extend(tool_descriptor(tool) for tool in page.tools)
        if len(tools) > MAX_TOOLS or len({tool["name"] for tool in tools}) != len(tools):
            raise MCPError("MCP catalog exceeds limits or contains duplicate names")
        if len(json.dumps(tools)) > 200000:
            raise MCPError("MCP catalog exceeds the total size limit")
        cursor = page.nextCursor
        if not cursor:
            return tools
        if cursor in cursors:
            break
        cursors.add(cursor)
    raise MCPError("MCP tool pagination did not complete")


async def discover_tools(config: dict) -> dict:
    started = time.monotonic()
    try:
        async with mcp_session(config) as session:
            tools = await list_tools(session)
            credential = config.get("credential")
            if credential and credential in json.dumps(tools):
                raise MCPError("MCP catalog contains a credential and cannot be displayed")
        return {"tools": tools, "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:
        raise MCPError(
            "MCP connection failed. Check HTTPS endpoint, bearer credential, protocol and timeout. "
            "Private-network destinations, redirects and legacy SSE are not supported."
        ) from exc


async def call_read_tool(config: dict, tool: dict, arguments: dict) -> dict:
    started = time.monotonic()
    try:
        if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 8000:
            raise MCPError("MCP arguments exceed supported limits")
        if not tool["read_only"] or tool["name"] not in config.get("allowed_tools", []):
            raise MCPError("MCP tool is not approved for read-only access")
        Draft202012Validator(tool["input_schema"]).validate(arguments)
        async with mcp_session(config) as session:
            current = {entry["name"]: entry for entry in await list_tools(session)}
            if current.get(tool["name"], {}).get("schema_hash") != tool["schema_hash"]:
                raise MCPError("MCP tool changed. Rediscover and review permissions")
            result = await session.call_tool(tool["name"], arguments)
            if result.isError:
                raise MCPError("MCP tool could not complete the request")
            # No images, resource links, executable attachments, or automatic fetches.
            content = [item.text for item in result.content if item.type == "text"]
            data = {"text": content, "structured": result.structuredContent}
            encoded = json.dumps(data)
            if len(encoded) > 12000:
                raise MCPError("MCP result is too large; request a narrower result")
            if config.get("credential") and config["credential"] in encoded:
                raise MCPError("MCP result contains a credential and cannot be returned")
        return {
            "status": "ok",
            "data": data,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        raise MCPError(
            "MCP lookup unavailable; do not invent an answer or claim an action"
        ) from exc


async def authorized_runtime_config(db, tenant_id, agent_id, integration_id, *, call_id=None):
    from app.services.browser_access import staff_browser, validate_staff_call

    profile = await db.scalar(
        select(AgentRuntimeProfile)
        .where(
            AgentRuntimeProfile.agent_id == agent_id,
            AgentRuntimeProfile.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if staff_browser(profile):
        if call_id is None:
            raise MCPError("Staff browser reservation required")
        await validate_staff_call(db, tenant_id=tenant_id, agent_id=agent_id, call_id=call_id)
    integration = await db.scalar(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.tenant_id == tenant_id,
            Integration.integration_type == "mcp",
            Integration.is_active.is_(True),
        )
    )
    if integration is None:
        raise MCPError("MCP access unavailable")
    config = load_integration_config(integration.config, integration.encrypted_config)
    validate_mcp_config(config)
    if private_mcp(config):
        if call_id is None or not staff_browser(profile):
            raise MCPError("Private MCP requires an authenticated staff browser call")
        call, _ = await validate_staff_call(
            db, tenant_id=tenant_id, agent_id=agent_id, call_id=call_id
        )
        if not (
            (call.call_metadata or {}).get("private_mcp") is True
            and call.call_metadata.get("private_mcp_integration_id") == str(integration_id)
        ):
            raise MCPError("Private company access was not reserved; start a new call")
        if (call.call_metadata or {}).get("browser_user_id") not in config.get(
            "allowed_user_ids", []
        ):
            raise MCPError("Private MCP user access unavailable")
    if str(agent_id) not in config.get("agent_ids", []):
        raise MCPError("MCP agent access revoked")
    await validate_agent_grants(db, tenant_id, config)
    return config
