"""Public call metadata must be useful without leaking dispatch secrets."""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.call import CallResponse
from app.services.call_metadata import public_call_metadata


def test_call_response_projects_only_safe_agent_configuration_metadata():
    response = CallResponse.model_validate(
        {
            "id": uuid4(),
            "tenant_id": uuid4(),
            "agent_id": uuid4(),
            "campaign_id": None,
            "direction": "outbound",
            "status": "completed",
            "from_number": "+971500000001",
            "to_number": "+971500000002",
            "provider": "smallest",
            "provider_call_sid": "provider-call-id",
            "provider_recording_url": "https://provider.example/private-recording.mp3?secret=1",
            "call_metadata": {
                "conversation_type": "webcall",
                "channel": "browser",
                "request": {"context": {"account_secret": "do-not-expose"}},
                "smallest_variables": {"private": "do-not-expose"},
                "smallest_analytics": {"internal": "do-not-expose"},
                "agent_configuration": {
                    "provider_revision_id": "revision-42",
                    "language": "en",
                    "supported_languages": ["en", "hi", "ta"],
                    "language_switching_enabled": True,
                    "language_switching_mode": "automatic",
                    "unexpected": "do-not-expose",
                },
            },
            "started_at": None,
            "answered_at": None,
            "ended_at": None,
            "duration_seconds": 60,
            "cost_cents": 4,
            "disposition": "appointment_booked",
            "sentiment_score": 0.8,
            "created_at": datetime.now(UTC),
        }
    )

    assert response.call_metadata == {
        "conversation_type": "webcall",
        "channel": "browser",
        "provider_revision_id": "revision-42",
        "language": "en",
        "supported_languages": ["en", "hi", "ta"],
        "language_switching_enabled": True,
        "language_switching_mode": "automatic",
    }
    assert response.recording_available is True
    assert "provider_recording_url" not in response.model_dump()
    assert "private-recording" not in str(response.model_dump())


def test_public_call_metadata_exposes_realtime_quality_and_usage_metrics():
    projected = public_call_metadata(
        {
            "agent_configuration": {"language": "en-GB"},
            "runtime": {
                "call_open_to_greeting_ms": 820,
                "session_start_to_greeting_ms": 420,
                "worker_job_entry_to_session_ready_ms": 610,
                "participant_active_to_session_ready_ms": 510,
                "worker_job_entry_to_first_server_speaking_ms": 920,
                "participant_active_to_first_server_speaking_ms": 820,
                "last_end_of_utterance_ms": 210,
                "last_transcription_delay_ms": 125,
                "last_knowledge_hook_ms": 75,
                "last_interruption_detection_ms": 160,
                "llm_tokens": 1500,
                "llm_input_audio_tokens": 700,
                "llm_output_audio_tokens": 120,
                "realtime_session_seconds": 42.5,
                "stale_generation_cancel_count": 3,
                "suppressed_fragment_count": 2,
                "last_suppressed_fragment_words": 1,
                "fragment_continuation_window_ms": 500,
                "knowledge_terminology_load_ms": 95,
                "knowledge_terminology_count": 80,
                "knowledge_terminology_total_count": 240,
                "knowledge_serving_revocation_generation": 3,
                "stt_model": "assemblyai/u3-rt-pro",
                "stt_language": "en-GB",
                "voice_runtime": "inworld_realtime",
                "knowledge_serving_knowledge_base_id": ("00000000-0000-0000-0000-000000000123"),
                "knowledge_admission_state": "admitted_before_dispatch",
                "knowledge_admitted_at": "2026-09-04T10:00:00+00:00",
                "knowledge_lookup_count": 1,
                "knowledge_match_count": 1,
                "last_knowledge_tool_ms": 43,
                "session_connection_ms": 310,
                "turn_diagnostics": [
                    {
                        "turn": 1,
                        "barge_in": True,
                        "transcript_words": 1,
                        "stabilization_ms": 500,
                        "outcome": "fragment_suppressed",
                        "tool_call": True,
                        "knowledge_result": "verified",
                        "knowledge_tool_ms": 43,
                        "knowledge_evidence_chars": 640,
                        "knowledge_query_variant_count": 2,
                        "knowledge_fallback_used": False,
                        "grounding_outcome": "response_after_verified_retrieval",
                        "private_transcript": "do-not-expose",
                    }
                ],
                "private_provider_request": "do-not-expose",
            },
        }
    )

    assert projected == {
        "language": "en-GB",
        "runtime": {
            "call_open_to_greeting_ms": 820,
            "session_start_to_greeting_ms": 420,
            "worker_job_entry_to_session_ready_ms": 610,
            "participant_active_to_session_ready_ms": 510,
            "worker_job_entry_to_first_server_speaking_ms": 920,
            "participant_active_to_first_server_speaking_ms": 820,
            "last_end_of_utterance_ms": 210,
            "last_transcription_delay_ms": 125,
            "last_knowledge_hook_ms": 75,
            "last_interruption_detection_ms": 160,
            "llm_tokens": 1500,
            "llm_input_audio_tokens": 700,
            "llm_output_audio_tokens": 120,
            "realtime_session_seconds": 42.5,
            "stale_generation_cancel_count": 3,
            "suppressed_fragment_count": 2,
            "last_suppressed_fragment_words": 1,
            "fragment_continuation_window_ms": 500,
            "knowledge_terminology_load_ms": 95,
            "knowledge_terminology_count": 80,
            "knowledge_terminology_total_count": 240,
            "knowledge_serving_revocation_generation": 3,
            "stt_model": "assemblyai/u3-rt-pro",
            "stt_language": "en-GB",
            "voice_runtime": "inworld_realtime",
            "knowledge_serving_knowledge_base_id": "00000000-0000-0000-0000-000000000123",
            "knowledge_admission_state": "admitted_before_dispatch",
            "knowledge_admitted_at": "2026-09-04T10:00:00+00:00",
            "knowledge_lookup_count": 1,
            "knowledge_match_count": 1,
            "last_knowledge_tool_ms": 43,
            "session_connection_ms": 310,
            "turn_diagnostics": [
                {
                    "turn": 1,
                    "barge_in": True,
                    "transcript_words": 1,
                    "stabilization_ms": 500,
                    "outcome": "fragment_suppressed",
                    "tool_call": True,
                    "knowledge_result": "verified",
                    "knowledge_tool_ms": 43,
                    "knowledge_evidence_chars": 640,
                    "knowledge_query_variant_count": 2,
                    "knowledge_fallback_used": False,
                    "grounding_outcome": "response_after_verified_retrieval",
                }
            ],
        },
    }


def test_public_call_metadata_preserves_serialization_diagnostics_and_unknown_usage():
    projected = public_call_metadata(
        {
            "agent_configuration": {"language": "en-GB"},
            "runtime": {
                "usage_source": "livekit_session_usage",
                "runtime_usage_components_complete": False,
                "usage_components_expected": ["llm"],
                "usage_components_reported": [],
                "llm_tokens": None,
                "llm_input_tokens": None,
                "realtime_session_seconds": None,
                "stt_session_update_serialized_model": "assemblyai/u3-rt-pro",
                "stt_session_update_serialized_language": "en-GB",
                "stt_session_update_serialized_prompt_chars": 147,
                "stt_session_update_serialized_lexicon_count": 12,
                "stt_session_update_serialized_sequence": 1,
                "stt_session_update_serialized_complete": True,
                "stt_session_update_provider_acknowledgement_observed": False,
                "stt_session_update_serialized_prompt_sha256": "do-not-expose",
                "audio_latency_observation_point": "livekit_server_response_start",
                "audio_latency_unobserved_segments": (
                    "downstream_network_browser_render_and_sip_rtp_arrival"
                ),
                "turn_latency_sample_count": 3,
                "stt_provider_reported_language": None,
                "stt_provider_language_reported": False,
                "turn_diagnostics": [
                    {
                        "turn": 1,
                        "retrieval_result": "verified",
                        "response_action": "responded_after_verified_retrieval",
                        "private_prompt": "never expose",
                    }
                ],
            },
        }
    )

    assert projected == {
        "language": "en-GB",
        "runtime": {
            "usage_source": "livekit_session_usage",
            "stt_session_update_serialized_model": "assemblyai/u3-rt-pro",
            "stt_session_update_serialized_language": "en-GB",
            "stt_session_update_serialized_prompt_chars": 147,
            "stt_session_update_serialized_lexicon_count": 12,
            "stt_session_update_serialized_sequence": 1,
            "audio_latency_observation_point": "livekit_server_response_start",
            "audio_latency_unobserved_segments": (
                "downstream_network_browser_render_and_sip_rtp_arrival"
            ),
            "turn_latency_sample_count": 3,
            "runtime_usage_components_complete": False,
            "stt_session_update_serialized_complete": True,
            "stt_session_update_provider_acknowledgement_observed": False,
            "stt_provider_language_reported": False,
            "usage_components_expected": ["llm"],
            "usage_components_reported": [],
            "llm_tokens": None,
            "llm_input_tokens": None,
            "realtime_session_seconds": None,
            "turn_diagnostics": [
                {
                    "turn": 1,
                    "retrieval_result": "verified",
                    "response_action": "responded_after_verified_retrieval",
                }
            ],
        },
    }


def test_public_call_metadata_keeps_external_tts_reconciliation_truthful():
    projected = public_call_metadata(
        {
            "agent_configuration": {"language": "en-GB"},
            "runtime": {
                "usage_source": "livekit_session_usage",
                "external_tts_usage_source": "vav_provider_request_units",
                "runtime_usage_components_complete": True,
                "usage_components_expected": ["external_tts", "llm"],
                "usage_components_reported": ["external_tts", "llm"],
                "external_tts_request_count": 2,
                "external_tts_characters": 84,
                "external_tts_provider_reconciliation_required": True,
                "greeting_provider_tts_request_count": 1,
                "greeting_tts_charge_expected": True,
                "external_tts_sources": [
                    "greeting_preparation",
                    "attacker-controlled-value",
                ],
            },
        }
    )

    assert projected == {
        "language": "en-GB",
        "runtime": {
            "usage_source": "livekit_session_usage",
            "external_tts_usage_source": "vav_provider_request_units",
            "runtime_usage_components_complete": True,
            "usage_components_expected": ["external_tts", "llm"],
            "usage_components_reported": ["external_tts", "llm"],
            "external_tts_request_count": 2,
            "external_tts_characters": 84,
            "external_tts_provider_reconciliation_required": True,
            "greeting_provider_tts_request_count": 1,
            "greeting_tts_charge_expected": True,
            "external_tts_sources": [
                "greeting_preparation",
            ],
        },
    }


def test_public_call_metadata_projects_versioned_knowledge_and_repair_diagnostics():
    projected = public_call_metadata(
        {
            "agent_configuration": {"language": "en-GB"},
            "runtime": {
                "greeting_cache_status": "hit",
                "greeting_preparation_overlap_ms": 82,
                "speech_lexicon_source": "versioned_artifact",
                "speech_lexicon_artifact_id": "artifact-42",
                "speech_lexicon_content_sha256": "a" * 64,
                "speech_lexicon_compiler_version": "vav-speech-lexicon-1",
                "speech_lexicon_tier_one_coverage_pct": 100.0,
                "knowledge_turn_mode": "single_pass_experimental",
                "inworld_single_pass_requested": True,
                "single_pass_turn_count": 1,
                "last_single_pass_outcome": "completed",
                "last_single_pass_total_ms": 252.5,
                "unexpected_script_count": 1,
                "entity_resolution_search_applied_count": 1,
                "last_exact_fact_total_ms": 4.25,
                "recording_requested_mode": "livekit_egress_explicit_consent",
                "recording_effective_mode": "off",
                "recording_state": "blocked",
                "recording_blocker": ("call_bound_consent_egress_storage_retention_not_configured"),
                "recording_enabled": False,
                "recording_consent_observed": False,
                "recording_artifact_available": False,
                "turn_diagnostics": [
                    {
                        "turn": 2,
                        "knowledge_retrieval_path": "exact_fact",
                        "exact_fact_action": "answer",
                        "exact_fact_reason": "verified_exact_fact",
                        "exact_fact_total_ms": 4.25,
                        "exact_fact_evidence_count": 1,
                        "exact_fact_cache_hit": True,
                        "exact_fact_intents": ["leadership"],
                        "exact_fact_evidence_ids": ["ef1:source:evidence"],
                        "entity_resolution_entry_id": "entry-1",
                        "entity_resolution_confidence": 0.96,
                        "entity_resolution_margin": 0.21,
                        "entity_resolution_applied_to_search": True,
                        "unexpected_script": True,
                        "expected_stt_language": "en-GB",
                        "unexpected_scripts": ["DEVANAGARI"],
                        "unexpected_script_ratio": 1.0,
                        "response_action": "asked_transcription_clarification",
                        "inworld_turn_mode": "single_pass_experimental",
                        "single_pass_sequence": 7,
                        "single_pass_outcome": "completed",
                        "single_pass_retrieval_ms": 12.5,
                        "single_pass_total_ms": 252.5,
                        "private_evidence": "do-not-expose",
                    }
                ],
            },
        }
    )

    runtime = projected["runtime"]
    assert runtime["greeting_cache_status"] == "hit"
    assert runtime["speech_lexicon_artifact_id"] == "artifact-42"
    assert runtime["speech_lexicon_tier_one_coverage_pct"] == 100.0
    assert runtime["knowledge_turn_mode"] == "single_pass_experimental"
    assert runtime["last_single_pass_total_ms"] == 252.5
    assert runtime["recording_requested_mode"] == "livekit_egress_explicit_consent"
    assert runtime["recording_effective_mode"] == "off"
    assert runtime["recording_state"] == "blocked"
    assert runtime["recording_enabled"] is False
    trace = runtime["turn_diagnostics"][0]
    assert trace["knowledge_retrieval_path"] == "exact_fact"
    assert trace["exact_fact_evidence_count"] == 1
    assert "exact_fact_evidence_ids" not in trace
    assert trace["unexpected_scripts"] == ["DEVANAGARI"]
    assert trace["response_action"] == "asked_transcription_clarification"
    assert trace["single_pass_outcome"] == "completed"
    assert trace["single_pass_retrieval_ms"] == 12.5
    assert "private_evidence" not in trace
