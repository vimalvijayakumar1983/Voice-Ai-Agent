from types import SimpleNamespace

import pytest

from app.livekit_runtime.worker import _build_inworld_realtime_model
from app.services.realtime_speech_config import inworld_transcription_language_hint


def config(language="en-GB", *, enabled=True, stt="assemblyai/u3-rt-pro"):
    return (
        SimpleNamespace(
            agent_metadata={"conversation_foundation_v1": enabled},
            name="Example Support",
            voice_id="inworld:Ashley",
            speech_rate=1.0,
            language=language,
            supported_languages=["en", "fr"],
            language_switching_enabled=True,
        ),
        SimpleNamespace(
            stt_language=language, llm_model="openai/gpt-4o-mini", runtime_config={"stt_model": stt}
        ),
    )


@pytest.mark.parametrize(
    "language,name", [("en-GB", "English"), ("fr", "French"), ("de", "German")]
)
def test_supported_language_uses_provider_prompt_guidance(language, name):
    model, profile = config(language)
    assert inworld_transcription_language_hint(model=model, profile=profile).startswith(
        f"Transcribe {name}."
    )


@pytest.mark.parametrize(
    "options",
    [
        {"enabled": False},
        {"enabled": "true"},
        {"language": "auto"},
        {"language": "ar"},
        {"stt": "inworld/inworld-stt-1"},
    ],
)
def test_no_silent_language_or_provider_switch(options):
    model, profile = config(**options)
    assert inworld_transcription_language_hint(model=model, profile=profile) == ""


@pytest.mark.asyncio
async def test_qa_prompt_guidance_is_in_serialized_transcription_without_changing_route(
    monkeypatch,
):
    from app.livekit_runtime.inworld_realtime import InworldRealtimeSession

    async def no_network(_session):
        return None

    monkeypatch.setattr(InworldRealtimeSession, "_main_task", no_network)
    model, profile = config()
    runtime = _build_inworld_realtime_model(model=model, profile=profile, api_key="test")
    transcription = runtime._opts.input_audio_transcription
    assert transcription.model == "assemblyai/u3-rt-pro"
    assert transcription.language == "en-GB"
    assert transcription.prompt.startswith("Transcribe English.")
    assert "Example Support" in transcription.prompt
    session = runtime.session()
    payload = session._create_session_update_event()["session"]["audio"]["input"]["transcription"]
    assert payload["prompt"].startswith("Transcribe English.")
    assert payload["language"] == "en-GB"
    await session.aclose()
