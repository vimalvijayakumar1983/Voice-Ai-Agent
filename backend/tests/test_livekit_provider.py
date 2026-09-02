"""LiveKit SIP transport tests."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from livekit import api
from sqlalchemy import select

from app.livekit_runtime import worker as livekit_worker
from app.livekit_runtime.worker import (
    _capture_turn_latency,
    _inworld_stt_model,
    _inworld_tts_options,
    _usage_snapshot,
    _worker_http_port,
    _worker_idle_processes,
)
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.services.provider_credentials import ProviderCredentialError
from app.telephony.livekit_provider import LiveKitSIPError, LiveKitSIPProvider
from tests.conftest import test_session_factory as session_factory


@pytest.mark.asyncio
async def test_open_outbound_call_merges_durable_context_into_session_variables(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Context-aware outbound agent",
        system_prompt="Welcome {{ customer_name }}.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
        greeting_message="Hello {{ customer_name }}",
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en-GB",
        assigned_numbers=["+97141234567"],
        status="active",
    )
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number="+97141234567",
        to_number="+971501234567",
        provider="livekit_sip",
        call_metadata={
            "request": {
                "context": {
                    "customer_name": "Maya",
                    "balance": 125.5,
                }
            }
        },
    )
    db.add_all([profile, call])
    await db.commit()
    variables = {}
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)

    opened_call_id = await livekit_worker._open_call(
        model=agent,
        profile=profile,
        room_name="vav-outbound-context-test",
        attributes={
            "sip.callDirection": "outbound",
            "sip.callStatus": "active",
            "sip.phoneNumber": "+971501234567",
            "sip.trunkPhoneNumber": "+97141234567",
        },
        dispatched_call_id=call.id,
        variables=variables,
    )

    assert opened_call_id == call.id
    assert variables == {"customer_name": "Maya", "balance": 125.5}


@pytest.mark.asyncio
async def test_outbound_livekit_call_dials_with_provider_cap_then_dispatches_worker(monkeypatch):
    events = []

    class Dispatch:
        async def create_dispatch(self, request):
            events.append(("dispatch", request))
            return SimpleNamespace(id="AD_dispatch")

    class SIP:
        async def create_sip_participant(self, request):
            events.append(("sip", request))
            return SimpleNamespace(sip_call_id="sip-call-1", participant_id="participant-1")

    class FakeLiveKit:
        def __init__(self, **_kwargs):
            self.agent_dispatch = Dispatch()
            self.sip = SIP()

        async def aclose(self):
            events.append(("close", None))

    monkeypatch.setattr("app.telephony.livekit_provider.api.LiveKitAPI", FakeLiveKit)
    call_id = uuid4()
    agent_id = uuid4()
    result = await LiveKitSIPProvider(
        url="wss://example.livekit.cloud",
        api_key="key",
        api_secret="secret",
    ).make_call(
        call_id=call_id,
        agent_id=agent_id,
        to_number="+971501234567",
        from_number="+97141234567",
        outbound_trunk_id="ST_outbound",
        agent_name="vav-inworld",
        max_call_duration_seconds=90,
    )

    assert [event[0] for event in events] == ["sip", "dispatch", "close"]
    assert events[1][1].agent_name == "vav-inworld"
    assert str(agent_id) in events[1][1].metadata
    assert str(call_id) in events[1][1].metadata
    assert events[0][1].sip_trunk_id == "ST_outbound"
    assert events[0][1].wait_until_answered is True
    assert events[0][1].max_call_duration.ToTimedelta() == timedelta(seconds=90)
    assert result.provider_call_sid == "sip-call-1"


@pytest.mark.asyncio
async def test_outbound_livekit_no_answer_is_terminal_and_cleans_dispatch(monkeypatch):
    events = []

    class Dispatch:
        async def create_dispatch(self, _request):
            return SimpleNamespace(id="AD_failed")

        async def delete_dispatch(self, dispatch_id, room_name):
            events.append(("delete_dispatch", dispatch_id, room_name))

    class SIP:
        async def create_sip_participant(self, request):
            events.append(("dial", request.wait_until_answered))
            raise api.SipCallError(
                "unavailable",
                "callee unavailable",
                status=409,
                metadata={"sip_status_code": "480", "sip_status": "Temporarily Unavailable"},
            )

    class Room:
        async def delete_room(self, request):
            events.append(("delete_room", request.room))

    class FakeLiveKit:
        def __init__(self, **_kwargs):
            self.agent_dispatch = Dispatch()
            self.sip = SIP()
            self.room = Room()

        async def aclose(self):
            events.append(("close",))

    monkeypatch.setattr("app.telephony.livekit_provider.api.LiveKitAPI", FakeLiveKit)
    provider = LiveKitSIPProvider(
        url="wss://example.livekit.cloud",
        api_key="key",
        api_secret="secret",
    )

    with pytest.raises(LiveKitSIPError) as caught:
        await provider.make_call(
            call_id=uuid4(),
            agent_id=uuid4(),
            to_number="+971501234567",
            from_number="+97141234567",
            outbound_trunk_id="ST_outbound",
            agent_name="vav-inworld",
            max_call_duration_seconds=60,
        )

    assert caught.value.ambiguous is False
    assert caught.value.terminal_status == "no_answer"
    assert events[0] == ("dial", True)
    assert events[1][0] == "delete_room"
    assert events[-1] == ("close",)


@pytest.mark.asyncio
async def test_outbound_livekit_dispatch_failure_hangs_up_answered_call(monkeypatch):
    events = []

    class Dispatch:
        async def create_dispatch(self, _request):
            events.append(("dispatch",))
            raise RuntimeError("dispatch unavailable")

    class SIP:
        async def create_sip_participant(self, request):
            events.append(("sip", request.max_call_duration.ToTimedelta()))
            return SimpleNamespace(sip_call_id="sip-call-answered", participant_id="sip-party")

    class Room:
        async def delete_room(self, request):
            events.append(("delete_room", request.room))

    class FakeLiveKit:
        def __init__(self, **_kwargs):
            self.agent_dispatch = Dispatch()
            self.sip = SIP()
            self.room = Room()

        async def aclose(self):
            events.append(("close",))

    monkeypatch.setattr("app.telephony.livekit_provider.api.LiveKitAPI", FakeLiveKit)

    with pytest.raises(LiveKitSIPError) as caught:
        await LiveKitSIPProvider(
            url="wss://example.livekit.cloud",
            api_key="key",
            api_secret="secret",
        ).make_call(
            call_id=uuid4(),
            agent_id=uuid4(),
            to_number="+971501234567",
            from_number="+97141234567",
            outbound_trunk_id="ST_outbound",
            agent_name="vav-inworld",
            max_call_duration_seconds=75,
        )

    assert caught.value.ambiguous is False
    assert caught.value.terminal_status == "failed"
    assert [event[0] for event in events] == ["sip", "dispatch", "delete_room", "close"]
    assert events[0][1] == timedelta(seconds=75)
    assert events[2][1].startswith("vav-call-")


@pytest.mark.asyncio
async def test_outbound_livekit_sip_failure_remains_definitive_when_cleanup_fails(monkeypatch):
    class SIP:
        async def create_sip_participant(self, _request):
            raise api.SipCallError(
                "busy",
                "callee busy",
                status=409,
                metadata={"sip_status_code": "486", "sip_status": "Busy Here"},
            )

    class Room:
        async def delete_room(self, _request):
            raise RuntimeError("room already absent")

    class FakeLiveKit:
        def __init__(self, **_kwargs):
            self.sip = SIP()
            self.room = Room()

        async def aclose(self):
            return None

    monkeypatch.setattr("app.telephony.livekit_provider.api.LiveKitAPI", FakeLiveKit)

    with pytest.raises(LiveKitSIPError) as caught:
        await LiveKitSIPProvider(
            url="wss://example.livekit.cloud",
            api_key="key",
            api_secret="secret",
        ).make_call(
            call_id=uuid4(),
            agent_id=uuid4(),
            to_number="+971501234567",
            from_number="+97141234567",
            outbound_trunk_id="ST_outbound",
            agent_name="vav-inworld",
            max_call_duration_seconds=60,
        )

    assert caught.value.ambiguous is False
    assert caught.value.terminal_status == "busy"


@pytest.mark.asyncio
async def test_worker_preopen_failure_terminalizes_exact_outbound_call_once(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Outbound Inworld agent",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    db.add(agent)
    await db.flush()
    agent_id = agent.id
    call_id = uuid4()
    room_name = f"vav-call-{call_id}"
    call = Call(
        id=call_id,
        tenant_id=tenant.id,
        agent_id=agent_id,
        direction="outbound",
        status="dispatching",
        from_number="+97141234567",
        to_number="+971501234567",
        provider="livekit_sip",
        call_metadata={
            "speech_provider": "inworld",
            "runtime": {
                "transport": "livekit_sip",
                "speech_provider": "inworld",
                "llm_provider": "openai",
            },
        },
    )
    db.add(call)
    await db.commit()

    class Room:
        name = room_name

    class Context:
        job = SimpleNamespace(
            metadata=json.dumps({"agent_id": str(agent_id), "call_id": str(call_id)})
        )
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity=f"sip-{call_id}",
                attributes={
                    "sip.callDirection": "outbound",
                    "sip.callStatus": "active",
                },
            )

    failure = RuntimeError("runtime key unavailable")
    delete_room = AsyncMock(return_value=True)
    finish_call = AsyncMock()
    open_call = AsyncMock()
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(livekit_worker, "_load_runtime", AsyncMock(side_effect=failure))
    monkeypatch.setattr(livekit_worker, "_open_call", open_call)
    monkeypatch.setattr(livekit_worker, "_finish_call", finish_call)
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)

    with pytest.raises(RuntimeError, match="runtime key unavailable"):
        await livekit_worker.vav_inworld_session(Context())

    open_call.assert_not_awaited()
    finish_call.assert_not_awaited()
    delete_room.assert_awaited_once_with(
        url=livekit_worker.settings.livekit_url,
        api_key=livekit_worker.settings.livekit_api_key,
        api_secret=livekit_worker.settings.livekit_api_secret,
        room_name=room_name,
    )
    db.expire_all()
    failed_call = await db.scalar(
        select(Call).where(Call.id == call_id).execution_options(populate_existing=True)
    )
    assert failed_call is not None
    assert failed_call.status == "failed"
    assert failed_call.started_at is not None
    assert failed_call.answered_at is not None
    assert failed_call.ended_at is not None
    assert failed_call.duration_seconds == 0
    assert failed_call.call_metadata["livekit_room"] == room_name
    assert failed_call.call_metadata["lifecycle_error"] == ("livekit_outbound_preopen_failure")
    assert failed_call.call_metadata["runtime_failure_type"] == "RuntimeError"
    assert "runtime key unavailable" not in json.dumps(failed_call.call_metadata)

    ended_at = failed_call.ended_at
    assert not await livekit_worker._fail_outbound_preopen_call(
        agent_id=agent_id,
        call_id=call_id,
        room_name=room_name,
        failure=RuntimeError("duplicate job"),
    )
    db.expire_all()
    unchanged_call = await db.scalar(
        select(Call).where(Call.id == call_id).execution_options(populate_existing=True)
    )
    assert unchanged_call is not None
    assert unchanged_call.ended_at == ended_at


@pytest.mark.asyncio
async def test_worker_logs_only_keyed_inbound_route_references(caplog, monkeypatch):
    trunk_id = "ST_sensitive_inbound_trunk"
    called_number = "+97141234567"
    room_name = "sensitive-livekit-room"

    class Room:
        name = room_name

    class Context:
        job = SimpleNamespace(metadata="{}")
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity="inbound-caller",
                attributes={
                    "sip.callDirection": "inbound",
                    "sip.callStatus": "active",
                    "sip.trunkID": trunk_id,
                    "sip.trunkPhoneNumber": called_number,
                },
            )

    monkeypatch.setattr(
        livekit_worker,
        "_resolve_inbound_runtime",
        AsyncMock(side_effect=RuntimeError("route unavailable")),
    )
    open_call = AsyncMock()
    monkeypatch.setattr(livekit_worker, "_open_call", open_call)

    with caplog.at_level("ERROR", logger=livekit_worker.__name__):
        with pytest.raises(RuntimeError, match="route unavailable"):
            await livekit_worker.vav_inworld_session(Context())

    open_call.assert_not_awaited()
    record = next(
        record for record in caplog.records if record.message == "livekit_inbound_preopen_failed"
    )
    assert record.failure_type == "RuntimeError"
    assert record.inbound_trunk_ref == livekit_worker._safe_log_identifier(
        "inbound_trunk",
        trunk_id,
    )
    assert record.called_number_ref == livekit_worker._safe_log_identifier(
        "called_number",
        called_number,
    )
    assert record.room_ref == livekit_worker._safe_log_identifier("room", room_name)
    assert trunk_id not in caplog.text
    assert called_number not in caplog.text
    assert room_name not in caplog.text


@pytest.mark.asyncio
async def test_worker_failure_after_call_persistence_finalizes_once_and_resolves_auto_stt(
    monkeypatch,
):
    agent_id = uuid4()
    call_id = uuid4()
    shutdown_callbacks = []
    stt_options = {}
    tts_options = {}
    llm_options = {}
    session_options = {}
    turn_detector_options = {}
    model = SimpleNamespace(
        id=agent_id,
        tenant_id=uuid4(),
        voice_id="inworld:Ashley",
        language="en-GB",
        language_switching_enabled=False,
        speech_rate=1.0,
        system_prompt="Answer with approved knowledge.",
        greeting_message="Hello",
        max_call_duration_seconds=60,
    )
    profile = SimpleNamespace(
        stt_language="auto",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )

    class Room:
        name = "vav-call-test"

        def on(self, _event, _callback):
            return None

    class Context:
        job = SimpleNamespace(
            metadata=json.dumps({"agent_id": str(agent_id), "call_id": str(call_id)})
        )
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity="sip-test",
                attributes={
                    "sip.callDirection": "outbound",
                    "sip.callStatus": "active",
                    "sip.phoneNumber": "+971501234567",
                    "sip.trunkPhoneNumber": "+97141234567",
                },
            )

        def add_shutdown_callback(self, callback):
            shutdown_callbacks.append(callback)

        def shutdown(self, reason=""):
            return None

    async def load_runtime(_agent_id):
        return (
            model,
            profile,
            livekit_worker._RuntimeApiKeys(
                speech="inworld-key",
                llm="tenant-openai-key",
            ),
        )

    def stt(**kwargs):
        stt_options.update(kwargs)
        return object()

    def tts(**kwargs):
        tts_options.update(kwargs)
        return object()

    def agent_session(**kwargs):
        session_options.update(kwargs)
        raise RuntimeError("session construction failed")

    def turn_detector(**kwargs):
        turn_detector_options.update(kwargs)
        return object()

    finalize = AsyncMock()
    monkeypatch.setattr(livekit_worker, "_load_runtime", load_runtime)
    monkeypatch.setattr(livekit_worker, "_open_call", AsyncMock(return_value=call_id))
    monkeypatch.setattr(livekit_worker, "_finish_call", finalize)
    monkeypatch.setattr(livekit_worker.inworld, "STT", stt)
    monkeypatch.setattr(livekit_worker.inworld, "TTS", tts)
    monkeypatch.setattr(livekit_worker.inference, "TurnDetector", turn_detector)

    def llm(**kwargs):
        llm_options.update(kwargs)
        return object()

    monkeypatch.setattr(livekit_worker.openai, "LLM", llm)
    monkeypatch.setattr(livekit_worker, "AgentSession", agent_session)

    with pytest.raises(RuntimeError, match="session construction failed"):
        await livekit_worker.vav_inworld_session(Context())

    assert stt_options["language"] == "en-GB"
    assert stt_options["enable_voice_profile"] is False
    assert stt_options["model"] == "assemblyai/u3-rt-pro"
    assert stt_options["min_end_of_turn_silence_when_confident"] == 300
    assert stt_options["end_of_turn_confidence_threshold"] == 0.5
    assert tts_options == {
        "api_key": "inworld-key",
        "model": "inworld-tts-2",
        "voice": "Ashley",
        "language": "en-GB",
        "speaking_rate": 1.0,
        "delivery_mode": "BALANCED",
        "text_normalization": "ON",
    }
    assert llm_options == {"api_key": "tenant-openai-key", "model": "gpt-4o-mini"}
    assert turn_detector_options == {}
    assert session_options["turn_handling"]["turn_detection"] == "stt"
    assert session_options["turn_handling"]["endpointing"] == {
        "mode": "fixed",
        "min_delay": 0.1,
        "max_delay": 0.35,
    }
    assert session_options["turn_handling"]["interruption"] == {
        "enabled": True,
        "mode": "vad",
        "min_duration": 0.2,
        "min_words": 1,
        "resume_false_interruption": True,
        "false_interruption_timeout": 1.0,
    }
    assert session_options["turn_handling"]["preemptive_generation"] == {
        "enabled": False,
        "preemptive_tts": False,
        "max_speech_duration": 10.0,
        "max_retries": 3,
    }
    assert len(shutdown_callbacks) == 1
    assert finalize.await_count == 1
    assert isinstance(finalize.await_args.kwargs["failure"], RuntimeError)
    await shutdown_callbacks[0]()
    assert finalize.await_count == 1


def test_livekit_agent_enforces_fixed_language_and_repairs_uncertain_transcripts():
    model = Agent(
        tenant_id=uuid4(),
        name="English receptionist",
        system_prompt="Detect and answer in any language when possible.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
        language_switching_enabled=False,
    )

    instructions = livekit_worker.VAVInworldAgent(model=model).instructions

    assert "Speak only in the configured primary language, en-GB" in instructions
    assert "overrides any broader or conflicting language claim" in instructions
    assert "without\n  relying on business knowledge" in instructions
    assert "automatically added to the current turn" in instructions
    assert "Do not search corrupted text and do not guess" in instructions
    assert "Never quote it" in instructions


@pytest.mark.asyncio
async def test_livekit_agent_injects_bounded_knowledge_before_one_llm_response(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Knowledge receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock(return_value="Source: Group overview\nVerified group companies.")
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    agent = livekit_worker.VAVInworldAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    message = turn_ctx.add_message(role="user", content="Which companies are in the group?")

    await agent.on_user_turn_completed(turn_ctx, message)

    retrieval.assert_awaited_once()
    assert retrieval.await_args.kwargs["query"] == "Which companies are in the group?"
    assert retrieval.await_args.kwargs["limit"] == livekit_worker.VOICE_KNOWLEDGE_MATCH_LIMIT
    assert (
        retrieval.await_args.kwargs["max_context_chars"]
        == livekit_worker.VOICE_KNOWLEDGE_CONTEXT_CHARS
    )
    assert "Verified group companies" in turn_ctx.messages()[-1].text_content


@pytest.mark.asyncio
async def test_livekit_agent_stays_silent_for_bare_hold_and_skips_control_search(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Patient receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock()
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    agent = livekit_worker.VAVInworldAgent(model=model)

    hold_ctx = livekit_worker.llm.ChatContext.empty()
    hold = hold_ctx.add_message(role="user", content="Wait.")
    with pytest.raises(livekit_worker.llm.StopResponse):
        await agent.on_user_turn_completed(hold_ctx, hold)

    stop_ctx = livekit_worker.llm.ChatContext.empty()
    stop = stop_ctx.add_message(role="user", content="Stop! Stop! Stop!")
    with pytest.raises(livekit_worker.llm.StopResponse):
        await agent.on_user_turn_completed(stop_ctx, stop)

    mistranscribed_stop_ctx = livekit_worker.llm.ChatContext.empty()
    mistranscribed_stop = mistranscribed_stop_ctx.add_message(
        role="user", content="You. It is top."
    )
    with pytest.raises(livekit_worker.llm.StopResponse):
        await agent.on_user_turn_completed(mistranscribed_stop_ctx, mistranscribed_stop)

    control_ctx = livekit_worker.llm.ChatContext.empty()
    control = control_ctx.add_message(role="user", content="Can you? Or slowly.")
    await agent.on_user_turn_completed(control_ctx, control)
    retrieval.assert_not_awaited()


@pytest.mark.asyncio
async def test_livekit_agent_recovers_group_knowledge_from_mistranscribed_entity(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Al Zaabi Group Receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock(
        side_effect=[None, "Source: Divisions — Al Zaabi Group\nVerified group companies."]
    )
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    agent = livekit_worker.VAVInworldAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    message = turn_ctx.add_message(role="user", content="What companies are part of Alzebra Group?")

    await agent.on_user_turn_completed(turn_ctx, message)

    assert retrieval.await_count == 2
    assert retrieval.await_args_list[0].kwargs["query"] == (
        "What companies are part of Alzebra Group?"
    )
    assert retrieval.await_args_list[1].kwargs["query"] == (
        "Al Zaabi Group overview divisions companies services"
    )
    assert "Verified group companies" in turn_ctx.messages()[-1].text_content


@pytest.mark.asyncio
async def test_livekit_agent_anchors_pronoun_follow_up_to_business_scope(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Al Zaabi Group Receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock(return_value="Source: Trading\nVerified building materials division.")
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    agent = livekit_worker.VAVInworldAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    message = turn_ctx.add_message(
        role="user", content="Do they have a building materials division?"
    )

    await agent.on_user_turn_completed(turn_ctx, message)

    assert retrieval.await_args.kwargs["query"] == (
        "Al Zaabi Group. Do they have a building materials division?"
    )


@pytest.mark.asyncio
async def test_livekit_agent_repairs_misheard_scoped_name_for_retrieval(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Al Zaabi Group Receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock(return_value="Source: Management\nVerified chairman.")
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    agent = livekit_worker.VAVInworldAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    message = turn_ctx.add_message(role="user", content="Who is the chairman of Al Sabah Group?")

    await agent.on_user_turn_completed(turn_ctx, message)

    assert retrieval.await_args.kwargs["query"] == ("Who is the chairman of Al Zaabi Group?")


@pytest.mark.asyncio
async def test_livekit_agent_resolves_elliptical_follow_up_before_retrieval(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Knowledge receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock(return_value="Source: Group overview\nVerified group companies.")
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    agent = livekit_worker.VAVInworldAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    turn_ctx.add_message(role="user", content="Which companies are in Al Zaabi Group?")
    turn_ctx.add_message(role="assistant", content="There are several companies.")
    follow_up = turn_ctx.add_message(role="user", content="Tell me more.")

    await agent.on_user_turn_completed(turn_ctx, follow_up)

    assert retrieval.await_args.kwargs["query"] == (
        "Which companies are in Al Zaabi Group? Tell me more."
    )


def test_livekit_usage_snapshot_reads_cumulative_model_usage_once():
    usage = SimpleNamespace(
        model_usage=[
            SimpleNamespace(type="llm_usage", input_tokens=1200, output_tokens=300),
            SimpleNamespace(type="tts_usage", characters_count=640),
            SimpleNamespace(type="stt_usage", audio_duration=42.5),
        ]
    )

    assert _usage_snapshot(usage) == {
        "llm_input_tokens": 1200,
        "llm_output_tokens": 300,
        "tts_characters": 640,
        "stt_audio_seconds": 42.5,
    }


@pytest.mark.asyncio
async def test_hybrid_worker_prefers_tenant_openai_key_and_fails_closed_if_unreadable(
    monkeypatch,
):
    tenant_id = uuid4()

    async def tenant_credentials(_db, loaded_tenant_id, provider):
        assert loaded_tenant_id == tenant_id
        return {"api_key": f"tenant-{provider}-key"}

    monkeypatch.setattr(livekit_worker, "load_provider_config", tenant_credentials)
    monkeypatch.setattr(livekit_worker.settings, "openai_api_key", "platform-openai-key")
    keys = await livekit_worker._load_runtime_api_keys(
        object(), tenant_id=tenant_id, llm_provider="openai"
    )
    assert keys.speech == "tenant-inworld-key"
    assert keys.llm == "tenant-openai-key"
    assert "tenant-openai-key" not in repr(keys)

    async def unreadable_openai(_db, _tenant_id, provider):
        if provider == "openai":
            raise ProviderCredentialError("cannot decrypt")
        return {"api_key": "tenant-inworld-key"}

    monkeypatch.setattr(livekit_worker, "load_provider_config", unreadable_openai)
    with pytest.raises(RuntimeError, match="OpenAI credential is unavailable"):
        await livekit_worker._load_runtime_api_keys(
            object(), tenant_id=tenant_id, llm_provider="openai"
        )


def test_inworld_tts_options_keep_auto_language_for_multilingual_agents():
    options = _inworld_tts_options(
        api_key="inworld-key",
        model=SimpleNamespace(
            voice_id="inworld:Olivia",
            language="en-GB",
            language_switching_enabled=True,
            speech_rate=0.95,
        ),
    )

    assert options == {
        "api_key": "inworld-key",
        "model": "inworld-tts-2",
        "voice": "Olivia",
        "speaking_rate": 0.95,
        "delivery_mode": "BALANCED",
        "text_normalization": "ON",
    }


def test_inworld_tts_options_use_native_profile_delivery_mode():
    options = _inworld_tts_options(
        api_key="inworld-key",
        model=SimpleNamespace(
            voice_id="inworld:Victoria",
            language="en-GB",
            language_switching_enabled=False,
            speech_rate=1.0,
        ),
        profile=SimpleNamespace(runtime_config={"tts_delivery_mode": "creative"}),
    )

    assert options["delivery_mode"] == "CREATIVE"
    assert options["language"] == "en-GB"


def test_inworld_stt_model_uses_fast_accurate_route_for_english():
    selected = _inworld_stt_model(
        model=SimpleNamespace(language="en-GB", supported_languages=["en-GB"]),
        profile=SimpleNamespace(stt_language="en-GB", runtime_config=None),
    )

    assert selected == "assemblyai/u3-rt-pro"


def test_inworld_stt_model_uses_wide_multilingual_route_for_arabic_and_hindi():
    selected = _inworld_stt_model(
        model=SimpleNamespace(language="ar-AE", supported_languages=["ar-AE", "hi-IN"]),
        profile=SimpleNamespace(stt_language="auto", runtime_config=None),
    )

    assert selected == "soniox/stt-rt-v4"


def test_inworld_stt_model_preserves_explicit_operator_choice():
    selected = _inworld_stt_model(
        model=SimpleNamespace(language="en", supported_languages=["en"]),
        profile=SimpleNamespace(
            stt_language="en",
            runtime_config={"stt_model": "inworld/inworld-stt-1"},
        ),
    )

    assert selected == "inworld/inworld-stt-1"


def test_livekit_turn_latency_is_recorded_as_public_runtime_metrics():
    runtime_metrics = {"turn_count": 0}
    samples = []

    _capture_turn_latency(
        role="assistant",
        metrics={"llm_node_ttft": 0.24, "tts_node_ttfb": 0.31, "e2e_latency": 0.91},
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
    )
    _capture_turn_latency(
        role="assistant",
        metrics=SimpleNamespace(llm_node_ttft=0.2, tts_node_ttfb=0.3, e2e_latency=1.2),
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
    )
    _capture_turn_latency(
        role="user",
        metrics={"e2e_latency": 0.1},
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
    )

    assert runtime_metrics == {
        "turn_count": 2,
        "last_llm_first_token_ms": 200,
        "last_tts_first_byte_ms": 300,
        "last_transcript_to_first_audio_ms": 500,
        "last_speech_end_to_first_audio_ms": 1200,
        "turn_latency_p50_ms": 910,
        "turn_latency_p95_ms": 1200,
    }


def test_livekit_worker_health_server_uses_valid_railway_port():
    assert _worker_http_port("8081") == 8081
    assert _worker_http_port(None) is None

    with pytest.raises(RuntimeError, match="between 1 and 65535"):
        _worker_http_port("70000")

    with pytest.raises(RuntimeError, match="must be an integer"):
        _worker_http_port("not-a-port")


def test_livekit_worker_idle_processes_are_bounded_for_small_services():
    assert _worker_idle_processes(None) == 1
    assert _worker_idle_processes("2") == 2

    with pytest.raises(RuntimeError, match="between 1 and 16"):
        _worker_idle_processes("0")
    with pytest.raises(RuntimeError, match="must be an integer"):
        _worker_idle_processes("many")


@pytest.mark.asyncio
async def test_livekit_worker_health_checks_connection_and_registered_name():
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(200, text="OK")
        return httpx.Response(
            200,
            json={
                "worker_type": "JT_ROOM",
                "agent_name": "vav-inworld",
                "active_jobs": 0,
            },
        )

    provider = LiveKitSIPProvider(
        url="wss://example.livekit.cloud",
        api_key="key",
        api_secret="secret",
        http_transport=httpx.MockTransport(handler),
    )
    await provider.verify_worker(
        health_url="http://livekit-agent.internal:8080/",
        agent_name="vav-inworld",
    )

    assert requested_paths == ["/", "/worker"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root_status", "registered_name", "expected"),
    [
        (503, "vav-inworld", "not connected and healthy"),
        (200, "wrong-agent", "wrong agent name"),
    ],
)
async def test_livekit_worker_health_fails_closed(
    root_status: int,
    registered_name: str,
    expected: str,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(root_status, text="OK")
        return httpx.Response(
            200,
            json={"worker_type": "JT_ROOM", "agent_name": registered_name},
        )

    provider = LiveKitSIPProvider(
        url="wss://example.livekit.cloud",
        api_key="key",
        api_secret="secret",
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LiveKitSIPError, match=expected):
        await provider.verify_worker(
            health_url="http://livekit-agent.internal:8080",
            agent_name="vav-inworld",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outbound_address", "from_host"),
    [("trunk.example.ae", ""), ("", "trunk.example.ae")],
)
async def test_livekit_route_verification_checks_trunks_rule_worker_and_outbound_route(
    monkeypatch,
    outbound_address: str,
    from_host: str,
):
    agent_id = uuid4()

    class SIP:
        async def list_sip_inbound_trunk(self, _request):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        sip_trunk_id="ST_in",
                        numbers=["+97141234567"],
                    )
                ]
            )

        async def list_sip_dispatch_rule(self, _request):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        sip_dispatch_rule_id="SDR_rule",
                        trunk_ids=["ST_in"],
                        inbound_numbers=["+97141234567"],
                        numbers=[],
                        room_config=SimpleNamespace(
                            agents=[
                                SimpleNamespace(
                                    agent_name="vav-inworld",
                                    metadata=f'{{"agent_id":"{agent_id}"}}',
                                )
                            ]
                        ),
                    )
                ]
            )

        async def list_sip_outbound_trunk(self, _request):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        sip_trunk_id="ST_out",
                        numbers=["+97141234567"],
                        address=outbound_address,
                        from_host=from_host,
                    )
                ]
            )

    class FakeLiveKit:
        def __init__(self, **_kwargs):
            self.sip = SIP()

        async def aclose(self):
            return None

    monkeypatch.setattr("app.telephony.livekit_provider.api.LiveKitAPI", FakeLiveKit)
    await LiveKitSIPProvider(
        url="wss://example.livekit.cloud", api_key="key", api_secret="secret"
    ).verify_route(
        inbound_trunk_id="ST_in",
        dispatch_rule_id="SDR_rule",
        outbound_trunk_id="ST_out",
        sip_uri="sip:trunk.example.ae",
        agent_name="vav-inworld",
        assigned_numbers=["+97141234567"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outbound_numbers", "outbound_address", "expected"),
    [
        ([], "trunk.example.ae", "does not explicitly allow every assigned caller DID"),
        (["+97141234567"], "wrong.example.ae", "does not match"),
    ],
)
async def test_livekit_route_verification_rejects_unusable_outbound_route(
    monkeypatch,
    outbound_numbers: list[str],
    outbound_address: str,
    expected: str,
):
    class SIP:
        async def list_sip_inbound_trunk(self, _request):
            return SimpleNamespace(
                items=[SimpleNamespace(sip_trunk_id="ST_in", numbers=["+97141234567"])]
            )

        async def list_sip_dispatch_rule(self, _request):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        sip_dispatch_rule_id="SDR_rule",
                        trunk_ids=["ST_in"],
                        inbound_numbers=["+97141234567"],
                        numbers=[],
                        room_config=SimpleNamespace(
                            agents=[SimpleNamespace(agent_name="vav-inworld")]
                        ),
                    )
                ]
            )

        async def list_sip_outbound_trunk(self, _request):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        sip_trunk_id="ST_out",
                        numbers=outbound_numbers,
                        address=outbound_address,
                        from_host="",
                    )
                ]
            )

    class FakeLiveKit:
        def __init__(self, **_kwargs):
            self.sip = SIP()

        async def aclose(self):
            return None

    monkeypatch.setattr("app.telephony.livekit_provider.api.LiveKitAPI", FakeLiveKit)

    with pytest.raises(LiveKitSIPError, match=expected):
        await LiveKitSIPProvider(
            url="wss://example.livekit.cloud", api_key="key", api_secret="secret"
        ).verify_route(
            inbound_trunk_id="ST_in",
            dispatch_rule_id="SDR_rule",
            outbound_trunk_id="ST_out",
            sip_uri="sip:trunk.example.ae",
            agent_name="vav-inworld",
            assigned_numbers=["+97141234567"],
        )
