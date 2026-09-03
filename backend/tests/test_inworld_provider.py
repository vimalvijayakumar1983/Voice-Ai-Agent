"""Direct Inworld provider adapter tests."""

import base64
import json

import httpx
import pytest

from app.providers import inworld as inworld_module
from app.providers.inworld import (
    INWORLD_TTS_SUPPORTED_LANGUAGES,
    MAX_PROBE_RESPONSE_BYTES,
    ROUTER_AUTO_PROBE_MAX_TOKENS,
    InworldClient,
    InworldError,
)
from app.services.agent_catalog import (
    LanguageCompatibilityStatus,
    voice_language_compatibility,
)


class _RealtimeProbeWebSocket:
    def __init__(self, events):
        self.events = list(events)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def receive_json(self):
        return self.events.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)


class _RealtimeProbeClientSession:
    def __init__(self, websocket, captured):
        self.websocket = websocket
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def ws_connect(self, url, headers):
        self.captured.update({"url": url, "headers": headers})
        return self.websocket


@pytest.mark.asyncio
async def test_realtime_readiness_executes_required_tool_without_generating_audio(monkeypatch):
    captured = {}
    websocket = _RealtimeProbeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {
                "type": "response.function_call_arguments.done",
                "name": "vav_readiness_check",
                "arguments": "{}",
            },
        ]
    )
    monkeypatch.setattr(
        inworld_module.aiohttp,
        "ClientSession",
        lambda: _RealtimeProbeClientSession(websocket, captured),
    )
    client = InworldClient(api_key="workspace-inworld-key-123456")

    await client.realtime_readiness_probe(
        model_id="openai/gpt-4o-mini",
        voice_id="Ashley",
        stt_model_id="assemblyai/u3-rt-pro",
    )

    update = websocket.sent[0]
    assert update["session"]["output_modalities"] == ["text"]
    assert update["session"]["tool_choice"] == "required"
    assert update["session"]["tools"][0]["name"] == "vav_readiness_check"
    assert websocket.sent[-1] == {"type": "response.create"}
    assert captured["headers"]["Authorization"] == "Basic workspace-inworld-key-123456"


@pytest.mark.asyncio
async def test_realtime_readiness_surfaces_plan_restriction_before_a_call(monkeypatch):
    websocket = _RealtimeProbeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {
                "type": "error",
                "error": {
                    "message": (
                        "Tool calling is currently restricted on your plan. "
                        "Add credits to unlock."
                    )
                },
            },
        ]
    )
    monkeypatch.setattr(
        inworld_module.aiohttp,
        "ClientSession",
        lambda: _RealtimeProbeClientSession(websocket, {}),
    )
    client = InworldClient(api_key="workspace-inworld-key-123456")

    with pytest.raises(InworldError, match="Tool calling is currently restricted"):
        await client.realtime_readiness_probe(
            model_id="openai/gpt-4o-mini",
            voice_id="Ashley",
            stt_model_id="assemblyai/u3-rt-pro",
        )


@pytest.mark.asyncio
async def test_inworld_catalog_uses_direct_basic_credential_and_normalizes_voices():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/voices/v1/voices"
        assert request.headers["Authorization"] == "Basic workspace-inworld-key-123456"
        return httpx.Response(
            200,
            json={
                "voices": [
                    {
                        "voiceId": "Ashley",
                        "displayName": "Ashley",
                        "langCode": "EN_US",
                        "tags": ["female", "American", "warm"],
                    },
                    {
                        "voiceId": "Layla",
                        "displayName": "Layla",
                        "langCode": "AR_SA",
                        "tags": ["female", "Gulf", "conversational"],
                    },
                    {
                        "voiceId": "Arjun",
                        "displayName": "Arjun",
                        "langCode": "hi-IN",
                        "tags": ["male", "Indian", "friendly"],
                    },
                ]
            },
        )

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )
    voices = await client.list_voices()

    assert voices[0]["id"] == "inworld:Ashley"
    assert voices[0]["languages"] == list(INWORLD_TTS_SUPPORTED_LANGUAGES)
    assert voices[0]["accent"] == "American"
    assert voices[0]["gender"] == "female"
    assert voices[1]["languages"] == list(INWORLD_TTS_SUPPORTED_LANGUAGES)
    assert voices[1]["accent"] == "Gulf"
    assert voices[2]["languages"] == list(INWORLD_TTS_SUPPORTED_LANGUAGES)
    assert voices[2]["gender"] == "male"
    assert voices[0]["synthesizer_model"] == "inworld-tts-2"
    assert voices[0]["use_cases"] == ["conversational", "multilingual"]

    # langCode is the native voice prompt/accent, not a TTS-2 language limit.
    for voice in voices:
        compatibility, unsupported = voice_language_compatibility(
            voices,
            voice["id"],
            ["en", "ar", "hi"],
        )
        assert compatibility == LanguageCompatibilityStatus.COMPATIBLE
        assert unsupported == []


def test_inworld_multilingual_metadata_does_not_weaken_generic_provider_checks():
    other_provider_voice = {
        "id": "other:english-only",
        "languages": ["en"],
        "synthesizer_model": "other-model",
    }

    compatibility, unsupported = voice_language_compatibility(
        [other_provider_voice],
        other_provider_voice["id"],
        ["en", "ar", "hi"],
    )

    assert compatibility == LanguageCompatibilityStatus.INCOMPATIBLE
    assert unsupported == ["ar", "hi"]


@pytest.mark.asyncio
async def test_inworld_preview_uses_documented_query_and_decodes_audio_content():
    preview = b"ID3\x04\x00\x00\x00\x00\x00\x15preview-audio"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tts/v1/voice:preview"
        assert dict(request.url.params) == {
            "voice_id": "Ashley",
            "model_id": "inworld-tts-2",
        }
        assert request.headers["Authorization"] == "Basic workspace-inworld-key-123456"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(
            200,
            json={"audioContent": base64.b64encode(preview).decode("ascii")},
        )

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )

    assert await client.voice_preview("Ashley") == preview


@pytest.mark.asyncio
async def test_inworld_readiness_probes_use_selected_routes_and_minimal_bounded_requests():
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x15ok"
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        requests.append((request.url.path, payload))
        assert request.headers["Authorization"] == "Basic workspace-inworld-key-123456"
        if request.url.path == "/tts/v1/voice":
            return httpx.Response(
                200,
                json={
                    "audioContent": base64.b64encode(audio).decode("ascii"),
                    "usage": {"processedCharactersCount": 3, "modelId": "inworld-tts-2"},
                },
            )
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "vav_readiness_check",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )
    await client.synthesize_readiness_probe(
        voice_id="Ashley",
        model_id="inworld-tts-2",
    )
    await client.router_readiness_probe(model_id="openai/gpt-4o-mini")

    assert requests == [
        (
            "/tts/v1/voice",
            {
                "text": "OK.",
                "voiceId": "Ashley",
                "modelId": "inworld-tts-2",
                "audioConfig": {
                    "audioEncoding": "MP3",
                    "bitRate": 32000,
                    "sampleRateHertz": 16000,
                },
                "deliveryMode": "STABLE",
                "applyTextNormalization": "OFF",
            },
        ),
        (
            "/v1/chat/completions",
            {
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": "Call vav_readiness_check now. Do not reply with text.",
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "vav_readiness_check",
                            "description": "Confirms tool-calling capability.",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "vav_readiness_check"},
                },
                "max_tokens": 64,
                "temperature": 0,
                "stream": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_inworld_tts_probe_accepts_snake_case_audio_response_fields():
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x15ok"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "audio_content": base64.b64encode(audio).decode("ascii"),
                "usage": {"model_id": "inworld-tts-2"},
            },
        )

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )

    await client.synthesize_readiness_probe(
        voice_id="Ashley",
        model_id="inworld-tts-2",
    )


@pytest.mark.asyncio
async def test_inworld_auto_router_probe_budgets_reasoning_and_requires_tool_call():
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(await request.aread()))
        return httpx.Response(
            200,
            json={
                "model": "deepinfra/MiniMaxAI/MiniMax-M2.5",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "vav_readiness_check",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                            "reasoning": "The user requested a bounded readiness response.",
                        },
                    }
                ],
                "usage": {
                    "completion_tokens": 67,
                    "completion_tokens_details": {"reasoning_tokens": 65},
                },
            },
        )

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )

    await client.router_readiness_probe(model_id="auto")

    assert captured["max_tokens"] == ROUTER_AUTO_PROBE_MAX_TOKENS
    assert captured["model"] == "auto"


@pytest.mark.asyncio
async def test_inworld_auto_router_probe_rejects_limit_and_missing_tool_calls():
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "", "reasoning": "Still reasoning."},
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "", "reasoning": "Internal only."},
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": [{"type": "text", "text": "OK"}]},
                        }
                    ]
                },
            ),
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(InworldError, match="exhausted its bounded output token budget"):
        await client.router_readiness_probe(model_id="auto")
    with pytest.raises(InworldError, match="required VAV knowledge tool call"):
        await client.router_readiness_probe(model_id="auto")
    with pytest.raises(InworldError, match="required VAV knowledge tool call"):
        await client.router_readiness_probe(model_id="auto")


@pytest.mark.asyncio
async def test_inworld_readiness_probes_bound_time_size_and_provider_errors():
    responses: list[object] = [
        httpx.ReadTimeout("too slow"),
        httpx.Response(200, content=b"x" * (MAX_PROBE_RESPONSE_BYTES + 1)),
        httpx.Response(422, json={"message": "selected Router model is unavailable"}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(InworldError, match="timed out after 10 seconds") as timeout_error:
        await client.router_readiness_probe(model_id="openai/gpt-4o-mini")
    assert timeout_error.value.status_code == 504

    with pytest.raises(InworldError, match="unexpectedly large response"):
        await client.router_readiness_probe(model_id="openai/gpt-4o-mini")

    with pytest.raises(InworldError, match="selected Router model is unavailable") as route_error:
        await client.router_readiness_probe(model_id="inworld/customer-support")
    assert route_error.value.status_code == 422


@pytest.mark.asyncio
async def test_inworld_preview_rejects_json_or_invalid_base64_as_audio():
    responses = iter(
        [
            httpx.Response(200, json={"voices": []}),
            httpx.Response(200, json={"audioContent": "not valid base64!"}),
            httpx.Response(
                200,
                json={"audioContent": base64.b64encode(b'{"not":"audio"}').decode("ascii")},
            ),
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = InworldClient(
        api_key="workspace-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(InworldError, match="without audio"):
        await client.voice_preview("Ashley")
    with pytest.raises(InworldError, match="invalid voice preview encoding"):
        await client.voice_preview("Ashley")
    with pytest.raises(InworldError, match="invalid voice preview"):
        await client.voice_preview("Ashley")


@pytest.mark.asyncio
async def test_inworld_validation_does_not_hide_provider_auth_failure():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid credential"}})

    client = InworldClient(
        api_key="invalid-inworld-key-123456",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(InworldError) as error:
        await client.validate_connection()

    assert error.value.status_code == 401
    assert "invalid credential" in str(error.value)
