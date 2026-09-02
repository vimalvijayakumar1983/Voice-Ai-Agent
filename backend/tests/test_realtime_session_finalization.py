from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.realtime import session as realtime_session


@pytest.mark.asyncio
async def test_provider_context_entry_failure_still_finalizes_inbound_call(monkeypatch):
    class FailingStream:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("provider handshake failed")

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    store_metrics = AsyncMock()
    finalize_call = AsyncMock()
    monkeypatch.setattr(realtime_session, "SarvamSTTStream", FailingStream)
    monkeypatch.setattr(realtime_session, "SarvamTTSStream", FailingStream)
    monkeypatch.setattr(realtime_session, "_store_metrics", store_metrics)
    monkeypatch.setattr(realtime_session, "_finalize_inbound_call", finalize_call)
    monkeypatch.setattr(realtime_session, "ConversationEngine", lambda **_kwargs: object())

    config = realtime_session.RuntimeSessionConfig(
        call_id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        system_prompt="Use approved knowledge.",
        greeting_message="Hello.",
        fallback_message="Please try again.",
        speaker="ishita",
        language_code="en-IN",
        stt_language="auto",
        speech_rate=1.0,
        temperature=0.2,
        max_tokens=100,
        llm_model="gpt-4o-mini",
        max_duration_seconds=60,
        speech_provider="sarvam",
        sarvam_api_key="sarvam-key",
        tts_api_key="sarvam-key",
        openai_api_key="openai-key",
    )

    with pytest.raises(RuntimeError, match="provider handshake failed"):
        await realtime_session.run_twilio_media_session(AsyncMock(), config)

    store_metrics.assert_awaited_once()
    finalize_call.assert_awaited_once_with(config)
