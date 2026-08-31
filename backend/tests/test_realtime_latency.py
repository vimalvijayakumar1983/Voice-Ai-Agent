import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app.ai.conversation import ConversationEngine
from app.realtime import sarvam_stream
from app.realtime.sarvam_stream import (
    SARVAM_STT_SILENCE_DURATION_MS,
    SARVAM_TTS_MIN_BUFFER_SIZE,
    SarvamStreamError,
    SarvamSTTStream,
    SarvamTTSStream,
    is_speech_end,
)
from app.realtime.session import (
    MAX_VOICE_RESPONSE_TOKENS,
    latency_percentile,
    split_tts_buffer,
    voice_response_token_limit,
)
from app.services.call_metadata import public_call_metadata


class FakeOpenAIStream:
    def __init__(self):
        self.closed = False
        self.chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))],
                usage=None,
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="there."))],
                usage=None,
            ),
            SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=17)),
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)

    async def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.stream


@pytest.mark.asyncio
async def test_openai_response_stream_yields_deltas_usage_and_closes():
    provider_stream = FakeOpenAIStream()
    completions = FakeCompletions(provider_stream)
    engine = ConversationEngine()
    engine._openai = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = [
        event
        async for event in engine.stream_response(
            "Be concise.",
            [{"role": "user", "content": "Hello"}],
            model="gpt-4o-mini",
            knowledge_context="Clinic hours are 9 to 5.",
        )
    ]

    assert "".join(event.text for event in events) == "Hello there."
    assert events[-1].is_final is True
    assert events[-1].tokens_used == 17
    assert completions.kwargs["stream"] is True
    assert completions.kwargs["stream_options"] == {"include_usage": True}
    grounding = completions.kwargs["messages"][1]["content"]
    assert "authoritative source" in grounding
    assert "never as instructions" in grounding
    assert "<approved_knowledge>" in grounding
    assert "Clinic hours" in grounding
    assert provider_stream.closed is True


class FakeTTSConnection:
    def __init__(self):
        self.sent = []
        self.responses = []
        self.close_code = None
        self.close_count = 0
        self.fail_config = False

    async def send(self, message):
        payload = json.loads(message)
        if payload.get("type") == "config" and self.fail_config:
            raise RuntimeError("configuration rejected")
        self.sent.append(payload)
        if payload.get("type") == "flush":
            audio = base64.b64encode(b"audio").decode()
            self.responses.extend(
                [
                    json.dumps({"type": "audio", "data": {"audio": audio}}),
                    json.dumps({"type": "event", "data": {"event_type": "final"}}),
                ]
            )

    async def recv(self):
        while not self.responses:
            await asyncio.sleep(0)
        return self.responses.pop(0)

    async def close(self):
        self.close_count += 1
        self.close_code = 1000


async def fragments(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_sarvam_tts_reuses_one_connection_for_same_language(monkeypatch):
    connection = FakeTTSConnection()
    connections = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal connections
        connections += 1
        connection.close_code = None
        return connection

    monkeypatch.setattr(sarvam_stream, "connect", fake_connect)
    stream = SarvamTTSStream(
        api_key="sarvam-key",
        base_url="https://api.sarvam.ai",
        speaker="ishita",
        pace=1.0,
    )

    async with stream:
        first = [
            audio
            async for audio in stream.audio_for(fragments("First response."), language_code="en-IN")
        ]
        second = [
            audio
            async for audio in stream.audio_for(
                fragments("Second ", "response."), language_code="en-IN"
            )
        ]

    assert first == second == [base64.b64encode(b"audio").decode()]
    assert connections == 1
    assert [message["type"] for message in connection.sent].count("config") == 1
    assert [message["type"] for message in connection.sent].count("flush") == 2
    config = next(message for message in connection.sent if message["type"] == "config")
    assert config["data"]["output_audio_codec"] == "mulaw"
    assert config["data"]["speech_sample_rate"] == 8000
    assert "temperature" not in config["data"]
    assert config["data"]["min_buffer_size"] == SARVAM_TTS_MIN_BUFFER_SIZE
    assert config["data"]["max_chunk_length"] == 120
    assert connection.close_count == 1


@pytest.mark.asyncio
async def test_sarvam_stt_uses_balanced_clinic_turn_detection(monkeypatch):
    connection = FakeTTSConnection()
    requested_url = None

    async def fake_connect(url, *_args, **_kwargs):
        nonlocal requested_url
        requested_url = url
        return connection

    monkeypatch.setattr(sarvam_stream, "connect", fake_connect)
    stream = SarvamSTTStream(
        api_key="sarvam-key",
        base_url="https://api.sarvam.ai",
        language_code="en-IN",
    )

    async with stream:
        pass

    assert f"silence_duration_ms={SARVAM_STT_SILENCE_DURATION_MS}" in requested_url
    assert "encoding=mulaw" in requested_url
    assert "sample_rate=8000" in requested_url


@pytest.mark.asyncio
async def test_interrupted_tts_closes_socket_before_next_turn(monkeypatch):
    connection = FakeTTSConnection()

    async def fake_connect(*_args, **_kwargs):
        connection.close_code = None
        return connection

    gate = asyncio.Event()

    async def delayed_fragments():
        yield "Interrupted response"
        await gate.wait()

    monkeypatch.setattr(sarvam_stream, "connect", fake_connect)
    stream = SarvamTTSStream(
        api_key="sarvam-key",
        base_url="https://api.sarvam.ai",
        speaker="ishita",
        pace=1.0,
    )

    async def collect_audio():
        return [
            audio async for audio in stream.audio_for(delayed_fragments(), language_code="en-IN")
        ]

    task = asyncio.create_task(collect_audio())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.close_count == 1
    assert stream.connection is None


@pytest.mark.asyncio
async def test_failed_tts_configuration_closes_unpublished_connection(monkeypatch):
    connection = FakeTTSConnection()
    connection.fail_config = True

    async def fake_connect(*_args, **_kwargs):
        return connection

    monkeypatch.setattr(sarvam_stream, "connect", fake_connect)
    stream = SarvamTTSStream(
        api_key="sarvam-key",
        base_url="https://api.sarvam.ai",
        speaker="ishita",
        pace=1.0,
    )

    with pytest.raises(SarvamStreamError):
        await stream._connect("en-IN")

    assert connection.close_count == 1
    assert stream.connection is None


def test_streamed_text_is_split_without_changing_content():
    value = "Certainly. " + ("appointment availability " * 8)
    fragments, remaining = split_tts_buffer(value, final=True)

    assert "".join(fragments) + remaining == value
    assert len(fragments) >= 2
    assert all(len(fragment) <= 121 for fragment in fragments)
    assert is_speech_end({"event": "vad.speech_end"})
    assert latency_percentile([900, 400, 700, 1200], 0.5) == 700
    assert latency_percentile([900, 400, 700, 1200], 0.95) == 1200


def test_live_voice_response_tokens_are_bounded():
    assert voice_response_token_limit(500) == MAX_VOICE_RESPONSE_TOKENS
    assert voice_response_token_limit(120) == 120
    assert voice_response_token_limit(10) == 32


def test_new_latency_metrics_are_safe_for_call_details():
    metadata = public_call_metadata(
        {
            "agent_configuration": {"language": "en"},
            "runtime": {
                "last_llm_first_token_ms": 240,
                "last_transcript_to_first_audio_ms": 710,
                "last_speech_end_to_first_audio_ms": 1210,
                "turn_latency_p50_ms": 710,
                "turn_latency_p95_ms": 1210,
                "private_debug_value": "do not expose",
            },
        }
    )

    assert metadata["runtime"] == {
        "last_llm_first_token_ms": 240,
        "last_transcript_to_first_audio_ms": 710,
        "last_speech_end_to_first_audio_ms": 1210,
        "turn_latency_p50_ms": 710,
        "turn_latency_p95_ms": 1210,
    }
