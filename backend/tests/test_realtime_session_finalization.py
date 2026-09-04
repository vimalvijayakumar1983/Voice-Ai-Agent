import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.v1.endpoints import realtime as realtime_api
from app.models.call import Call
from app.realtime import session as realtime_session
from tests.conftest import test_session_factory as session_factory


def _twilio_start_message(*, stream_sid: str = "MZ-test", call_sid: str = "CA-test") -> str:
    return (
        '{"event":"start","start":{"streamSid":"' + stream_sid + '","callSid":"' + call_sid + '"}}'
    )


async def _persist_inbound_call(db, tenant, *, call_sid: str) -> Call:
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="inbound",
        status="in_progress",
        from_number="+15550100001",
        to_number="+15550100002",
        provider="twilio",
        provider_call_sid=call_sid,
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        call_metadata={
            "runtime": {
                "transport": "twilio_media_streams",
                "speech_provider": "sarvam",
            }
        },
    )
    db.add(call)
    await db.commit()
    return call


def _runtime_config(call: Call) -> realtime_session.RuntimeSessionConfig:
    return realtime_session.RuntimeSessionConfig(
        call_id=call.id,
        tenant_id=call.tenant_id,
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
        knowledge_serving_revision_id=uuid4(),
        knowledge_serving_knowledge_base_id=uuid4(),
        knowledge_serving_revocation_generation=0,
        knowledge_terminology=("Example Medical Centre",),
    )


async def _assert_failed_runtime_call(db, call: Call) -> None:
    await db.refresh(call)
    assert call.status == "failed"
    assert call.ended_at is not None
    assert call.duration_seconds >= 1
    assert call.call_metadata["lifecycle_error"] == "runtime_provider_session_failed"
    runtime = call.call_metadata["runtime"]
    assert runtime["runtime_start_failure"] == "runtime_provider_session_failed"
    assert runtime["media_stream_started"] is True
    assert runtime["cost_state"] == "pending_provider_billing_sync"
    assert runtime["duration_source"] == "minimum_answered_runtime_start_failure"


@pytest.mark.asyncio
async def test_provider_context_entry_failure_terminalizes_inbound_as_failed(
    db,
    tenant,
    monkeypatch,
):
    call = await _persist_inbound_call(
        db,
        tenant,
        call_sid="CA-provider-context-entry-failure",
    )

    class FailingStream:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("provider handshake failed")

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    monkeypatch.setattr(realtime_session, "SarvamSTTStream", FailingStream)
    monkeypatch.setattr(realtime_session, "SarvamTTSStream", FailingStream)
    monkeypatch.setattr(realtime_session, "ConversationEngine", lambda **_kwargs: object())
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    config = _runtime_config(call)

    with pytest.raises(RuntimeError, match="provider handshake failed"):
        await realtime_session.run_twilio_media_session(AsyncMock(), config)

    await _assert_failed_runtime_call(db, call)


@pytest.mark.asyncio
async def test_provider_event_failure_exits_promptly_and_terminalizes_failed(
    db,
    tenant,
    monkeypatch,
):
    call = await _persist_inbound_call(
        db,
        tenant,
        call_sid="CA-provider-event-failure",
    )
    never = asyncio.Event()

    class FailingEventsSTT:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def events(self):
            if False:
                yield {}
            raise RuntimeError("provider event stream failed")

        async def send_audio(self, _audio):
            return None

    class IdleTTS:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def audio_for(self, _fragments, *, language_code):
            del language_code
            if False:
                yield ""

    async def receive_forever():
        await never.wait()
        return ""

    websocket = AsyncMock()
    websocket.receive_text.side_effect = receive_forever
    monkeypatch.setattr(realtime_session, "SarvamSTTStream", FailingEventsSTT)
    monkeypatch.setattr(realtime_session, "SarvamTTSStream", IdleTTS)
    monkeypatch.setattr(realtime_session, "ConversationEngine", lambda **_kwargs: object())
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    with pytest.raises(RuntimeError, match="provider event stream failed"):
        await asyncio.wait_for(
            realtime_session.run_twilio_media_session(websocket, _runtime_config(call)),
            timeout=1,
        )

    await _assert_failed_runtime_call(db, call)


@pytest.mark.asyncio
async def test_greeting_transport_failure_exits_promptly_and_terminalizes_failed(
    db,
    tenant,
    monkeypatch,
):
    call = await _persist_inbound_call(
        db,
        tenant,
        call_sid="CA-greeting-transport-failure",
    )
    never = asyncio.Event()

    class BlockingSTT:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def events(self):
            await never.wait()
            if False:
                yield {}

        async def send_audio(self, _audio):
            return None

    class AudioTTS:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def audio_for(self, fragments, *, language_code):
            del language_code
            async for _fragment in fragments:
                yield "Zm9v"

    async def receive_forever():
        await never.wait()
        return ""

    websocket = AsyncMock()
    websocket.receive_text.side_effect = receive_forever
    websocket.send_text.side_effect = RuntimeError("media transport send failed")
    monkeypatch.setattr(realtime_session, "SarvamSTTStream", BlockingSTT)
    monkeypatch.setattr(realtime_session, "SarvamTTSStream", AudioTTS)
    monkeypatch.setattr(realtime_session, "ConversationEngine", lambda **_kwargs: object())
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    with pytest.raises(RuntimeError, match="media transport send failed"):
        await asyncio.wait_for(
            realtime_session.run_twilio_media_session(
                websocket,
                _runtime_config(call),
                initial_messages=['{"event":"start","start":{"streamSid":"MZ-test"}}'],
            ),
            timeout=1,
        )

    await _assert_failed_runtime_call(db, call)


@pytest.mark.asyncio
async def test_twilio_stop_remains_a_successful_terminal_path(db, tenant, monkeypatch):
    call = await _persist_inbound_call(
        db,
        tenant,
        call_sid="CA-normal-stop",
    )
    never = asyncio.Event()

    class BlockingSTT:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def events(self):
            await never.wait()
            if False:
                yield {}

        async def send_audio(self, _audio):
            return None

    class IdleTTS:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def audio_for(self, _fragments, *, language_code):
            del language_code
            if False:
                yield ""

    monkeypatch.setattr(realtime_session, "SarvamSTTStream", BlockingSTT)
    monkeypatch.setattr(realtime_session, "SarvamTTSStream", IdleTTS)
    monkeypatch.setattr(realtime_session, "ConversationEngine", lambda **_kwargs: object())
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    await realtime_session.run_twilio_media_session(
        AsyncMock(),
        _runtime_config(call),
        initial_messages=['{"event":"stop"}'],
    )

    await db.refresh(call)
    assert call.status == "completed"


@pytest.mark.asyncio
async def test_malformed_twilio_media_event_terminalizes_failed(db, tenant, monkeypatch):
    call = await _persist_inbound_call(
        db,
        tenant,
        call_sid="CA-malformed-media-event",
    )
    never = asyncio.Event()

    class IdleSTT:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def events(self):
            await never.wait()
            if False:
                yield {}

    class IdleTTS:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    monkeypatch.setattr(realtime_session, "SarvamSTTStream", IdleSTT)
    monkeypatch.setattr(realtime_session, "SarvamTTSStream", IdleTTS)
    monkeypatch.setattr(realtime_session, "ConversationEngine", lambda **_kwargs: object())
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    with pytest.raises(json.JSONDecodeError):
        await asyncio.wait_for(
            realtime_session.run_twilio_media_session(
                AsyncMock(),
                _runtime_config(call),
                initial_messages=["{not-json"],
            ),
            timeout=1,
        )

    await _assert_failed_runtime_call(db, call)


@pytest.mark.asyncio
async def test_authenticated_stream_terminalizes_unavailable_runtime(monkeypatch):
    call_id = uuid4()
    claim_id = uuid4()
    websocket = AsyncMock()
    monkeypatch.setattr(
        realtime_api,
        "_authenticate_twilio_stream",
        AsyncMock(return_value=[_twilio_start_message()]),
    )
    claim = AsyncMock(return_value=claim_id)
    monkeypatch.setattr(realtime_api, "claim_runtime_media_session", claim)
    monkeypatch.setattr(realtime_api, "load_runtime_session", AsyncMock(return_value=None))
    fail_start = AsyncMock(return_value=True)
    monkeypatch.setattr(realtime_api, "fail_inbound_runtime_start", fail_start)

    await realtime_api.twilio_media_socket(websocket, call_id)

    fail_start.assert_awaited_once_with(
        call_id,
        reason="runtime_configuration_unavailable",
        media_session_claim_id=claim_id,
    )
    claim.assert_awaited_once_with(
        call_id,
        stream_sid="MZ-test",
        provider_call_sid="CA-test",
    )
    websocket.close.assert_awaited_once_with(code=4403, reason="Runtime is not active")


@pytest.mark.asyncio
async def test_authenticated_stream_terminalizes_runtime_load_exception(monkeypatch):
    call_id = uuid4()
    claim_id = uuid4()
    websocket = AsyncMock()
    monkeypatch.setattr(
        realtime_api,
        "_authenticate_twilio_stream",
        AsyncMock(return_value=[_twilio_start_message()]),
    )
    monkeypatch.setattr(
        realtime_api,
        "claim_runtime_media_session",
        AsyncMock(return_value=claim_id),
    )
    monkeypatch.setattr(
        realtime_api,
        "load_runtime_session",
        AsyncMock(side_effect=RuntimeError("credential decrypt failed")),
    )
    fail_start = AsyncMock(return_value=True)
    monkeypatch.setattr(realtime_api, "fail_inbound_runtime_start", fail_start)

    await realtime_api.twilio_media_socket(websocket, call_id)

    fail_start.assert_awaited_once_with(
        call_id,
        reason="runtime_configuration_load_failed",
        media_session_claim_id=claim_id,
    )
    websocket.close.assert_awaited_once_with(code=1011, reason="Runtime could not start")


@pytest.mark.asyncio
async def test_unauthenticated_stream_cannot_terminalize_call(monkeypatch):
    call_id = uuid4()
    websocket = AsyncMock()
    monkeypatch.setattr(
        realtime_api,
        "_authenticate_twilio_stream",
        AsyncMock(return_value=None),
    )
    fail_start = AsyncMock(return_value=True)
    claim = AsyncMock()
    monkeypatch.setattr(realtime_api, "claim_runtime_media_session", claim)
    monkeypatch.setattr(realtime_api, "fail_inbound_runtime_start", fail_start)

    await realtime_api.twilio_media_socket(websocket, call_id)

    fail_start.assert_not_awaited()
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_authenticated_stream_does_not_terminalize_owner(monkeypatch):
    call_id = uuid4()
    websocket = AsyncMock()
    monkeypatch.setattr(
        realtime_api,
        "_authenticate_twilio_stream",
        AsyncMock(return_value=[_twilio_start_message()]),
    )
    monkeypatch.setattr(
        realtime_api,
        "claim_runtime_media_session",
        AsyncMock(
            side_effect=realtime_session.RuntimeMediaSessionAlreadyClaimedError("already claimed")
        ),
    )
    fail_start = AsyncMock(return_value=True)
    run_session = AsyncMock()
    monkeypatch.setattr(realtime_api, "fail_inbound_runtime_start", fail_start)
    monkeypatch.setattr(realtime_api, "run_twilio_media_session", run_session)

    await realtime_api.twilio_media_socket(websocket, call_id)

    fail_start.assert_not_awaited()
    run_session.assert_not_awaited()
    websocket.close.assert_awaited_once_with(
        code=4409,
        reason="Media session already active",
    )
