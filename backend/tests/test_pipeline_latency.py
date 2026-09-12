"""Offline request-context and streaming tests; no provider calls or production DB."""

import asyncio
import json

import pytest
from livekit.agents import llm

from app.livekit_runtime import pipeline_latency as latency
from app.livekit_runtime.worker import VAVInworldAgent, VAVInworldRealtimeAgent
from app.services.call_metadata import public_call_metadata


def pair(ctx, call_id, text, *, name=latency.KNOWLEDGE_TOOL, error=False):
    ctx.items.extend(
        [
            llm.FunctionCall(call_id=call_id, name=name, arguments='{"query":"department"}'),
            llm.FunctionCallOutput(call_id=call_id, name=name, output=text, is_error=error),
        ]
    )


def conversation():
    ctx = llm.ChatContext()
    ctx.add_message(role="system", content="Approved evidence only. Never guess a name.")
    ctx.add_message(role="user", content="Which departments do you have?")
    pair(ctx, "old", "Department source with exact names. " * 110)
    ctx.add_message(role="assistant", content="We have orthopedics and urology.")
    ctx.add_message(role="user", content="Who is the orthopedic doctor?")
    pair(ctx, "previous", "Dr. Example, 9 years of experience.")
    ctx.add_message(role="assistant", content="Dr. Example is our orthopedic doctor.")
    ctx.add_message(role="user", content="What about the urologist?")
    pair(ctx, "current", "Dr. Sample, 8 years of experience.")
    return ctx


def outputs(ctx):
    return {
        item.call_id: item.output for item in ctx.items if isinstance(item, llm.FunctionCallOutput)
    }


def test_disabled_is_exact_original_object():
    ctx = conversation()
    copied, removed = latency.request_context(ctx, enabled=False)
    assert copied is ctx
    assert removed == 0


def test_preserves_current_previous_dialogue_and_tool_pairs_without_mutating_history():
    ctx = conversation()
    before = [item.model_dump() for item in ctx.items]
    copied, removed = latency.request_context(ctx, enabled=True)
    assert [item.model_dump() for item in ctx.items] == before
    assert outputs(copied)["old"] == latency.OMITTED_EVIDENCE
    assert outputs(copied)["previous"] == outputs(ctx)["previous"]
    assert outputs(copied)["current"] == outputs(ctx)["current"]
    assert [item.model_dump() for item in copied.messages()] == [
        item.model_dump() for item in ctx.messages()
    ]
    assert [(item.id, item.type) for item in copied.items] == [
        (item.id, item.type) for item in ctx.items
    ]
    assert removed == len(outputs(ctx)["old"]) - len(latency.OMITTED_EVIDENCE)
    assert latency.request_context(copied, enabled=True)[1] == 0


@pytest.mark.parametrize("name,error", [("erp_sales", False), (latency.KNOWLEDGE_TOOL, True)])
def test_never_rewrites_financial_mcp_data_or_errors(name, error):
    ctx = conversation()
    ctx.items[1:1] = [
        llm.FunctionCall(call_id="protected", name=name, arguments="{}"),
        llm.FunctionCallOutput(
            call_id="protected", name=name, output="AED 21,379,403.99 " * 30, is_error=error
        ),
    ]
    copied, _ = latency.request_context(ctx, enabled=True)
    assert outputs(copied)["protected"] == outputs(ctx)["protected"]


def test_unknown_or_unpaired_result_not_replaced():
    ctx = conversation()
    ctx.items.insert(
        0, llm.FunctionCallOutput(call_id="orphan", output="evidence " * 100, is_error=False)
    )
    copied, _ = latency.request_context(ctx, enabled=True)
    assert outputs(copied)["orphan"] == outputs(ctx)["orphan"]


@pytest.mark.parametrize("count", [0, 1, 2])
def test_short_conversation_not_trimmed(count):
    ctx = llm.ChatContext()
    for i in range(count):
        ctx.add_message(role="user", content=f"question {i}")
        pair(ctx, str(i), "long evidence " * 100)
    copied, removed = latency.request_context(ctx, enabled=True)
    assert copied is ctx
    assert removed == 0


@pytest.mark.parametrize("value", [None, False, "true", 1])
def test_experiment_requires_explicit_boolean(value):
    assert not latency.context_window_enabled(
        {latency.CONTEXT_FLAG: value}, has_mcp_tools=False, tools_only=False
    )


def test_mcp_and_tools_only_cannot_enable_experiment():
    metadata = {latency.CONTEXT_FLAG: True}
    assert latency.context_window_enabled(metadata, has_mcp_tools=False, tools_only=False)
    assert not latency.context_window_enabled(metadata, has_mcp_tools=True, tools_only=False)
    assert not latency.context_window_enabled(metadata, has_mcp_tools=False, tools_only=True)
    assert not latency.context_window_enabled(None, has_mcp_tools=False, tools_only=False)


def test_serialized_openai_request_keeps_function_pairs_and_current_evidence():
    ctx, _ = latency.request_context(conversation(), enabled=True)
    messages, _ = ctx.to_provider_format("openai")
    results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
    assert results["old"] == latency.OMITTED_EVIDENCE
    assert results["current"] == "Dr. Sample, 8 years of experience."
    assert results["previous"] == "Dr. Example, 9 years of experience."
    calls = {call["id"] for item in messages for call in item.get("tool_calls", [])}
    assert calls == set(results)


def test_ten_turn_synthetic_payload_benchmark():
    ctx = llm.ChatContext()
    ctx.add_message(role="system", content="S" * 12000)
    original_chars = compact_chars = 0
    for i in range(10):
        ctx.add_message(role="user", content=f"Question {i}: tell me about this department")
        # Model call to choose a search, then model call to answer with the result.
        for after_tool in (False, True):
            if after_tool:
                pair(ctx, f"search-{i}", "E" * 3600)
            compact, _ = latency.request_context(ctx, enabled=True)
            original_chars += sum(latency.context_counts(ctx).values())
            compact_chars += sum(latency.context_counts(compact).values())
        ctx.add_message(role="assistant", content=f"Answer {i} grounded in the source.")
    reduction = (original_chars - compact_chars) / original_chars
    print(
        f"synthetic_context_chars: original={original_chars}, candidate={compact_chars}, "
        f"reduction={reduction:.1%}"
    )
    assert reduction > 0.35
    assert len(outputs(ctx)) == 10  # stored history is complete


def test_public_request_metrics_are_bounded_and_do_not_expose_payloads():
    request = {
        "sequence": 1,
        "turn": 2,
        "status": "completed",
        "prompt_tokens": None,
        "duration_ms": 812.5,
        "first_text_ms": float("nan"),
        "cached_prompt_tokens": True,
        "completion_tokens": -1,
        "prompt": "private name",
        "user_item_id": "private-id",
    }
    projected = public_call_metadata(
        {
            "agent_configuration": {},
            "runtime": {
                "pipeline_llm_requests": [request] * 130,
                "pipeline_llm_request_count": 130,
                "pipeline_llm_requests_truncated": True,
                "soniox_knowledge_context_window_enabled": False,
            },
        }
    )["runtime"]
    assert len(projected["pipeline_llm_requests"]) == 128
    assert projected["pipeline_llm_requests"][0] == {
        "sequence": 1,
        "turn": 2,
        "status": "completed",
        "prompt_tokens": None,
        "duration_ms": 812.5,
    }
    assert projected["pipeline_llm_request_count"] == 130
    assert projected["soniox_knowledge_context_window_enabled"] is False
    assert "private" not in json.dumps(projected)


@pytest.mark.asyncio
async def test_stream_is_unchanged_and_provider_usage_not_double_counted():
    chunks = [
        llm.ChatChunk(
            id="r",
            delta=llm.ChoiceDelta(
                tool_calls=[
                    llm.FunctionToolCall(
                        name=latency.KNOWLEDGE_TOOL, arguments="{}", call_id="tool"
                    )
                ]
            ),
        ),
        llm.ChatChunk(id="r", delta=llm.ChoiceDelta(content="Dr. Example")),
        llm.ChatChunk(
            id="r",
            usage=llm.CompletionUsage(
                prompt_tokens=1000,
                completion_tokens=20,
                total_tokens=1020,
                prompt_cached_tokens=400,
            ),
        ),
    ]

    async def source():
        for item in [*chunks, chunks[-1]]:
            yield item

    metrics = {"llm_tokens": 999}
    received = [
        item
        async for item in latency.observe_requests(
            source(), metrics=metrics, chat_ctx=conversation(), removed_chars=20
        )
    ]
    assert received == [*chunks, chunks[-1]]
    assert all(item is expected for item, expected in zip(received, [*chunks, chunks[-1]]))
    record = metrics["pipeline_llm_requests"][0]
    assert record["prompt_tokens"] == 1000
    assert record["cached_prompt_tokens"] == 400
    assert record["first_tool_call_ms"] is not None
    assert record["first_text_ms"] is not None
    assert record["status"] == "completed"
    assert metrics["llm_tokens"] == 999  # billing remains authoritative elsewhere
    assert "Dr. Example" not in json.dumps(metrics)


@pytest.mark.asyncio
async def test_cancel_closes_upstream_and_keeps_missing_usage_unknown():
    closed = []

    async def source():
        try:
            yield "first"
            await asyncio.sleep(100)
        finally:
            closed.append(True)

    metrics = {}
    stream = latency.observe_requests(
        source(), metrics=metrics, chat_ctx=conversation(), removed_chars=0
    )
    assert await anext(stream) == "first"
    await stream.aclose()
    assert closed == [True]
    record = metrics["pipeline_llm_requests"][0]
    assert record["status"] == "cancelled"
    assert record["prompt_tokens"] is None


@pytest.mark.asyncio
async def test_failure_not_swallowed_or_logged_with_private_content():
    async def source():
        raise RuntimeError("private payload")
        yield  # pragma: no cover

    metrics = {}
    with pytest.raises(RuntimeError):
        async for _ in latency.observe_requests(
            source(), metrics=metrics, chat_ctx=conversation(), removed_chars=0
        ):
            pass
    assert metrics["pipeline_llm_requests"][0]["status"] == "failed"
    assert "private payload" not in json.dumps(metrics)


@pytest.mark.asyncio
async def test_records_bounded_and_requests_separate():
    async def source():
        yield "hello"

    metrics = {}
    for _ in range(latency.MAX_REQUEST_RECORDS + 2):
        async for _ in latency.observe_requests(
            source(), metrics=metrics, chat_ctx=llm.ChatContext(), removed_chars=0
        ):
            pass
    assert len(metrics["pipeline_llm_requests"]) == latency.MAX_REQUEST_RECORDS
    assert metrics["pipeline_llm_request_count"] == latency.MAX_REQUEST_RECORDS + 2
    assert metrics["pipeline_llm_requests_truncated"] is True


@pytest.mark.asyncio
async def test_worker_hook_passes_compacted_context_to_same_model(monkeypatch):
    captured = []

    async def base_node(self, chat_ctx, tools, model_settings):
        captured.append(chat_ctx)
        yield "same speech"

    monkeypatch.setattr(VAVInworldAgent, "llm_node", base_node)
    agent = object.__new__(VAVInworldRealtimeAgent)
    agent._pipeline_latency_metrics = {}
    agent._soniox_context_window_enabled = True
    ctx = conversation()
    assert [chunk async for chunk in agent.llm_node(ctx, [], None)] == ["same speech"]
    assert outputs(captured[0])["old"] == latency.OMITTED_EVIDENCE
    assert outputs(ctx)["old"] != latency.OMITTED_EVIDENCE
    assert agent._pipeline_latency_metrics["pipeline_llm_request_count"] == 1
