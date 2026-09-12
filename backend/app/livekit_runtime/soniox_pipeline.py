"""Soniox speech components composed with the existing LiveKit session."""

from livekit.plugins import silero, soniox

from app.providers.soniox import SONIOX_STT_MODEL, SONIOX_TTS_MODEL
from app.services.realtime_speech_config import resolve_inworld_stt_language


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
