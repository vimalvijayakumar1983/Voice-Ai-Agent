import asyncio

import pytest

from app.livekit_runtime.tts_preconnect import (
    preconnect_tts_transport,
    start_preconnect_after_first_audio,
)


class Stream:
    def __init__(self, *, wait=False, audio=False, fail=False):
        self.wait = wait
        self.audio = audio
        self.fail = fail
        self.closed = False
        self.ended = False
        self.texts = []
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def end_input(self):
        self.ended = True

    def push_text(self, value):
        self.texts.append(value)
        raise AssertionError("Transport preconnect must not synthesize speech")

    async def __aiter__(self):
        assert self.ended
        if self.fail:
            raise RuntimeError("secret-provider-exception")
        if self.wait:
            await asyncio.Event().wait()
        if self.audio:
            yield object()


class Engine:
    def __init__(self, stream):
        self.test_stream = stream
        self.options = None

    def stream(self, *, conn_options):
        self.options = conn_options
        return self.test_stream


async def test_first_audio_latch_never_delays_greeting_or_restarts_on_later_turns():
    engine = Engine(Stream())
    metrics = {}
    for enabled, state in [(True, "listening"), (True, "thinking"), (False, "speaking")]:
        assert (
            start_preconnect_after_first_audio(
                engine, metrics, enabled=enabled, new_state=state, task=None
            )
            is None
        )
    assert engine.options is None
    task = start_preconnect_after_first_audio(
        engine, metrics, enabled=True, new_state="speaking", task=None
    )
    assert task is not None
    await task
    assert (
        start_preconnect_after_first_audio(
            engine, metrics, enabled=True, new_state="speaking", task=task
        )
        is task
    )


async def test_preconnect_only_opens_and_closes_public_stream():
    stream = Stream()
    engine = Engine(stream)
    metrics = {}
    await preconnect_tts_transport(engine, metrics)
    assert stream.closed and stream.ended and not stream.texts
    assert engine.options.max_retry == 0
    assert engine.options.timeout == 3
    assert metrics["tts_transport_preconnect_status"] == "completed"
    assert metrics["tts_transport_preconnect_text_characters"] == 0
    assert metrics["tts_transport_preconnect_ms"] >= 0


@pytest.mark.parametrize("failure,status", [("fail", "failed"), ("audio", "unexpected_audio")])
async def test_failure_is_content_free_and_never_plays_audio(failure, status):
    stream = Stream(**{failure: True})
    metrics = {}
    await preconnect_tts_transport(Engine(stream), metrics)
    assert stream.closed
    assert metrics["tts_transport_preconnect_status"] == status
    assert "secret" not in str(metrics)


async def test_slow_provider_is_bounded_and_stream_closed():
    stream = Stream(wait=True)
    metrics = {}
    await asyncio.wait_for(
        preconnect_tts_transport(Engine(stream), metrics, timeout_seconds=0.01), 0.5
    )
    assert stream.closed
    assert metrics["tts_transport_preconnect_status"] == "timeout"


async def test_call_shutdown_cancels_idle_transport_and_closes_stream():
    stream = Stream(wait=True)
    metrics = {}
    task = asyncio.create_task(preconnect_tts_transport(Engine(stream), metrics))
    await stream.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert metrics["tts_transport_preconnect_status"] == "cancelled"


def test_public_projection_keeps_only_transport_diagnostics():
    from app.services.call_metadata import public_call_metadata

    result = public_call_metadata(
        {
            "agent_configuration": {},
            "runtime": {
                "tts_transport_preconnect_status": "completed",
                "tts_transport_preconnect_ms": 970,
                "tts_transport_preconnect_text_characters": 0,
                "tts_transport_preconnect_provider_error": "secret",
            },
        }
    )
    assert result["runtime"] == {
        "tts_transport_preconnect_status": "completed",
        "tts_transport_preconnect_ms": 970,
        "tts_transport_preconnect_text_characters": 0,
    }
