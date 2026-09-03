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
                "stt_model": "assemblyai/u3-rt-pro",
                "stt_language": "en-GB",
                "voice_runtime": "inworld_realtime",
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
                        "grounding_outcome": "verified_answer",
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
            "stt_model": "assemblyai/u3-rt-pro",
            "stt_language": "en-GB",
            "voice_runtime": "inworld_realtime",
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
                    "grounding_outcome": "verified_answer",
                }
            ],
        },
    }
