"""Prepared transport is optional, tenant/call isolated and never changes retrieval."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.livekit_runtime.repair_transport import RepairTransport
from app.services.call_metadata import public_call_metadata


def fake_client():
    return SimpleNamespace(models=SimpleNamespace(list=AsyncMock()), close=AsyncMock())


async def settle():
    # Let the public nonblocking preparation task complete, without real timing.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_prepares_once_reuses_matching_key_without_generated_content():
    client = fake_client()
    factory = Mock(return_value=client)
    metrics = {}
    transport = RepairTransport(metrics, client_factory=factory)
    loader = AsyncMock(return_value="tenant-one-key")
    assert transport.client_for("tenant-one-key") is None
    transport.start(loader)
    transport.start(loader)
    await settle()
    assert transport.client_for("tenant-one-key") is client
    assert transport.client_for("tenant-one-key") is client
    assert transport.client_for("rotated-key") is None
    assert transport.client_for("") is None
    client.models.list.assert_awaited_once_with()
    loader.assert_awaited_once()
    assert metrics["knowledge_repair_transport_client_uses"] == 2
    await transport.aclose()
    await transport.aclose()
    client.close.assert_awaited_once()
    assert transport.client_for("tenant-one-key") is None
    transport.start(loader)
    loader.assert_awaited_once()


async def test_foreground_never_waits_and_cancellation_closes_client():
    client = fake_client()
    entered = asyncio.Event()

    async def pending():
        entered.set()
        await asyncio.Event().wait()

    client.models.list.side_effect = pending
    metrics = {}
    transport = RepairTransport(metrics, client_factory=lambda _: client)
    transport.start(AsyncMock(return_value="key"))
    await entered.wait()
    assert transport.client_for("key") is None
    await transport.aclose()
    client.close.assert_awaited_once()
    assert metrics["knowledge_repair_transport_status"] == "cancelled"


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("secret-value")])
async def test_failure_falls_back_without_exposing_errors(failure):
    client = fake_client()
    client.models.list.side_effect = failure
    metrics = {}
    transport = RepairTransport(metrics, client_factory=lambda _: client)
    transport.start(AsyncMock(return_value="key"))
    await settle()
    assert transport.client_for("key") is None
    assert "secret" not in str(metrics)
    await transport.aclose()
    client.close.assert_awaited_once()


async def test_missing_key_and_shutdown_during_key_lookup_create_no_client():
    factory = Mock()
    transport = RepairTransport({}, client_factory=factory)
    transport.start(AsyncMock(return_value=""))
    await settle()
    await transport.aclose()
    factory.assert_not_called()
    transport = RepairTransport({}, client_factory=factory)

    async def load():
        await asyncio.Event().wait()

    transport.start(load)
    await settle()
    await transport.aclose()
    factory.assert_not_called()


async def test_separate_calls_never_share_client_even_for_same_key():
    first, second = fake_client(), fake_client()
    one = RepairTransport({}, client_factory=lambda _: first)
    two = RepairTransport({}, client_factory=lambda _: second)
    for transport in (one, two):
        transport.start(AsyncMock(return_value="key"))
    await settle()
    assert one.client_for("key") is first
    assert two.client_for("key") is second
    await one.aclose()
    assert two.client_for("key") is second
    await two.aclose()


def test_public_projection_only_exposes_content_free_metrics():
    runtime = public_call_metadata(
        {
            "agent_configuration": {},
            "runtime": {
                "knowledge_repair_transport_status": "prepared",
                "knowledge_repair_transport_prepare_ms": 200,
                "knowledge_repair_transport_client_uses": 1,
                "knowledge_repair_transport_close_failed": True,
                "knowledge_repair_transport_api_key": "never expose",
            },
        }
    )["runtime"]
    assert runtime["knowledge_repair_transport_prepare_ms"] == 200
    assert runtime["knowledge_repair_transport_client_uses"] == 1
    assert "never expose" not in str(runtime)


async def test_runtime_injects_prepared_client_without_changing_repair_evidence(
    db, tenant, monkeypatch
):
    from app.livekit_runtime import worker
    from tests.test_knowledge_query_interpreter import enabled_runtime, repaired

    runtime = await enabled_runtime(db, tenant, monkeypatch)
    client = fake_client()
    transport = RepairTransport({}, client_factory=lambda _: client)
    transport.start(AsyncMock(return_value="test"))
    await settle()
    runtime._repair_transport = transport
    planner = AsyncMock(return_value=repaired())
    monkeypatch.setattr(worker, "interpret_knowledge_question", planner)
    try:
        answer = await runtime.retrieve_single_pass_evidence("How do I get hold of the team?")
        assert "665 9998" in answer and "551 3831" not in answer
        assert planner.call_args.kwargs["client"] is client
        assert planner.await_count == 1
        # Existing approved exact facts still bypass all model generation.
        await runtime.retrieve_single_pass_evidence("What is the phone number?")
        assert planner.await_count == 1
    finally:
        await transport.aclose()
