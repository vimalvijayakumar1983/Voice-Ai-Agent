import json

import httpx
import pytest

from app.api.v1.endpoints import runtime as runtime_endpoint
from app.core.config import settings
from app.models.agent import Agent
from app.providers.elevenlabs import ElevenLabsClient, ElevenLabsError
from app.realtime import elevenlabs_stream
from app.realtime.elevenlabs_stream import ElevenLabsTTSStream
from app.realtime.session import audio_with_fallback
from tests.knowledge_test_utils import publish_test_knowledge


@pytest.mark.asyncio
async def test_elevenlabs_voice_catalog_is_paginated_and_namespaced():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        token = request.url.params.get("next_page_token")
        if token:
            return httpx.Response(
                200,
                json={
                    "voices": [
                        {
                            "voice_id": "voice-b",
                            "name": "British Support",
                            "category": "professional",
                            "labels": {"accent": "british", "gender": "female"},
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "voices": [{"voice_id": "voice-a", "name": "Concierge"}],
                "has_more": True,
                "next_page_token": "page-2",
            },
        )

    client = ElevenLabsClient(
        api_key="elevenlabs_test_key_123456789",
        transport=httpx.MockTransport(handler),
    )

    voices = await client.list_voices()

    assert [voice["id"] for voice in voices] == [
        "elevenlabs:voice-a",
        "elevenlabs:voice-b",
    ]
    assert voices[1]["accent"] == "british"
    assert voices[1]["voice_pool"] == "pro"
    assert "en" in voices[1]["languages"]
    assert requests[0].headers["xi-api-key"] == "elevenlabs_test_key_123456789"
    assert requests[1].url.params["next_page_token"] == "page-2"


@pytest.mark.asyncio
async def test_elevenlabs_connection_validation_is_bounded_and_credit_free():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"voices": [], "has_more": False})

    client = ElevenLabsClient(
        api_key="elevenlabs_test_key_123456789",
        transport=httpx.MockTransport(handler),
    )

    await client.validate_connection()

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v2/voices"
    assert requests[0].url.params["page_size"] == "1"


@pytest.mark.asyncio
async def test_elevenlabs_preview_uses_flash_model_and_bounded_speed():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"ID3-preview")

    client = ElevenLabsClient(
        api_key="elevenlabs_test_key_123456789",
        transport=httpx.MockTransport(handler),
    )
    audio = await client.synthesize_voice_preview(
        voice_id="voice/with slash",
        language="en-GB",
        speed=2,
    )

    assert audio == b"ID3-preview"
    assert requests[0].url.raw_path.startswith(b"/v1/text-to-speech/voice%2Fwith%20slash/stream")
    payload = json.loads(requests[0].content)
    assert payload["model_id"] == "eleven_flash_v2_5"
    assert payload["language_code"] == "en"
    assert payload["voice_settings"]["speed"] == 1.2


@pytest.mark.asyncio
async def test_elevenlabs_preview_fails_closed_without_server_credential():
    with pytest.raises(ElevenLabsError) as caught:
        await ElevenLabsClient(api_key="").synthesize_voice_preview(
            voice_id="voice-a",
            language="en",
        )

    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_elevenlabs_realtime_stream_requests_twilio_native_audio(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.sent: list[str] = []
            self.messages = iter(
                [
                    json.dumps({"audio": "YXVkaW8=", "is_final": False}),
                    json.dumps({"is_final": True}),
                ]
            )

        async def send(self, value: str):
            self.sent.append(value)

        async def recv(self):
            return next(self.messages)

        async def close(self):
            return None

    connection = FakeConnection()
    connection_args: dict = {}

    async def fake_connect(url: str, **kwargs):
        connection_args.update({"url": url, **kwargs})
        return connection

    monkeypatch.setattr(elevenlabs_stream, "connect", fake_connect)
    stream = ElevenLabsTTSStream(
        api_key="elevenlabs_test_key_123456789",
        base_url="https://api.elevenlabs.io",
        voice_id="voice-a",
        speed=1,
    )

    async def fragments():
        yield "Hello there."

    audio = [chunk async for chunk in stream.audio_for(fragments(), language_code="en-GB")]

    assert audio == ["YXVkaW8="]
    assert "output_format=ulaw_8000" in connection_args["url"]
    assert "model_id=eleven_flash_v2_5" in connection_args["url"]
    assert connection_args["additional_headers"]["xi-api-key"].startswith("elevenlabs_")
    sent = [json.loads(value) for value in connection.sent]
    assert sent[0]["voice_settings"]["speed"] == 1
    assert sent[-1] == {"text": ""}


@pytest.mark.asyncio
async def test_elevenlabs_empty_stream_fails_instead_of_leaving_caller_silent(monkeypatch):
    class EmptyConnection:
        async def send(self, value: str):
            return None

        async def recv(self):
            return json.dumps({"is_final": True})

        async def close(self):
            return None

    async def fake_connect(url: str, **kwargs):
        return EmptyConnection()

    monkeypatch.setattr(elevenlabs_stream, "connect", fake_connect)
    stream = ElevenLabsTTSStream(
        api_key="elevenlabs_test_key_123456789",
        base_url="https://api.elevenlabs.io",
        voice_id="voice-a",
        speed=1,
    )

    async def fragments():
        yield "Hello there."

    with pytest.raises(elevenlabs_stream.ElevenLabsStreamError) as caught:
        _ = [chunk async for chunk in stream.audio_for(fragments(), language_code="en")]

    assert str(caught.value) == "ElevenLabs returned no speech audio"


@pytest.mark.asyncio
async def test_tts_failure_before_audio_replays_complete_text_through_fallback():
    events: list[str] = []

    class FailingPrimary:
        async def audio_for(self, fragments, *, language_code: str):
            async for fragment in fragments:
                events.append(f"primary:{fragment}")
            raise RuntimeError("primary unavailable")
            yield ""  # pragma: no cover

    class WorkingFallback:
        async def audio_for(self, fragments, *, language_code: str):
            async for fragment in fragments:
                events.append(f"fallback:{fragment}")
            yield "ZmFsbGJhY2s="

    async def fragments():
        yield "Complete "
        yield "answer."

    failures: list[str] = []
    fallbacks: list[str] = []
    audio = [
        chunk
        async for chunk in audio_with_fallback(
            FailingPrimary(),
            WorkingFallback(),
            fragments(),
            language_code="en",
            on_primary_failure=lambda: failures.append("failed"),
            on_fallback=lambda: fallbacks.append("used"),
        )
    ]

    assert audio == ["ZmFsbGJhY2s="]
    assert events == [
        "primary:Complete ",
        "primary:answer.",
        "fallback:Complete ",
        "fallback:answer.",
    ]
    assert failures == ["failed"]
    assert fallbacks == ["used"]


@pytest.mark.asyncio
async def test_elevenlabs_agent_runtime_uses_vav_readiness_pipeline(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    async def verify_route(**_kwargs):
        return None

    async def synthesize_ok(self, *, voice_id: str, language: str, speed: float = 1.0):
        assert voice_id == "voice-a"
        assert language == "en"
        return b"preview"

    async def sarvam_synthesize_ok(*_args, **_kwargs):
        return b"RIFFaudio"

    async def readiness_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ElevenLabsClient, "synthesize_voice_preview", synthesize_ok)
    monkeypatch.setattr(
        runtime_endpoint.SarvamAIClient,
        "synthesize_voice_preview",
        sarvam_synthesize_ok,
    )
    monkeypatch.setattr(runtime_endpoint, "_sarvam_stt_readiness_probe", readiness_ok)
    monkeypatch.setattr(
        runtime_endpoint.OpenAIProviderClient,
        "tool_readiness_probe",
        readiness_ok,
    )
    monkeypatch.setattr(runtime_endpoint, "verify_twilio_route_ownership", verify_route)
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "elevenlabs-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key-long-enough")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://voice.example.com")
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": "AC" + "3" * 32,
            "auth_token": "workspace-twilio-test-token",
            "default_from_number": "+15551234567",
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="ElevenLabs concierge",
        system_prompt="Answer using VAV knowledge and concise phone responses.",
        voice_provider="elevenlabs",
        voice_id="elevenlabs:voice-a",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="ElevenLabs clinic",
    )
    await db.commit()

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+15551234567"],
            "telephony_provider": "twilio",
            "primary_speech_provider": "elevenlabs",
            "fallback_speech_provider": None,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "stt_language": "auto",
            "max_concurrent_calls": 1,
            "daily_call_limit": 50,
            "monthly_budget_cents": 10000,
        },
    )
    tested = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/test",
        headers=auth_headers,
    )

    assert configured.status_code == 200
    assert configured.json()["primary_speech_provider"] == "elevenlabs"
    assert configured.json()["ready"] is False
    assert tested.status_code == 200
    assert tested.json()["checks"]["stt_credential"] is True
    assert tested.json()["checks"]["tts_credential"] is True
    assert tested.json()["checks"]["speech_provider_match"] is True
    assert tested.json()["checks"]["tts_provider_live"] is True
    assert tested.json()["checks"]["fallback_tts_provider_live"] is True
    assert tested.json()["checks"]["stt_provider_live"] is True
    assert tested.json()["checks"]["llm_provider_live"] is True


@pytest.mark.asyncio
async def test_elevenlabs_live_readiness_fails_closed_when_synthesis_fails(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    async def verify_route(**_kwargs):
        return None

    async def synthesize_failed(self, *, voice_id: str, language: str, speed: float = 1.0):
        raise ElevenLabsError("selected voice is unavailable", status_code=403)

    async def sarvam_synthesize_ok(*_args, **_kwargs):
        return b"RIFFaudio"

    async def readiness_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ElevenLabsClient, "synthesize_voice_preview", synthesize_failed)
    monkeypatch.setattr(
        runtime_endpoint.SarvamAIClient,
        "synthesize_voice_preview",
        sarvam_synthesize_ok,
    )
    monkeypatch.setattr(runtime_endpoint, "_sarvam_stt_readiness_probe", readiness_ok)
    monkeypatch.setattr(
        runtime_endpoint.OpenAIProviderClient,
        "tool_readiness_probe",
        readiness_ok,
    )
    monkeypatch.setattr(runtime_endpoint, "verify_twilio_route_ownership", verify_route)
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "elevenlabs-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key-long-enough")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://voice.example.com")
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": "AC" + "4" * 32,
            "auth_token": "workspace-twilio-test-token",
            "default_from_number": "+15551234567",
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="Broken ElevenLabs voice",
        system_prompt="Help callers.",
        voice_provider="elevenlabs",
        voice_id="elevenlabs:voice-a",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Broken voice clinic",
    )
    await db.commit()
    await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={"assigned_numbers": ["+15551234567"], "primary_speech_provider": "elevenlabs"},
    )

    tested = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/test",
        headers=auth_headers,
    )

    assert tested.status_code == 200
    assert tested.json()["ready"] is False
    assert tested.json()["checks"]["tts_provider_live"] is False
    assert tested.json()["checks"]["fallback_tts_provider_live"] is True
    assert tested.json()["checks"]["stt_provider_live"] is True
    assert tested.json()["checks"]["llm_provider_live"] is True
    assert tested.json()["blockers"] == [
        "ElevenLabs live TTS synthesis failed: selected voice is unavailable"
    ]
