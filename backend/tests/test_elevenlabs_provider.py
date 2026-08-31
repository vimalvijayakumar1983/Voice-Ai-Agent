import json

import httpx
import pytest

from app.core.config import settings
from app.models.agent import Agent
from app.providers.elevenlabs import ElevenLabsClient, ElevenLabsError
from app.realtime import elevenlabs_stream
from app.realtime.elevenlabs_stream import ElevenLabsTTSStream


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
    assert requests[0].url.raw_path.startswith(
        b"/v1/text-to-speech/voice%2Fwith%20slash/stream"
    )
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
async def test_elevenlabs_agent_runtime_uses_vav_readiness_pipeline(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "elevenlabs-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key-long-enough")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://voice.example.com")
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
    assert configured.json()["ready"] is True
    assert tested.status_code == 200
    assert tested.json()["checks"]["stt_credential"] is True
    assert tested.json()["checks"]["tts_credential"] is True
    assert tested.json()["checks"]["speech_provider_match"] is True
