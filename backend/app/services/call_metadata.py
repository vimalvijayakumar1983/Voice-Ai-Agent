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
        "post_call_analysis_mode": getattr(agent, "post_call_analysis_mode", "provider_first"),
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
    analysis_mode = snapshot.get("post_call_analysis_mode")
    if analysis_mode in {"provider_first", "vav_ai", "disabled"}:
        result["post_call_analysis_mode"] = analysis_mode
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
            "voice_runtime",
            "stt_model",
            "stt_language",
            "cost_state",
        }
        numeric_fields = {
            "max_duration_seconds",
            "turn_count",
            "llm_tokens",
            "llm_input_tokens",
            "llm_output_tokens",
            "llm_input_audio_tokens",
            "llm_output_audio_tokens",
            "llm_input_text_tokens",
            "llm_output_text_tokens",
            "realtime_session_seconds",
            "tts_characters",
            "tts_audio_seconds",
            "stt_audio_seconds",
            "inbound_audio_bytes",
            "outbound_audio_bytes",
            "barge_in_count",
            "stale_generation_cancel_count",
            "suppressed_fragment_count",
            "last_suppressed_fragment_words",
            "fragment_continuation_window_ms",
            "knowledge_terminology_load_ms",
            "knowledge_terminology_count",
            "knowledge_terminology_total_count",
            "knowledge_lookup_count",
            "knowledge_match_count",
            "knowledge_no_match_count",
            "knowledge_error_count",
            "unsupported_knowledge_response_count",
            "last_knowledge_tool_ms",
            "session_connection_ms",
            "call_open_to_greeting_ms",
            "session_start_to_greeting_ms",
            "last_llm_latency_ms",
            "last_llm_first_token_ms",
            "last_tts_first_byte_ms",
            "last_end_of_utterance_ms",
            "last_transcription_delay_ms",
            "last_knowledge_hook_ms",
            "last_interruption_detection_ms",
            "last_transcript_to_first_audio_ms",
            "last_speech_end_to_first_audio_ms",
            "turn_latency_p50_ms",
            "turn_latency_p90_ms",
            "turn_latency_p95_ms",
        }
        for field in string_fields:
            if isinstance(runtime.get(field), str):
                safe_runtime[field] = runtime[field]
        for field in numeric_fields:
            if isinstance(runtime.get(field), (int, float)):
                safe_runtime[field] = runtime[field]
        turn_diagnostics = runtime.get("turn_diagnostics")
        if isinstance(turn_diagnostics, list):
            safe_turns: list[dict[str, Any]] = []
            numeric_trace_fields = {
                "turn",
                "user_speech_ms",
                "transcript_words",
                "transcript_after_speech_ms",
                "stabilization_ms",
                "transcript_to_first_audio_ms",
                "speech_end_to_first_audio_ms",
                "llm_first_token_ms",
                "tts_first_byte_ms",
                "end_of_utterance_ms",
                "transcription_delay_ms",
                "knowledge_hook_ms",
                "knowledge_tool_ms",
                "knowledge_evidence_chars",
                "knowledge_query_variant_count",
                "interruption_detection_ms",
            }
            for trace in turn_diagnostics[-50:]:
                if not isinstance(trace, dict):
                    continue
                safe_trace: dict[str, Any] = {}
                for field in numeric_trace_fields:
                    if isinstance(trace.get(field), (int, float)) and not isinstance(
                        trace.get(field), bool
                    ):
                        safe_trace[field] = trace[field]
                if isinstance(trace.get("barge_in"), bool):
                    safe_trace["barge_in"] = trace["barge_in"]
                for field in ("tool_call", "knowledge_fallback_used"):
                    if isinstance(trace.get(field), bool):
                        safe_trace[field] = trace[field]
                if trace.get("knowledge_result") in {"verified", "no_match", "error"}:
                    safe_trace["knowledge_result"] = trace["knowledge_result"]
                if trace.get("grounding_outcome") in {
                    "verified_answer",
                    "no_match_correctly_refused",
                    "no_match_clarification",
                    "no_match_unverified_response",
                    "knowledge_error_response",
                }:
                    safe_trace["grounding_outcome"] = trace["grounding_outcome"]
                if trace.get("outcome") in {
                    "answered",
                    "fragment_suppressed",
                    "superseded_by_caller",
                }:
                    safe_trace["outcome"] = trace["outcome"]
                if safe_trace:
                    safe_turns.append(safe_trace)
            if safe_turns:
                safe_runtime["turn_diagnostics"] = safe_turns
        if safe_runtime:
            result["runtime"] = safe_runtime
    return result or None
