"""Direct Soniox boundary, tenant isolation and pipeline contract regression tests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from livekit.agents import llm
from pydantic import ValidationError

from app.core.config import settings
from app.livekit_runtime import soniox_pipeline, worker
from app.models.agent import Agent
from app.models.call import Call
from app.providers import soniox as soniox_provider
from app.providers.soniox import SonioxClient, SonioxError
from app.schemas.runtime import RuntimeProfileUpdate
from app.services.cost_reporting import _call_components
from app.services.production_voice_preset import new_soniox_profile
from app.services.provider_credentials import load_provider_config
from app.services.realtime_speech_config import resolve_inworld_stt_model


def model(**changes):
    return SimpleNamespace(
        **{
            "language": "en-GB",
            "supported_languages": ["en-GB", "ar-AE", "hi-IN"],
            "language_switching_enabled": True,
            "speech_rate": 1.0,
            "voice_id": "soniox:Maya",
            **changes,
        }
    )


def profile(**changes):
    return SimpleNamespace(
        **{
            "stt_language": "auto",
            "primary_speech_provider": "soniox",
            "runtime_config": {"stt_model": "stt-rt-v5"},
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_catalog_and_key_are_direct_soniox_only():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.soniox.com"
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"id": "stt-rt-v5"}]})
        if request.url.path.endswith("/tts-models"):
            return httpx.Response(
                200,
                json={
                    "models": [{"id": "tts-rt-v1", "languages": [{"code": "en"}, {"code": "ar"}]}]
                },
            )
        return httpx.Response(200, json={"voices": [{"id": "Maya", "gender": "female"}]})

    client = SonioxClient(api_key="test-secret", transport=httpx.MockTransport(handler))
    await client.validate_connection()
    voices = await client.list_voices()
    assert voices[0]["id"] == "soniox:Maya"
    assert voices[0]["languages"] == ["en", "ar"]
    assert voices[0]["synthesizer_model"] == "tts-rt-v1"
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 402, 429, 500])
async def test_provider_failure_never_echoes_secrets(status):
    client = SonioxClient(
        api_key="test-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="test-secret provider details")
        ),
    )
    with pytest.raises(SonioxError) as failure:
        await client.validate_connection()
    assert "test-secret" not in str(failure.value)


@pytest.mark.asyncio
async def test_missing_model_is_not_reported_ready():
    client = SonioxClient(
        api_key="key",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []})),
    )
    with pytest.raises(SonioxError, match="does not expose"):
        await client.validate_connection()


@pytest.mark.asyncio
async def test_voice_preview_is_fixed_bounded_audio_not_user_text():
    def handler(request):
        assert request.url == "https://tts-rt.soniox.com/tts"
        data = json.loads(request.content)
        assert data["model"] == "tts-rt-v1"
        assert data["voice"] == "Maya"
        assert data["language"] == "en"
        assert data["audio_format"] == "wav"
        assert data["text"].startswith("Hello, I am your voice assistant.")
        return httpx.Response(200, content=b"RIFF" + bytes(4) + b"WAVE" + bytes(50))

    client = SonioxClient(api_key="key", transport=httpx.MockTransport(handler))
    assert len(await client.voice_preview(voice_id="soniox:Maya", language="en-GB")) > 44


@pytest.mark.asyncio
async def test_preview_rejects_success_response_with_no_audio():
    client = SonioxClient(
        api_key="key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"message": "not audio"})
        ),
    )
    with pytest.raises(SonioxError, match="no valid"):
        await client.voice_preview(voice_id="Maya", language="en")


def test_language_configuration_and_wire_model_agree():
    assert soniox_pipeline.language_hints(model(), profile()) == ["en", "ar", "hi"]
    assert soniox_pipeline.language_hints(model(), profile(stt_language="en-GB")) == ["en"]
    assert resolve_inworld_stt_model(model=model(), profile=profile()) == "stt-rt-v5"
    opts = soniox_pipeline.tts_options(model(), "test-secret")
    assert opts["model"] == "tts-rt-v1" and opts["voice"] == "Maya"
    assert opts["language"] == "en" and opts["speed"] == 1.0


@pytest.mark.asyncio
async def test_stt_uses_livekit_plugin_and_approved_terms(monkeypatch):
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(soniox_pipeline.soniox, "STT", build)
    soniox_pipeline.build_stt(model(), profile(), "key", ("Al Zaabi", "PRP"))
    params = captured["params"]
    assert params.model == "stt-rt-v5"
    assert params.language_hints_strict is True
    assert params.context.terms == ["Al Zaabi", "PRP"]
    assert params.max_endpoint_delay_ms == 1000


@pytest.mark.parametrize(
    "changes",
    [
        {"telephony_provider": "twilio"},
        {"llm_provider": "inworld", "llm_model": "openai/gpt-4o-mini"},
        {"voice_runtime": "inworld_realtime"},
        {"stt_model": "assemblyai/u3-rt-pro"},
        {"knowledge_turn_mode": "single_pass_experimental"},
    ],
)
def test_soniox_rejects_mixed_provider_routes(changes):
    config = {"primary_speech_provider": "soniox", "telephony_provider": "livekit_sip"}
    with pytest.raises(ValidationError):
        RuntimeProfileUpdate(**(config | changes))


def test_new_profile_and_private_browser_policy():
    agent = Agent(id=uuid4(), tenant_id=uuid4(), voice_provider="soniox")
    result = new_soniox_profile(agent)
    assert not result.enabled
    assert result.llm_provider == "openai"
    assert result.runtime_config["voice_runtime"] == "pipeline"
    assert result.primary_speech_provider == "soniox"
    RuntimeProfileUpdate(
        primary_speech_provider="soniox",
        telephony_provider="livekit_sip",
        staff_browser_only=True,
        knowledge_source_mode="tools_only",
    )


@pytest.mark.asyncio
async def test_credentials_do_not_load_inworld(monkeypatch):
    lookup = AsyncMock(side_effect=[{"api_key": "soniox-key"}, {"api_key": "openai-key"}])
    monkeypatch.setattr(worker, "load_provider_config", lookup)
    keys = await worker._load_runtime_api_keys(
        None, tenant_id=uuid4(), llm_provider="openai", speech_provider="soniox"
    )
    assert keys.speech == "soniox-key" and keys.llm == "openai-key"
    assert [call.args[2] for call in lookup.await_args_list] == ["soniox", "openai"]


@pytest.mark.asyncio
async def test_bad_tenant_key_does_not_fall_back_to_platform(monkeypatch):
    monkeypatch.setattr(settings, "soniox_api_key", "platform-secret")
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": ""}))
    with pytest.raises(RuntimeError, match="workspace credential is invalid"):
        await worker._load_runtime_api_keys(
            None, tenant_id=uuid4(), llm_provider="openai", speech_provider="soniox"
        )


@pytest.mark.asyncio
async def test_checked_mcp_pipeline_blocks_text_but_preserves_tool_calls():
    tool = llm.FunctionToolCall(name="vav_answer", arguments="{}", call_id="call-1")
    candidate = llm.ChatChunk(
        id="chunk", delta=llm.ChoiceDelta(content="Unverified sales figure", tool_calls=[tool])
    )

    async def chunks():
        yield candidate
        yield "another unverified claim"

    metrics = {}
    result = [chunk async for chunk in soniox_pipeline.gate_unchecked_text(chunks(), metrics)]
    assert len(result) == 1 and result[0].delta.content is None
    assert result[0].delta.tool_calls == [tool]
    assert candidate.delta.content == "Unverified sales figure"
    assert metrics["mcp_unchecked_text_chunks_blocked"] == 2


@pytest.mark.asyncio
async def test_soniox_key_saved_write_only_and_removed(
    client, auth_headers, db, tenant, monkeypatch
):
    monkeypatch.setattr(SonioxClient, "validate_connection", AsyncMock())
    key = "soniox-private-key-1234567890"
    response = await client.put(
        "/api/v1/runtime/credentials/soniox", headers=auth_headers, json={"api_key": key}
    )
    assert response.status_code == 200, response.text
    assert key not in response.text
    assert (await load_provider_config(db, tenant.id, "soniox"))["api_key"] == key
    response = await client.delete("/api/v1/runtime/credentials/soniox", headers=auth_headers)
    assert response.status_code in {200, 204}


def test_soniox_cost_is_not_misreported_as_inworld_or_free():
    call = Call(
        provider="livekit_webrtc",
        duration_seconds=60,
        call_metadata={
            "runtime": {
                "speech_provider": "soniox",
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "llm_input_tokens": 1000,
                "llm_output_tokens": 100,
            }
        },
    )
    components, missing = _call_components(call, None, None)
    assert any("Soniox" in item for item in missing)
    assert not any(item["provider"] in {"Inworld", "Sarvam"} for item in components)
    assert any(item["provider"] == "OpenAI" for item in components)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,success",
    [
        ({"finished": True}, True),
        ({"error_code": 401, "error_message": "secret-key"}, False),
        ({"tokens": []}, False),
    ],
)
async def test_stream_probe_requires_provider_completion(monkeypatch, reply, success):
    sent = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, value):
            sent.append(value)

        async def __aiter__(self):
            yield json.dumps(reply)

    def connect(url, **options):
        assert url == "wss://stt-rt.soniox.com/transcribe-websocket"
        assert options["max_size"] == 256 * 1024
        return Socket()

    monkeypatch.setattr(soniox_provider, "connect", connect)
    operation = SonioxClient(api_key="secret-key").stt_readiness_probe(languages=["en"])
    if success:
        await operation
    else:
        with pytest.raises(SonioxError) as failure:
            await operation
        assert "secret-key" not in str(failure.value)
    wire = json.loads(sent[0])
    assert wire["model"] == "stt-rt-v5"
    assert wire["language_hints"] == ["en"]
    assert wire["language_hints_strict"] is True
    assert isinstance(sent[1], bytes) and sent[-1] == ""


@pytest.mark.asyncio
async def test_malformed_catalog_fails_with_readable_error():
    client = SonioxClient(
        api_key="key",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [None]})),
    )
    with pytest.raises(SonioxError, match="metadata"):
        await client.validate_connection()
