import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from livekit.agents import llm

from app.livekit_runtime import mcp_tools
from app.livekit_runtime.mcp_request_context import report_arguments, report_date_instruction
from app.services import mcp_connections as mcp
from tests.test_mcp_connections import config, read_tool

SCHEMA = {
    "type": "object",
    "properties": {
        key: {"type": "string", "description": "YYYY-MM-DD"} for key in ("start_date", "end_date")
    },
    "required": ["start_date", "end_date"],
}


@pytest.mark.parametrize(
    "now,start,end",
    [
        (datetime(2026, 9, 8, tzinfo=UTC), "2026-08-01", "2026-08-31"),
        (datetime(2026, 1, 2, tzinfo=UTC), "2025-12-01", "2025-12-31"),
        (datetime(2024, 3, 1, tzinfo=UTC), "2024-02-01", "2024-02-29"),
        (datetime(2026, 8, 31, 21, tzinfo=UTC), "2026-08-01", "2026-08-31"),
    ],
)
def test_previous_month_uses_company_timezone_and_preserves_filters(now, start, end):
    original = {"start_date": "last month", "end_date": "last month", "group_by": "channel"}
    answer = report_arguments(
        original, "Give me last month's sales", SCHEMA, timezone="Asia/Dubai", now=now
    )
    assert answer == {"start_date": start, "end_date": end, "group_by": "channel"}
    assert original["start_date"] == "last month"
    assert start in report_date_instruction("Asia/Dubai", now=now)


@pytest.mark.parametrize(
    "question",
    [
        "Not last month, July",
        "Compare last month with July",
        "Last month of 2025",
        "Sales since last month",
        "Last month to today",
    ],
)
def test_ambiguous_or_comparison_dates_are_not_overwritten(question):
    args = {"start_date": "2026-07-01", "end_date": "2026-07-31"}
    assert report_arguments(args, question, SCHEMA) == args


def test_unknown_schema_not_assumed_to_be_financial():
    assert report_arguments({"q": "last month"}, "last month", {}) == {"q": "last month"}


def test_timestamp_or_untyped_fields_are_not_converted_to_calendar_dates():
    for prop in ({"type": "integer"}, {"type": "string", "format": "date-time"}, {}):
        schema = {"properties": {key: prop for key in ("start_date", "end_date")}}
        args = {"start_date": "a", "end_date": "b"}
        assert report_arguments(args, "last month", schema) == args


@pytest.mark.parametrize(
    "rpc_code,expected",
    [(-32602, "invalid_arguments"), (-32601, "schema_changed"), (-32603, "upstream_unavailable")],
)
def test_protocol_error_is_classified_without_echoing_private_provider_message(rpc_code, expected):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    error = McpError(ErrorData(code=rpc_code, message="private-token"))
    safe = mcp.safe_lookup_error(ExceptionGroup("SDK", [error]), stage="execute")
    assert safe.code == expected and "private-token" not in str(safe)


@pytest.mark.parametrize(
    "error,code,retryable",
    [
        (TimeoutError("private-token"), "timeout", True),
        (httpx.ConnectError("private-token"), "network_error", True),
        (ValueError("private-token"), "lookup_failed", False),
        (mcp.MCPError("private-token", code="schema_changed"), "schema_changed", False),
    ],
)
def test_sdk_task_groups_keep_safe_reason_not_private_exception(error, code, retryable):
    wrapped = ExceptionGroup("private-token", [error])
    safe = mcp.safe_lookup_error(wrapped, stage="execute")
    assert safe.code == code and safe.retryable is retryable
    assert safe.stage == "execute" and "private-token" not in str(safe)


@pytest.mark.parametrize(
    "start,end",
    [("last month", "2026-08-31"), ("2026-02-30", "2026-08-31"), ("2026-09-01", "2026-08-31")],
)
async def test_bad_dates_fail_before_connecting(monkeypatch, start, end):
    session = AsyncMock()
    monkeypatch.setattr(mcp, "mcp_session", session)
    descriptor = mcp.tool_descriptor(read_tool()) | {"input_schema": SCHEMA}
    with pytest.raises(mcp.MCPError) as failure:
        await mcp.call_read_tool(
            config() | {"allowed_tools": [descriptor["name"]]},
            descriptor,
            {"start_date": start, "end_date": end},
        )
    assert failure.value.code == "invalid_arguments"
    session.assert_not_called()


async def test_remote_tool_error_is_not_access_denial(monkeypatch):
    descriptor = mcp.tool_descriptor(read_tool())
    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(isError=True)),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[read_tool()], nextCursor=None)),
    )

    @asynccontextmanager
    async def connected(_):
        yield session

    monkeypatch.setattr(mcp, "mcp_session", connected)
    with pytest.raises(mcp.MCPError) as failure:
        await mcp.call_read_tool(config() | {"allowed_tools": [descriptor["name"]]}, descriptor, {})
    assert failure.value.code == "tool_error" and failure.value.stage == "execute"
    assert not failure.value.retryable


def runtime_fixture(monkeypatch, errors, *, idempotent=True):
    descriptor = mcp.tool_descriptor(read_tool())
    descriptor["annotations"]["idempotentHint"] = idempotent
    cfg = config() | {"allowed_tools": [descriptor["name"]], "tools": [descriptor]}

    @asynccontextmanager
    async def database():
        yield object()

    auth = AsyncMock(return_value=cfg)
    execute = AsyncMock(side_effect=errors)
    monkeypatch.setattr(mcp_tools, "async_session_factory", database)
    monkeypatch.setattr(mcp_tools, "authorized_runtime_config", auth)
    monkeypatch.setattr(mcp_tools, "call_read_tool", execute)
    metrics = {}
    tool = mcp_tools._make_tool(uuid4(), uuid4(), uuid4(), "Fixture", descriptor, metrics)
    return tool, metrics, auth, execute


@pytest.mark.parametrize("size,allowed", [(13000, True), (110000, False)])
async def test_larger_reports_preserved_but_result_remains_bounded(monkeypatch, size, allowed):
    descriptor = mcp.tool_descriptor(read_tool())
    text = "x" * size
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text=text)],
                structuredContent={"report": text},
            )
        ),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[read_tool()], nextCursor=None)),
    )

    @asynccontextmanager
    async def connected(_):
        yield session

    monkeypatch.setattr(mcp, "mcp_session", connected)
    if allowed:
        result = await mcp.call_read_tool(
            config() | {"allowed_tools": [descriptor["name"]]}, descriptor, {}
        )
        assert result["data"] == {"text": [text], "structured": {"report": text}}
    else:
        with pytest.raises(mcp.MCPError) as failure:
            await mcp.call_read_tool(
                config() | {"allowed_tools": [descriptor["name"]]}, descriptor, {}
            )
        assert failure.value.code == "result_too_large"
        assert failure.value.stage == "result_validation"


async def test_transient_read_retries_once_after_fresh_authorization(monkeypatch):
    error = mcp.MCPError("safe", code="network_error", retryable=True)
    tool, metrics, auth, execute = runtime_fixture(
        monkeypatch, [error, {"status": "ok", "data": {}}]
    )
    result = json.loads(await tool({}))
    assert result["untrusted_tool_data"]["status"] == "ok"
    assert execute.await_count == 2 and auth.await_count == 3
    assert metrics["mcp_tool_calls"][0]["attempts"] == 2
    assert metrics["mcp_retry_count"] == 1


@pytest.mark.parametrize(
    "code",
    [
        "invalid_arguments",
        "schema_changed",
        "tool_error",
        "upstream_auth_failed",
        "result_too_large",
    ],
)
async def test_nonrecoverable_errors_not_blindly_retried_or_called_revocation(monkeypatch, code):
    tool, metrics, _, execute = runtime_fixture(
        monkeypatch, [mcp.MCPError("private-token", code=code, stage="execute")]
    )
    result = json.loads(await tool({}))
    assert result["error_code"] == code and execute.await_count == 1
    assert metrics["mcp_tool_calls"][0]["error_code"] == code
    assert "private-token" not in json.dumps(metrics) + json.dumps(result)
    assert "access was revoked" not in result["instruction"]


async def test_second_transient_failure_stops_and_reports_reason(monkeypatch):
    error = mcp.MCPError("safe", code="timeout", retryable=True)
    tool, metrics, _, execute = runtime_fixture(monkeypatch, [error, error])
    result = json.loads(await tool({}))
    assert result["error_code"] == "timeout" and execute.await_count == 2


async def test_retry_forbidden_for_non_idempotent_reads(monkeypatch):
    tool, _, _, execute = runtime_fixture(
        monkeypatch, [mcp.MCPError("safe", code="timeout", retryable=True)], idempotent=False
    )
    await tool({})
    assert execute.await_count == 1


async def test_permission_revocation_before_retry_never_executes_again(monkeypatch):
    tool, _, auth, execute = runtime_fixture(
        monkeypatch, [mcp.MCPError("safe", code="timeout", retryable=True)]
    )
    cfg = auth.return_value
    auth.side_effect = [cfg, mcp.MCPError("revoked")]
    result = json.loads(await tool({}))
    assert result["error_code"] == "authorization_failed" and execute.await_count == 1


async def test_caller_interruption_prevents_pending_retry(monkeypatch):
    tool, _, _, execute = runtime_fixture(monkeypatch, [])
    ctx = SimpleNamespace(speech_handle=SimpleNamespace(interrupted=False))

    async def first(*args):
        ctx.speech_handle.interrupted = True
        raise mcp.MCPError("safe", code="network_error", retryable=True)

    execute.side_effect = first
    with pytest.raises(llm.StopResponse):
        await tool({}, ctx)
    assert execute.await_count == 1


async def test_cancellation_is_not_swallowed(monkeypatch):
    tool, _, _, execute = runtime_fixture(monkeypatch, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await tool({})
    assert execute.await_count == 1


async def test_interruption_during_retry_authorization_prevents_remote_read(monkeypatch):
    tool, _, auth, execute = runtime_fixture(
        monkeypatch, [mcp.MCPError("safe", code="timeout", retryable=True)]
    )
    cfg = auth.return_value
    ctx = SimpleNamespace(speech_handle=SimpleNamespace(interrupted=False))

    async def permission(*args, **kwargs):
        if auth.await_count == 2:
            ctx.speech_handle.interrupted = True
        return cfg

    auth.side_effect = permission
    with pytest.raises(llm.StopResponse):
        await tool({}, ctx)
    assert execute.await_count == 1
