"""Offline tests use the actual LiveKit filler scheduler; no provider calls."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from livekit.agents import RunContext, llm
from livekit.agents.llm.utils import prepare_function_arguments

from app.livekit_runtime.lookup_filler import LookupFiller
from app.livekit_runtime.mcp_tools import PRIVATE_MCP_INSTRUCTIONS, _make_tool
from app.livekit_runtime.reporting import FINANCIAL_SPEECH_INSTRUCTIONS


class Speech:
    def __init__(self):
        self.num_steps = 1
        self.interrupted = False
        self.finished = asyncio.get_running_loop().create_future()
        self.cancelled = asyncio.Event()

    def done(self):
        return self.finished.done()

    def interrupt(self):
        self.interrupted = True
        self.cancelled.set()
        if not self.finished.done():
            self.finished.set_result(None)

    def add_done_callback(self, callback):
        self.finished.add_done_callback(lambda _: callback(self))

    async def wait_if_not_interrupted(self, tasks):
        interrupted = asyncio.create_task(self.cancelled.wait())
        try:
            await asyncio.wait([*tasks, interrupted], return_when=asyncio.FIRST_COMPLETED)
        finally:
            interrupted.cancel()

    def __await__(self):
        return self.finished.__await__()


class Session:
    def __init__(self):
        self._global_run_state = None
        self.listeners = {}
        self.idle = asyncio.Event()
        self.idle.set()
        self.spoken = []

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def off(self, event, callback):
        self.listeners[event].remove(callback)

    async def wait_for_idle(self):
        await self.idle.wait()

    def user_speaking(self):
        self.idle.clear()
        for callback in self.listeners.get("user_state_changed", []):
            callback(SimpleNamespace(new_state="speaking"))

    def say(self, phrase, **options):
        handle = Speech()
        self.spoken.append((phrase, options, handle))
        return handle


class FastContext(RunContext):
    @asynccontextmanager
    async def with_filler(self, source, *, delay, max_steps):
        assert delay == 1.5 and max_steps == 1
        # Shorten only the clock; exercise LiveKit's real scheduler and cleanup.
        async with super().with_filler(source, delay=0.01, max_steps=max_steps):
            yield


def context():
    session, parent = Session(), Speech()
    return FastContext(
        session=session,
        speech_handle=parent,
        function_call=llm.FunctionCall(name="report", call_id="one", arguments="{}"),
    )


async def test_fast_lookup_never_speaks():
    ctx = context()
    async with LookupFiller({}).pending(ctx):
        pass
    await asyncio.sleep(0.03)
    assert not ctx.session.spoken


@pytest.mark.parametrize("fail", [False, True])
async def test_slow_lookup_cue_cancelled_on_success_or_error(fail):
    ctx, metrics = context(), {}
    filler = LookupFiller(metrics)
    try:
        async with filler.pending(ctx):
            await asyncio.sleep(0.04)
            assert len(ctx.session.spoken) == 1
            assert metrics["mcp_filler_active"] is True
            if fail:
                raise ValueError("lookup failed")
    except ValueError:
        assert fail
    phrase, options, handle = ctx.session.spoken[0]
    assert phrase == "I'm checking the requested information."
    assert options == {"allow_interruptions": True, "add_to_chat_ctx": False}
    assert handle.interrupted
    assert metrics["mcp_filler_active"] is False
    # Further tools in the same turn cannot repeat the reassurance.
    async with filler.pending(ctx):
        await asyncio.sleep(0.03)
    assert len(ctx.session.spoken) == 1
    assert all(not callbacks for callbacks in ctx.session.listeners.values())


async def test_no_cue_during_caller_speech_and_dwell_restarts():
    ctx = context()
    async with LookupFiller({}).pending(ctx):
        await asyncio.sleep(0)
        ctx.session.user_speaking()
        await asyncio.sleep(0.03)
        assert not ctx.session.spoken
        ctx.session.idle.set()
        await asyncio.sleep(0.04)
        assert len(ctx.session.spoken) == 1


async def test_interrupted_parent_and_cancelled_lookup_leave_no_cue():
    ctx = context()
    async with LookupFiller({}).pending(ctx):
        ctx.speech_handle.interrupt()
        await asyncio.sleep(0.03)
    assert not ctx.session.spoken
    ctx = context()

    async def run():
        async with LookupFiller({}).pending(ctx):
            await asyncio.sleep(10)

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.03)
    assert not ctx.session.spoken


async def test_filler_failure_does_not_fail_lookup():
    ctx, metrics = context(), {}

    def broken_say(*args, **kwargs):
        raise RuntimeError("speech unavailable")

    ctx.session.say = broken_say
    async with LookupFiller(metrics).pending(ctx):
        await asyncio.sleep(0.03)
    assert metrics["mcp_filler_error_count"] == 1
    assert metrics["mcp_filler_active"] is False


async def test_missing_context_and_unknown_or_switching_language_are_silent():
    metrics = {}
    async with LookupFiller(metrics).pending(None):
        pass
    assert metrics["mcp_filler_supported"] is False
    for language in ("auto", "unknown"):
        ctx = context()
        async with LookupFiller(metrics, language).pending(ctx):
            await asyncio.sleep(0.02)
        assert not ctx.session.spoken


def test_raw_mcp_tool_receives_sdk_context_without_changing_provider_schema():
    from uuid import uuid4

    descriptor = {"name": "report", "description": "A report", "input_schema": {"type": "object"}}
    tool = _make_tool(uuid4(), uuid4(), uuid4(), "Company", descriptor, {})
    ctx = object.__new__(RunContext)
    args, kwargs = prepare_function_arguments(fnc=tool, json_arguments={}, call_ctx=ctx)
    assert args == ({}, ctx) and kwargs == {}
    assert tool.info.raw_schema["parameters"] == {"type": "object"}


def test_shared_policy_covers_exact_amounts_currency_units_and_analysis():
    for rule in (
        "exact invoice amounts",
        "payments, collections",
        "reconciliation",
        "AED means dirhams",
        "USD means US dollars",
        "450 dollars, NOT 450,000",
        "apply that scale exactly once",
        "identifiers",
        "exact source values",
    ):
        assert rule in FINANCIAL_SPEECH_INSTRUCTIONS
    for rule in ("recommendations", "Never invent", "directly", "permitted tool"):
        assert rule in PRIVATE_MCP_INSTRUCTIONS


def test_worker_keeps_financial_policy_in_tools_only():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "app/livekit_runtime/worker.py").read_text(
        encoding="utf-8"
    )
    assert "instructions=FINANCIAL_SPEECH_INSTRUCTIONS + instructions" in source


def test_filler_does_not_consume_actual_answer_latency(monkeypatch):
    from app.livekit_runtime import worker

    metrics, samples = {"mcp_filler_active": True}, []
    telemetry = worker._LiveKitRuntimeTelemetry(metrics, samples, 0)
    telemetry.first_agent_audio_seen = True
    telemetry.last_user_speech_end_at = 10.0
    telemetry.last_final_transcript_at = 10.1
    telemetry.current_turn_trace = {"turn": 1}
    monkeypatch.setattr(worker.time, "monotonic", lambda: 11.5)
    telemetry.on_agent_state(new_state="speaking")
    assert samples == [] and telemetry.turn_diagnostics == []
    assert telemetry.last_user_speech_end_at == 10.0
    assert telemetry.last_final_transcript_at == 10.1
    metrics["mcp_filler_active"] = False
    monkeypatch.setattr(worker.time, "monotonic", lambda: 16.0)
    telemetry.on_agent_state(new_state="speaking")
    assert samples == [6000]
    assert metrics["last_transcript_to_first_audio_ms"] == 5900
    assert telemetry.turn_diagnostics[0]["outcome"] == "answered"


async def test_concurrent_tools_share_one_cue_and_new_turn_can_speak():
    ctx, metrics = context(), {}
    filler = LookupFiller(metrics)

    async def lookup():
        async with filler.pending(ctx):
            await asyncio.sleep(0.04)

    await asyncio.gather(lookup(), lookup())
    assert len(ctx.session.spoken) == 1
    second = FastContext(
        session=ctx.session,
        speech_handle=Speech(),
        function_call=llm.FunctionCall(name="report", call_id="two", arguments="{}"),
    )
    async with filler.pending(second):
        await asyncio.sleep(0.04)
    assert len(ctx.session.spoken) == 2
    assert metrics["mcp_filler_scheduled_count"] == 2
