"""Safe per-call configuration snapshots and their API projection."""

from typing import Any


def agent_configuration_snapshot(agent: Any) -> dict[str, Any]:
    """Capture the agent language/revision serving a call at dispatch time."""
    return {
        "provider_revision_id": getattr(agent, "provider_revision_id", None),
        "language": getattr(agent, "language", None),
        "supported_languages": list(getattr(agent, "supported_languages", None) or []),
        "language_switching_enabled": bool(getattr(agent, "language_switching_enabled", False)),
        "language_switching_mode": getattr(agent, "language_switching_mode", "disabled"),
    }


def public_call_metadata(value: Any) -> dict[str, Any] | None:
    """Return only non-secret operational metadata safe for workspace viewers."""
    if not isinstance(value, dict):
        return None
    snapshot = value.get("agent_configuration")
    if not isinstance(snapshot, dict):
        return None

    result: dict[str, Any] = {}
    conversation_type = value.get("conversation_type")
    if conversation_type in {"webcall", "chat", "telephonyInbound", "telephonyOutbound"}:
        result["conversation_type"] = conversation_type
    channel = value.get("channel")
    if channel in {"browser", "chat", "phone"}:
        result["channel"] = channel
    revision_id = snapshot.get("provider_revision_id")
    if isinstance(revision_id, str) and revision_id:
        result["provider_revision_id"] = revision_id
    language = snapshot.get("language")
    if isinstance(language, str) and language:
        result["language"] = language
    supported_languages = snapshot.get("supported_languages")
    if isinstance(supported_languages, list):
        result["supported_languages"] = [
            language for language in supported_languages if isinstance(language, str) and language
        ][:50]
    switching_enabled = snapshot.get("language_switching_enabled")
    if isinstance(switching_enabled, bool):
        result["language_switching_enabled"] = switching_enabled
    switching_mode = snapshot.get("language_switching_mode")
    if switching_mode in {"disabled", "automatic"}:
        result["language_switching_mode"] = switching_mode
    speech_provider = value.get("speech_provider")
    if speech_provider in {"smallest", "sarvam", "elevenlabs", "inworld"}:
        result["speech_provider"] = speech_provider
    runtime = value.get("runtime")
    if isinstance(runtime, dict):
        safe_runtime: dict[str, Any] = {}
        string_fields = {
            "speech_provider",
            "llm_provider",
            "llm_model",
            "stt_language",
            "cost_state",
        }
        numeric_fields = {
            "max_duration_seconds",
            "turn_count",
            "llm_tokens",
            "inbound_audio_bytes",
            "outbound_audio_bytes",
            "barge_in_count",
            "last_llm_latency_ms",
            "last_llm_first_token_ms",
            "last_tts_first_byte_ms",
            "last_transcript_to_first_audio_ms",
            "last_speech_end_to_first_audio_ms",
            "turn_latency_p50_ms",
            "turn_latency_p95_ms",
        }
        for field in string_fields:
            if isinstance(runtime.get(field), str):
                safe_runtime[field] = runtime[field]
        for field in numeric_fields:
            if isinstance(runtime.get(field), (int, float)):
                safe_runtime[field] = runtime[field]
        if safe_runtime:
            result["runtime"] = safe_runtime
    return result or None
