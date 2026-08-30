"""VAV realtime runtime control-plane and protocol tests."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.models.agent import Agent
from app.realtime.auth import create_media_token, verify_media_token
from app.realtime.sarvam_stream import is_speech_start, parse_transcript_event
from app.services.knowledge_retrieval import rank_knowledge


def test_media_token_is_call_scoped_and_expires():
    from uuid import uuid4

    call_id = uuid4()
    token = create_media_token(call_id, now=1000)

    assert verify_media_token(token, call_id, now=1001)
    assert not verify_media_token(token, uuid4(), now=1001)
    assert not verify_media_token(token, call_id, now=2000)
    assert not verify_media_token(token + "tampered", call_id, now=1001)


def test_sarvam_events_support_documented_and_nested_transcripts():
    final = parse_transcript_event(
        {
            "event": "transcript.final",
            "data": {"transcript": "I need a dermatologist", "language_code": "en-IN"},
        }
    )
    partial = parse_transcript_event(
        {"type": "transcript.partial", "data": {"transcript": {"text": "I need"}}}
    )

    assert final and final.is_final and final.language_code == "en-IN"
    assert partial and not partial.is_final and partial.text == "I need"
    assert is_speech_start({"event": "vad.speech_start"})


def test_provider_neutral_knowledge_ranking_prefers_query_coverage():
    matches = rank_knowledge(
        "doctor hair specialist",
        [
            ("PRP", "PRP may support hair rejuvenation."),
            ("Doctors", "Dr Rao is a doctor and hair specialist at the clinic."),
        ],
    )

    assert matches[0].source == "Doctors"
    assert "Dr Rao" in matches[0].text


@pytest.mark.asyncio
async def test_runtime_profile_requires_readiness_before_activation(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Royal concierge",
        system_prompt="Answer patients using approved clinic knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en", "hi"],
        language_switching_enabled=True,
        language_switching_mode="automatic",
    )
    db.add(agent)
    await db.commit()

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+971501234567"],
            "telephony_provider": "twilio",
            "primary_speech_provider": "sarvam",
            "fallback_speech_provider": None,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "stt_language": "auto",
            "max_concurrent_calls": 2,
            "daily_call_limit": 50,
            "monthly_budget_cents": 10000,
        },
    )
    activated = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate",
        headers=auth_headers,
    )

    assert configured.status_code == 200
    assert not configured.json()["ready"]
    assert activated.status_code == 409
    assert "blockers" in activated.json()["detail"]


@pytest.mark.asyncio
async def test_ready_runtime_can_be_activated(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://api.example.com")
    agent = Agent(
        tenant_id=tenant.id,
        name="Active concierge",
        system_prompt="Answer patients using approved clinic knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
        language_switching_enabled=False,
        language_switching_mode="disabled",
        last_synced_at=datetime.now(UTC),
    )
    db.add(agent)
    await db.commit()
    payload = {
        "assigned_numbers": ["+971501234567"],
        "telephony_provider": "twilio",
        "primary_speech_provider": "sarvam",
        "fallback_speech_provider": None,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "stt_language": "auto",
        "max_concurrent_calls": 2,
        "daily_call_limit": 50,
        "monthly_budget_cents": 10000,
    }

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers, json=payload
    )
    tested = await client.post(f"/api/v1/runtime/agents/{agent.id}/test", headers=auth_headers)
    activated = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate", headers=auth_headers
    )
    retested = await client.post(f"/api/v1/runtime/agents/{agent.id}/test", headers=auth_headers)
    active_profile = await client.get(f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers)

    assert configured.json()["ready"] is True
    assert tested.json()["ready"] is True
    assert activated.status_code == 200
    assert activated.json()["enabled"] is True
    assert activated.json()["status"] == "active"
    assert retested.status_code == 200
    assert retested.json()["ready"] is True
    assert active_profile.json()["enabled"] is True
    assert active_profile.json()["status"] == "active"
