"""Public call metadata must be useful without leaking dispatch secrets."""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.call import CallResponse


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
