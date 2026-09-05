"""Safe per-call configuration snapshots and their API projection."""

import hashlib
from typing import Any


def public_transport_identity_ref(label: str, value: object) -> str | None:
    """Return a stable, non-reversible reference for a private transport ID."""

    normalized_label = str(label or "").strip().casefold()
    normalized_value = str(value or "").strip()
    if not normalized_label or not normalized_value:
        return None
    digest = hashlib.sha256(f"{normalized_label}:{normalized_value}".encode()).hexdigest()
    return f"{normalized_label}:sha256:{digest}"


def agent_configuration_snapshot(agent: Any) -> dict[str, Any]:
    """Capture the agent language/revision serving a call at dispatch time."""
    return {
        "knowledge_company_scope": getattr(agent, "knowledge_company_scope", None),
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
    livekit_room_ref = public_transport_identity_ref("livekit_room", value.get("livekit_room"))
    if livekit_room_ref is not None:
        # The raw room name is a private provider locator.  A replay harness
        # still needs to prove that the API call row belongs to the exact room
        # whose scoped token it received, so expose only a deterministic hash.
        result["livekit_room_ref"] = livekit_room_ref
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
            "knowledge_turn_mode",
            "stt_model",
            "stt_language",
            "stt_session_update_serialized_model",
            "stt_session_update_serialized_language",
            "stt_session_update_serialized_at",
            "audio_latency_observation_point",
            "audio_latency_unobserved_segments",
            "stt_provider_reported_language",
            "usage_source",
            "external_tts_usage_source",
            "cost_state",
            "greeting_cache_status",
            "greeting_shared_cache_lookup_status",
            "greeting_shared_cache_store_status",
            "speech_lexicon_source",
            "speech_lexicon_artifact_id",
            "speech_lexicon_content_sha256",
            "speech_lexicon_compiler_version",
            "speech_lexicon_source_revision_sha256",
            "knowledge_serving_revision_id",
            "knowledge_serving_knowledge_base_id",
            "knowledge_serving_content_sha256",
            "knowledge_source_revision_sha256",
            "knowledge_admission_state",
            "knowledge_admitted_at",
            "last_single_pass_outcome",
            "last_single_pass_error_type",
            "recording_requested_mode",
            "recording_effective_mode",
            "recording_state",
            "recording_blocker",
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
            "knowledge_serving_revocation_generation",
            "knowledge_lookup_count",
            "knowledge_match_count",
            "knowledge_no_match_count",
            "knowledge_error_count",
            "unsupported_knowledge_response_count",
            "last_knowledge_tool_ms",
            "session_connection_ms",
            "call_open_to_greeting_ms",
            "session_start_to_greeting_ms",
            "worker_job_entry_to_session_ready_ms",
            "participant_active_to_session_ready_ms",
            "worker_job_entry_to_first_server_speaking_ms",
            "participant_active_to_first_server_speaking_ms",
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
            "turn_latency_sample_count",
            "stt_session_update_serialized_prompt_chars",
            "stt_session_update_serialized_lexicon_count",
            "stt_session_update_serialized_sequence",
            "greeting_preparation_overlap_ms",
            "greeting_synthesis_first_frame_ms",
            "greeting_preparation_lead_ms",
            "greeting_synthesis_total_ms",
            "greeting_shared_cache_lookup_ms",
            "greeting_shared_cache_store_ms",
            "speech_lexicon_selected_entry_count",
            "speech_lexicon_tier_one_coverage_pct",
            "speech_lexicon_weighted_coverage_pct",
            "unexpected_script_count",
            "entity_resolution_count",
            "entity_resolution_search_applied_count",
            "last_exact_fact_preclassification_ms",
            "last_exact_fact_binding_lookup_ms",
            "last_exact_fact_revision_lookup_ms",
            "last_exact_fact_cache_lookup_ms",
            "last_exact_fact_source_load_ms",
            "last_exact_fact_index_build_ms",
            "last_exact_fact_resolution_ms",
            "last_exact_fact_total_ms",
            "last_exact_fact_evidence_count",
            "last_exact_fact_candidate_count",
            "single_pass_turn_count",
            "single_pass_cancelled_count",
            "single_pass_stale_count",
            "single_pass_failed_count",
            "single_pass_error_count",
            "last_single_pass_transcript_chars",
            "last_single_pass_evidence_chars",
            "last_single_pass_retrieval_ms",
            "last_single_pass_generation_dispatch_ms",
            "last_single_pass_generation_ms",
            "last_single_pass_total_ms",
            "external_tts_request_count",
            "external_tts_characters",
            "greeting_provider_tts_request_count",
        }
        for field in string_fields:
            if isinstance(runtime.get(field), str):
                safe_runtime[field] = runtime[field]
        for field in numeric_fields:
            if isinstance(runtime.get(field), (int, float)) and not isinstance(
                runtime.get(field), bool
            ):
                safe_runtime[field] = runtime[field]
        for field in (
            "runtime_usage_components_complete",
            "stt_session_update_serialized_complete",
            "stt_session_update_provider_acknowledgement_observed",
            "stt_provider_language_reported",
            "greeting_preparation_fallback",
            "greeting_first_frame_ready_before_session",
            "greeting_tts_charge_expected",
            "greeting_tts_warmup_attempted",
            "external_tts_provider_reconciliation_required",
            "inworld_single_pass_requested",
            "recording_enabled",
            "recording_consent_observed",
            "recording_artifact_available",
        ):
            if isinstance(runtime.get(field), bool):
                safe_runtime[field] = runtime[field]
        components = runtime.get("usage_components_reported")
        if isinstance(components, list):
            safe_runtime["usage_components_reported"] = [
                component
                for component in components
                if component in {"external_tts", "llm", "tts", "stt"}
            ]
        expected_components = runtime.get("usage_components_expected")
        if isinstance(expected_components, list):
            safe_runtime["usage_components_expected"] = [
                component
                for component in expected_components
                if component in {"external_tts", "llm", "tts", "stt"}
            ]
        external_tts_sources = runtime.get("external_tts_sources")
        if isinstance(external_tts_sources, list):
            allowed_external_tts_sources = {
                "greeting_preparation",
            }
            safe_runtime["external_tts_sources"] = [
                source for source in external_tts_sources if source in allowed_external_tts_sources
            ][:10]
        # Null is materially different from zero for provider billing usage.
        # Preserve it so the UI cannot present unavailable metering as free use.
        for field in {
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
        }:
            if field in runtime and runtime[field] is None:
                safe_runtime[field] = None
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
                "exact_fact_preclassification_ms",
                "exact_fact_binding_lookup_ms",
                "exact_fact_revision_lookup_ms",
                "exact_fact_cache_lookup_ms",
                "exact_fact_source_load_ms",
                "exact_fact_index_build_ms",
                "exact_fact_resolution_ms",
                "exact_fact_total_ms",
                "exact_fact_evidence_count",
                "exact_fact_candidate_count",
                "entity_resolution_confidence",
                "entity_resolution_margin",
                "unexpected_script_ratio",
                "single_pass_sequence",
                "single_pass_transcript_chars",
                "single_pass_evidence_chars",
                "single_pass_retrieval_ms",
                "single_pass_generation_dispatch_ms",
                "single_pass_generation_ms",
                "single_pass_total_ms",
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
                for field in (
                    "tool_call",
                    "knowledge_fallback_used",
                    "exact_fact_cache_hit",
                    "entity_resolution_applied_to_search",
                    "unexpected_script",
                ):
                    if isinstance(trace.get(field), bool):
                        safe_trace[field] = trace[field]
                if trace.get("knowledge_result") in {"verified", "no_match", "error"}:
                    safe_trace["knowledge_result"] = trace["knowledge_result"]
                if trace.get("retrieval_result") in {"verified", "no_match", "error"}:
                    safe_trace["retrieval_result"] = trace["retrieval_result"]
                if trace.get("grounding_outcome") in {
                    "verified_answer",
                    "response_after_verified_retrieval",
                    "no_match_correctly_refused",
                    "no_match_clarification",
                    "no_match_unverified_response",
                    "knowledge_error_response",
                }:
                    safe_trace["grounding_outcome"] = trace["grounding_outcome"]
                if trace.get("response_action") in {
                    "answered_from_verified_evidence",
                    "responded_after_verified_retrieval",
                    "refused_despite_verified_evidence",
                    "asked_clarification_despite_verified_evidence",
                    "refused_unverified",
                    "asked_clarification",
                    "answered_without_verified_evidence",
                    "knowledge_error_response",
                    "asked_transcription_clarification",
                }:
                    safe_trace["response_action"] = trace["response_action"]
                for field in (
                    "knowledge_retrieval_path",
                    "knowledge_company_subject",
                    "exact_fact_action",
                    "exact_fact_reason",
                    "entity_resolution_entry_id",
                    "expected_stt_language",
                    "inworld_turn_mode",
                    "single_pass_outcome",
                    "single_pass_error_type",
                ):
                    if isinstance(trace.get(field), str):
                        safe_trace[field] = trace[field]
                for field in (
                    "exact_fact_intents",
                    "unexpected_scripts",
                ):
                    values = trace.get(field)
                    if isinstance(values, list):
                        safe_trace[field] = [
                            str(item)[:128] for item in values[:5] if str(item).strip()
                        ]
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
