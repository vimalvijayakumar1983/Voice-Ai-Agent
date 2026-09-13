"""Soniox speech components composed with the existing LiveKit session."""

from livekit.plugins import silero, soniox

from app.providers.soniox import SONIOX_STT_MODEL, SONIOX_TTS_MODEL
from app.services.realtime_speech_config import resolve_inworld_stt_language

PAUSE_SAFE_FLAG = "soniox_pause_safe_v1"


def pause_safe_enabled(profile) -> bool:
    """Explicit opt-in only; never interpret strings or cross-provider flags."""
    raw = getattr(profile, "runtime_config", None)
    config = raw if isinstance(raw, dict) else {}
    return (
        config.get(PAUSE_SAFE_FLAG) is True
        and getattr(profile, "primary_speech_provider", None) == "soniox"
        and str(config.get("voice_runtime") or "pipeline") == "pipeline"
    )


def input_sample_rate(profile) -> int:
    return 48000 if pause_safe_enabled(profile) else 16000


def endpointing(profile) -> dict:
    # Native SDK delay, not a second turn model or a transcript rewrite. Allow
    # short in-sentence pauses without the previous audio model's 2.5 s tail.
    return {
        "mode": "fixed",
        "min_delay": 0.8 if pause_safe_enabled(profile) else 0.3,
        "max_delay": 0.8,
    }


def input_diagnostics(profile) -> dict:
    return {
        "soniox_pause_safe_enabled": pause_safe_enabled(profile),
        "soniox_turn_completion_mode": "stt_pause_safe_v1"
        if pause_safe_enabled(profile)
        else "stt_standard",
        "stt_sample_rate_configured": input_sample_rate(profile),
        "stt_max_endpoint_delay_ms_configured": 1000,
        "turn_endpointing_configured": endpointing(profile),
    }


def speech_language(model) -> str:
    return str(model.language or "en").split("-")[0].lower()


def tts_options(model, api_key: str) -> dict:
    return {
        "api_key": api_key,
        "model": SONIOX_TTS_MODEL,
        "voice": model.voice_id.removeprefix("soniox:"),
        "language": speech_language(model),
        "speed": max(0.7, min(1.3, float(model.speech_rate or 1.0))),
        "sample_rate": 24000,
    }


def language_hints(model, profile) -> list[str]:
    effective = resolve_inworld_stt_language(model=model, profile=profile)
    languages = (model.supported_languages if effective == "auto" else [effective]) or [
        model.language or "en"
    ]
    return list(dict.fromkeys(str(item).split("-")[0].lower() for item in languages))


def build_stt(model, profile, api_key: str, terminology=()):
    return soniox.STT(
        api_key=api_key,
        params=soniox.STTOptions(
            model=SONIOX_STT_MODEL,
            sample_rate=input_sample_rate(profile),
            language_hints=language_hints(model, profile),
            language_hints_strict=True,
            context=soniox.ContextObject(terms=list(terminology)[:100]),
            max_endpoint_delay_ms=1000,
        ),
    )


def build_vad():
    return silero.VAD.load()


async def gate_unchecked_text(chunks, metrics):
    """Keep planning/tool calls and usage, but never synthesize unchecked MCP prose."""
    from livekit.agents import llm

    async for chunk in chunks:
        if isinstance(chunk, str):
            if chunk:
                metrics["mcp_unchecked_text_chunks_blocked"] = (
                    metrics.get("mcp_unchecked_text_chunks_blocked", 0) + 1
                )
            continue
        if isinstance(chunk, llm.ChatChunk) and chunk.delta and chunk.delta.content:
            metrics["mcp_unchecked_text_chunks_blocked"] = (
                metrics.get("mcp_unchecked_text_chunks_blocked", 0) + 1
            )
            chunk = chunk.model_copy(
                update={"delta": chunk.delta.model_copy(update={"content": None})}
            )
        yield chunk
