"""LiveKit SIP transport tests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from livekit import api
from sqlalchemy import func, select

from app.livekit_runtime import inworld_realtime as inworld_realtime_adapter
from app.livekit_runtime import worker as livekit_worker
from app.livekit_runtime.inworld_single_pass import SinglePassTurnOutcome
from app.livekit_runtime.worker import (
    _build_inworld_realtime_model,
    _capture_turn_latency,
    _inworld_recognition_terms,
    _inworld_stt_model,
    _inworld_tts_options,
    _inworld_voice_runtime,
    _LiveKitRuntimeTelemetry,
    _no_match_response_outcome,
    _record_external_tts_request,
    _runtime_date_context,
    _usage_snapshot,
    _worker_http_port,
    _worker_idle_processes,
)
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    AgentRuntimeProfile,
    KnowledgeBase,
    KnowledgeSource,
)
from app.models.call import Call
from app.models.campaign import ProviderCallbackOutbox
from app.services.call_disposition import summarize_runtime_grounding
from app.services.knowledge_serving import (
    KnowledgeServingError,
    knowledge_admission_is_durable,
    knowledge_call_reservation_metadata,
    pre_admit_outbound_knowledge_call,
    publish_serving_revision,
)
from app.services.provider_credentials import ProviderCredentialError
from app.services.speech_lexicon import publish_speech_lexicon
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
async def test_inbound_open_locks_capacity_before_knowledge_admission(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Inbound lock-order agent",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
        assigned_numbers=["+97141234567"],
    )
    db.add(profile)
    await db.commit()
    lock_order: list[str] = []

    async def track_capacity(*_args, **_kwargs):
        lock_order.append("capacity")

    async def track_knowledge(*_args, **_kwargs):
        lock_order.append("knowledge")

    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(livekit_worker, "_enforce_inbound_limits", track_capacity)
    monkeypatch.setattr(livekit_worker, "_admit_reserved_knowledge_pin", track_knowledge)

    call_id = await livekit_worker._open_call(
        model=agent,
        profile=profile,
        room_name=f"vav-inbound-lock-order-{uuid4()}",
        attributes={
            "sip.callDirection": "inbound",
            "sip.phoneNumber": "+971501234567",
            "sip.trunkPhoneNumber": "+97141234567",
        },
    )

    assert lock_order == ["capacity", "knowledge"]
    assert await db.get(Call, call_id) is not None


@pytest.mark.asyncio
async def test_pre_admitted_outbound_worker_keeps_reserved_revision_after_later_changes(
    db,
    tenant,
    monkeypatch,
):
    monkeypatch.setattr(livekit_worker.settings, "inworld_api_key", "inworld-test-key")
    agent = Agent(
        tenant_id=tenant.id,
        name="Pinned outbound agent",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
        max_call_duration_seconds=60,
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Pinned outbound knowledge",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Version one",
        status="indexed",
        content="The approved support number is +971 2 111 1111.",
    )
    knowledge.sources.append(source)
    db.add_all((agent, knowledge))
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
        stt_language="en-GB",
        assigned_numbers=["+97141234567"],
    )
    db.add_all(
        (
            profile,
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=agent.id,
                knowledge_base_id=knowledge.id,
            ),
        )
    )
    first_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    first_revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=first_lexicon,
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
            "speech_provider": "inworld",
            "runtime": {
                "transport": "livekit_sip",
                "speech_provider": "inworld",
                **knowledge_call_reservation_metadata(first_revision, 0),
            },
        },
    )
    db.add(call)
    await db.flush()
    call = await pre_admit_outbound_knowledge_call(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        call_id=call.id,
    )
    assert knowledge_admission_is_durable(call.call_metadata)
    await db.commit()
    agent_id = agent.id
    knowledge_id = knowledge.id
    call_id = call.id
    first_revision_id = first_revision.id
    first_content_sha256 = first_revision.content_sha256
    first_source_sha256 = first_revision.source_revision_sha256

    # Publish a new green revision after the outbound reservation was created
    # but before the worker joins the LiveKit room.
    source.content = "The new support number is +971 2 222 2222."
    await db.flush()
    second_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    second_revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=second_lexicon,
    )
    await db.commit()
    second_revision_id = second_revision.id
    assert second_revision_id != first_revision_id
    assert knowledge.serving_revision_id == second_revision_id

    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    original_hashes = {
        "knowledge_serving_content_sha256": first_content_sha256,
        "knowledge_source_revision_sha256": first_source_sha256,
    }
    for field_name, expected_hash in original_hashes.items():
        reserved_call = await db.get(Call, call_id)
        metadata = dict(reserved_call.call_metadata)
        runtime = dict(metadata["runtime"])
        runtime[field_name] = "tampered"
        reserved_call.call_metadata = {**metadata, "runtime": runtime}
        await db.commit()

        with pytest.raises(RuntimeError, match="failed integrity validation"):
            await livekit_worker._load_runtime(agent_id, call_id=call_id)

        reserved_call = await db.get(Call, call_id)
        metadata = dict(reserved_call.call_metadata)
        reserved_call.call_metadata = {
            **metadata,
            "runtime": {**dict(metadata["runtime"]), field_name: expected_hash},
        }
        await db.commit()

    loaded_model, loaded_profile, _api_keys, knowledge_pin = await livekit_worker._load_runtime(
        agent_id, call_id=call_id
    )
    assert knowledge_pin.revision_id == first_revision_id
    assert knowledge_pin.content_sha256 == first_content_sha256
    assert knowledge_pin.revocation_generation == 0
    assert knowledge_pin.revision_id != second_revision_id

    with pytest.raises(RuntimeError, match="knowledge revision changed before connect"):
        await livekit_worker._open_call(
            model=loaded_model,
            profile=loaded_profile,
            room_name=f"vav-call-{call_id}",
            attributes={"sip.callDirection": "outbound"},
            dispatched_call_id=call_id,
            knowledge_pin=livekit_worker._RuntimeKnowledgePin.from_revision(second_revision),
        )

    # A later ordinary publication may move the live pointer, but does not
    # invalidate the immutable release already reserved for this paid call.
    current_knowledge = await db.get(KnowledgeBase, knowledge_id)
    await livekit_worker._admit_reserved_knowledge_pin(
        db,
        model=loaded_model,
        knowledge_pin=knowledge_pin,
        call=await db.get(Call, call_id),
    )

    # This outbound reservation crossed its durable admission boundary before
    # dialing. A later rebind or explicit revoke must not make an answered,
    # paid call go silent; it keeps only the immutable release it admitted.
    alternate_knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Alternate approved knowledge",
        approval_status="approved",
        sync_status="ready",
        is_active=True,
    )
    db.add(alternate_knowledge)
    await db.flush()
    binding = await db.scalar(
        select(AgentKnowledgeBinding).where(AgentKnowledgeBinding.agent_id == agent_id)
    )
    binding.knowledge_base_id = alternate_knowledge.id
    await db.commit()
    current_knowledge = await db.get(KnowledgeBase, knowledge_id)
    current_knowledge.serving_revocation_generation += 1
    current_knowledge.serving_revision_id = None
    current_knowledge.approval_status = "draft"
    await db.commit()
    reloaded_model, reloaded_profile, _api_keys, reloaded_pin = await livekit_worker._load_runtime(
        agent_id, call_id=call_id
    )
    assert reloaded_pin.knowledge_base_id == knowledge_id
    assert reloaded_pin.revision_id == first_revision_id
    assert (
        await livekit_worker._open_call(
            model=reloaded_model,
            profile=reloaded_profile,
            room_name=f"vav-call-{call_id}",
            attributes={"sip.callDirection": "outbound"},
            dispatched_call_id=call_id,
            knowledge_pin=reloaded_pin,
        )
        == call_id
    )

    # Once admitted, a later publication/revocation does not mutate the
    # immutable release already serving the active call, and a duplicate job
    # cannot start a second agent session for that same paid call.
    with pytest.raises(
        livekit_worker.OutboundReservationAlreadyClaimedError,
        match="already claimed",
    ):
        await livekit_worker._open_call(
            model=loaded_model,
            profile=loaded_profile,
            room_name=f"vav-call-{call_id}",
            attributes={"sip.callDirection": "outbound"},
            dispatched_call_id=call_id,
            knowledge_pin=reloaded_pin,
        )


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
async def test_duplicate_outbound_worker_never_fails_or_hangs_up_active_call(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Already active outbound agent",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    db.add(agent)
    await db.flush()
    call_id = uuid4()
    room_name = f"vav-call-{call_id}"
    call = Call(
        id=call_id,
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="in_progress",
        from_number="+97141234567",
        to_number="+971501234567",
        provider="livekit_sip",
        call_metadata={
            "speech_provider": "inworld",
            "livekit_room": room_name,
            "runtime": {"transport": "livekit_sip", "speech_provider": "inworld"},
        },
    )
    db.add(call)
    await db.commit()

    class Room:
        name = room_name

    class Context:
        job = SimpleNamespace(
            metadata=json.dumps({"agent_id": str(agent.id), "call_id": str(call_id)})
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

    profile = SimpleNamespace()
    load_runtime = AsyncMock(
        return_value=(
            agent,
            profile,
            livekit_worker._RuntimeApiKeys(speech="inworld-key", llm="inworld-key"),
            livekit_worker._RuntimeKnowledgePin(),
        )
    )
    open_call = AsyncMock(
        side_effect=livekit_worker.OutboundReservationAlreadyClaimedError(
            "Outbound call reservation was already claimed"
        )
    )
    delete_room = AsyncMock(return_value=True)
    monkeypatch.setattr(livekit_worker, "_load_runtime", load_runtime)
    monkeypatch.setattr(livekit_worker, "_open_call", open_call)
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)

    with pytest.raises(livekit_worker.OutboundReservationAlreadyClaimedError):
        await livekit_worker.vav_inworld_session(Context())

    delete_room.assert_not_awaited()
    db.expire_all()
    unchanged = await db.get(Call, call_id)
    assert unchanged.status == "in_progress"
    assert unchanged.ended_at is None
    assert "lifecycle_error" not in unchanged.call_metadata


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
        "_resolve_inbound_route",
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
    assert record.participant_was_active is True
    assert record.billing_state == "unattributed_provider_reconciliation_required"
    assert trunk_id not in caplog.text
    assert called_number not in caplog.text
    assert room_name not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_failure",
    (
        "Inbound LiveKit calls require an immutable published knowledge revision",
        "OpenAI credential is unavailable",
    ),
)
async def test_answered_inbound_dependency_failure_is_durable_and_pending_billing(
    db,
    tenant,
    monkeypatch,
    runtime_failure,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Inbound billing-truth agent",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en-GB",
        assigned_numbers=["+97141234567"],
        max_concurrent_calls=5,
        daily_call_limit=100,
        monthly_budget_cents=10_000,
    )
    db.add(profile)
    await db.commit()
    tenant_id = tenant.id
    agent_id = agent.id
    room_name = f"vav-inbound-{uuid4()}"
    sip_call_id = f"sip-{uuid4()}"

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
                    "sip.callIDFull": sip_call_id,
                    "sip.phoneNumber": "+971501234567",
                    "sip.trunkID": "ST_inbound",
                    "sip.trunkPhoneNumber": "+97141234567",
                },
            )

    async def fail_after_durable_reservation(*, tenant_id, agent_id):
        assert tenant_id == tenant_id_expected
        assert agent_id == agent_id_expected
        async with session_factory() as verification_db:
            reserved = await verification_db.scalar(
                select(Call).where(Call.provider_call_sid == sip_call_id)
            )
            assert reserved is not None
            assert reserved.status == "in_progress"
            assert reserved.call_metadata["runtime"]["cost_state"] == (
                "pending_provider_billing_sync"
            )
        raise RuntimeError(runtime_failure)

    delete_room = AsyncMock(return_value=True)
    outbox_kick = Mock()
    tenant_id_expected = tenant_id
    agent_id_expected = agent_id
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(
        livekit_worker,
        "_resolve_inbound_route",
        AsyncMock(return_value=(agent, profile)),
    )
    monkeypatch.setattr(livekit_worker, "_load_inbound_runtime", fail_after_durable_reservation)
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)
    monkeypatch.setattr(
        "app.tasks.campaign_tasks.dispatch_provider_callback_outbox.delay",
        outbox_kick,
    )

    with pytest.raises(RuntimeError, match=runtime_failure):
        await livekit_worker.vav_inworld_session(Context())

    delete_room.assert_awaited_once_with(
        url=livekit_worker.settings.livekit_url,
        api_key=livekit_worker.settings.livekit_api_key,
        api_secret=livekit_worker.settings.livekit_api_secret,
        room_name=room_name,
    )
    db.expire_all()
    failed_call = await db.scalar(
        select(Call)
        .where(Call.provider_call_sid == sip_call_id)
        .execution_options(populate_existing=True)
    )
    assert failed_call is not None
    assert failed_call.tenant_id == tenant_id
    assert failed_call.agent_id == agent_id
    assert failed_call.status == "failed"
    assert failed_call.started_at is not None
    assert failed_call.answered_at is not None
    assert failed_call.ended_at is not None
    assert failed_call.duration_seconds >= 1
    assert failed_call.call_metadata["livekit_room"] == room_name
    assert failed_call.call_metadata["lifecycle_error"] == ("livekit_inbound_preopen_failure")
    assert failed_call.call_metadata["runtime_failure_type"] == "RuntimeError"
    assert failed_call.call_metadata["runtime"]["runtime_setup_state"] == (
        "failed_before_session_start"
    )
    assert failed_call.call_metadata["runtime"]["cost_state"] == ("pending_provider_billing_sync")
    assert failed_call.call_metadata["runtime"]["media_stream_started"] is False
    assert runtime_failure not in json.dumps(failed_call.call_metadata)
    outbox = await db.scalar(
        select(ProviderCallbackOutbox).where(ProviderCallbackOutbox.call_id == failed_call.id)
    )
    assert outbox is not None
    assert outbox.action == "process_completed_call"
    assert outbox.status == "pending"
    outbox_kick.assert_called_once_with(str(outbox.id))


@pytest.mark.asyncio
async def test_answered_inbound_limit_rejection_terminalizes_and_hangs_up(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Capacity-bound inbound agent",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en-GB",
        assigned_numbers=["+97141234567"],
        max_concurrent_calls=1,
        daily_call_limit=100,
        monthly_budget_cents=10_000,
    )
    existing = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number="+971509999999",
        to_number="+97141234567",
        provider="livekit_sip",
        provider_call_sid=f"sip-existing-{uuid4()}",
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
    )
    db.add_all((profile, existing))
    await db.commit()
    agent_id = agent.id
    room_name = f"vav-inbound-capacity-{uuid4()}"
    sip_call_id = f"sip-capacity-{uuid4()}"

    class Room:
        name = room_name

    class Context:
        job = SimpleNamespace(metadata="{}")
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity="inbound-capacity-caller",
                attributes={
                    "sip.callDirection": "inbound",
                    "sip.callStatus": "active",
                    "sip.callIDFull": sip_call_id,
                    "sip.phoneNumber": "+971501234567",
                    "sip.trunkID": "ST_inbound",
                    "sip.trunkPhoneNumber": "+97141234567",
                },
            )

    load_runtime = AsyncMock()
    delete_room = AsyncMock(return_value=True)
    outbox_kick = Mock()
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(
        livekit_worker,
        "_resolve_inbound_route",
        AsyncMock(return_value=(agent, profile)),
    )
    monkeypatch.setattr(livekit_worker, "_load_inbound_runtime", load_runtime)
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)
    monkeypatch.setattr(
        "app.tasks.campaign_tasks.dispatch_provider_callback_outbox.delay",
        outbox_kick,
    )

    with pytest.raises(
        livekit_worker.InboundReservationRejectedError,
        match="concurrent call limit",
    ):
        await livekit_worker.vav_inworld_session(Context())

    load_runtime.assert_not_awaited()
    delete_room.assert_awaited_once()
    db.expire_all()
    rejected = await db.scalar(
        select(Call)
        .where(Call.provider_call_sid == sip_call_id)
        .execution_options(populate_existing=True)
    )
    assert rejected is not None
    assert rejected.agent_id == agent_id
    assert rejected.status == "failed"
    assert rejected.duration_seconds >= 1
    assert rejected.call_metadata["lifecycle_error"] == "livekit_inbound_limit_rejection"
    assert rejected.call_metadata["runtime"]["runtime_setup_state"] == (
        "rejected_before_dependency_load"
    )
    assert rejected.call_metadata["runtime"]["cost_state"] == ("pending_provider_billing_sync")
    outbox = await db.scalar(
        select(ProviderCallbackOutbox).where(ProviderCallbackOutbox.call_id == rejected.id)
    )
    assert outbox is not None
    outbox_kick.assert_called_once_with(str(outbox.id))


@pytest.mark.asyncio
async def test_duplicate_inbound_job_cannot_fail_or_delete_legitimate_session(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Duplicate-safe inbound agent",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        assigned_numbers=["+97141234567"],
    )
    room_name = f"vav-inbound-owner-{uuid4()}"
    sip_call_id = f"sip-owned-{uuid4()}"
    owner = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number="+971501234567",
        to_number="+97141234567",
        provider="livekit_sip",
        provider_call_sid=sip_call_id,
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        call_metadata={
            "livekit_room": room_name,
            "runtime": {"transport": "livekit_sip"},
        },
    )
    db.add_all((profile, owner))
    await db.commit()
    owner_id = owner.id

    class Room:
        name = room_name

    class Context:
        job = SimpleNamespace(metadata="{}")
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity="duplicate-inbound-caller",
                attributes={
                    "sip.callDirection": "inbound",
                    "sip.callStatus": "active",
                    "sip.callIDFull": sip_call_id,
                    "sip.phoneNumber": "+971501234567",
                    "sip.trunkID": "ST_inbound",
                    "sip.trunkPhoneNumber": "+97141234567",
                },
            )

    delete_room = AsyncMock(return_value=True)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(
        livekit_worker,
        "_resolve_inbound_route",
        AsyncMock(return_value=(agent, profile)),
    )
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)

    with pytest.raises(livekit_worker.InboundReservationAlreadyClaimedError):
        await livekit_worker.vav_inworld_session(Context())

    delete_room.assert_not_awaited()
    db.expire_all()
    unchanged = await db.scalar(
        select(Call).where(Call.id == owner_id).execution_options(populate_existing=True)
    )
    assert unchanged is not None
    assert unchanged.status == "in_progress"
    assert unchanged.ended_at is None
    assert (
        await db.scalar(
            select(func.count()).select_from(Call).where(Call.provider_call_sid == sip_call_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_inbound_knowledge_revocation_after_load_fails_exact_reservation(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Revocation-safe inbound agent",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        max_call_duration_seconds=60,
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Inbound revision",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Approved facts",
            status="indexed",
            content="The approved support number is +971 2 111 1111.",
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        assigned_numbers=["+97141234567"],
        monthly_budget_cents=10_000,
    )
    db.add_all(
        (
            profile,
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=agent.id,
                knowledge_base_id=knowledge.id,
            ),
        )
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
    )
    await db.commit()
    revision_id = revision.id
    knowledge_id = knowledge.id
    knowledge_pin = livekit_worker._RuntimeKnowledgePin.from_revision(
        revision,
        revocation_generation=0,
    )
    room_name = f"vav-inbound-revoked-{uuid4()}"
    sip_call_id = f"sip-revoked-{uuid4()}"

    class Room:
        name = room_name

    class Context:
        job = SimpleNamespace(metadata="{}")
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity="inbound-revoked-caller",
                attributes={
                    "sip.callDirection": "inbound",
                    "sip.callStatus": "active",
                    "sip.callIDFull": sip_call_id,
                    "sip.phoneNumber": "+971501234567",
                    "sip.trunkID": "ST_inbound",
                    "sip.trunkPhoneNumber": "+97141234567",
                },
            )

    original_admit = livekit_worker._admit_inbound_call

    async def revoke_then_admit(**kwargs):
        async with session_factory() as revocation_db:
            current = await revocation_db.get(KnowledgeBase, knowledge_id)
            current.serving_revocation_generation += 1
            current.serving_revision_id = None
            current.approval_status = "draft"
            await revocation_db.commit()
        await original_admit(**kwargs)

    delete_room = AsyncMock(return_value=True)
    outbox_kick = Mock()
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(
        livekit_worker,
        "_resolve_inbound_route",
        AsyncMock(return_value=(agent, profile)),
    )
    monkeypatch.setattr(
        livekit_worker,
        "_load_inbound_runtime",
        AsyncMock(
            return_value=(
                agent,
                profile,
                livekit_worker._RuntimeApiKeys(
                    speech="inworld-test-key",
                    llm="openai-test-key",
                ),
                knowledge_pin,
            )
        ),
    )
    monkeypatch.setattr(livekit_worker, "_admit_inbound_call", revoke_then_admit)
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)
    monkeypatch.setattr(
        "app.tasks.campaign_tasks.dispatch_provider_callback_outbox.delay",
        outbox_kick,
    )

    with pytest.raises(KnowledgeServingError, match="revoked before admission"):
        await livekit_worker.vav_inworld_session(Context())

    delete_room.assert_awaited_once()
    db.expire_all()
    failed_call = await db.scalar(
        select(Call)
        .where(Call.provider_call_sid == sip_call_id)
        .execution_options(populate_existing=True)
    )
    assert failed_call is not None
    assert failed_call.status == "failed"
    assert failed_call.duration_seconds >= 1
    assert failed_call.call_metadata["lifecycle_error"] == ("livekit_inbound_preopen_failure")
    assert failed_call.call_metadata["runtime"]["cost_state"] == ("pending_provider_billing_sync")
    assert failed_call.call_metadata["runtime"]["knowledge_serving_revision_id"] == str(revision_id)
    outbox_kick.assert_called_once()


@pytest.mark.asyncio
async def test_inbound_preopen_cleanup_survives_cancellation(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def terminalize(**_kwargs):
        entered.set()
        await release.wait()
        return True

    delete_room = AsyncMock(return_value=True)
    monkeypatch.setattr(livekit_worker, "_fail_inbound_preopen_call", terminalize)
    monkeypatch.setattr(livekit_worker, "delete_browser_room", delete_room)
    task = asyncio.create_task(
        livekit_worker._abort_inbound_preopen_despite_cancellation(
            tenant_id=uuid4(),
            agent_id=uuid4(),
            call_id=uuid4(),
            room_name="vav-inbound-cancelled-cleanup",
            failure=RuntimeError("cancelled setup"),
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    await task

    delete_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_session_start_failure_after_greeting_provider_request_is_billing_true(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    greeting = "Welcome to the approved support line."
    agent = Agent(
        tenant_id=tenant.id,
        name="Prewarm billing agent",
        system_prompt="Use approved knowledge.",
        greeting_message=greeting,
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        max_call_duration_seconds=60,
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        stt_language="en-GB",
        assigned_numbers=["+97141234567"],
    )
    room_name = f"vav-inbound-prewarm-{uuid4()}"
    sip_call_id = f"sip-prewarm-{uuid4()}"
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number="+971501234567",
        to_number="+97141234567",
        provider="livekit_sip",
        provider_call_sid=sip_call_id,
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        call_metadata={
            "livekit_room": room_name,
            "runtime": {
                "transport": "livekit_sip",
                "speech_provider": "inworld",
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "tts_model": "inworld-tts-2",
                "media_stream_started": False,
                "runtime_setup_state": "knowledge_admitted",
                "cost_state": "pending_provider_billing_sync",
            },
        },
    )
    db.add_all((profile, call))
    await db.commit()
    call_id = call.id

    class Room:
        name = room_name

        def on(self, _event, _callback):
            return None

    class Context:
        job = SimpleNamespace(metadata="{}")
        room = Room()

        async def connect(self):
            return None

        async def wait_for_participant(self):
            return SimpleNamespace(
                identity="inbound-prewarm-caller",
                attributes={
                    "sip.callDirection": "inbound",
                    "sip.callStatus": "active",
                    "sip.callIDFull": sip_call_id,
                    "sip.phoneNumber": "+971501234567",
                    "sip.trunkID": "ST_inbound",
                    "sip.trunkPhoneNumber": "+97141234567",
                },
            )

        def add_shutdown_callback(self, _callback):
            return None

        def shutdown(self, reason=""):
            return None

    class PreparedGreeting:
        cache_status = "miss_cached"
        provider_request_count = 1
        started_at_monotonic = 1.0
        first_frame_at_monotonic = 1.01
        completed_at_monotonic = 1.02
        failed_before_playout = False

        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

        async def frames(self):
            if False:
                yield None

    class FailingSession:
        def __init__(self, **_kwargs):
            self.start_count = 0
            self.close_count = 0

        def on(self, _event):
            return lambda callback: callback

        async def start(self, **_kwargs):
            self.start_count += 1
            raise RuntimeError("session start failed")

        async def aclose(self):
            self.close_count += 1

    prepared = PreparedGreeting()
    session_holder = {}

    def build_session(**kwargs):
        session_holder["options"] = kwargs
        session_holder["session"] = FailingSession(**kwargs)
        return session_holder["session"]

    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    monkeypatch.setattr(
        livekit_worker,
        "_resolve_inbound_route",
        AsyncMock(return_value=(agent, profile)),
    )
    monkeypatch.setattr(
        livekit_worker,
        "_reserve_inbound_call",
        AsyncMock(return_value=call_id),
    )
    monkeypatch.setattr(
        livekit_worker,
        "_load_inbound_runtime",
        AsyncMock(
            return_value=(
                agent,
                profile,
                livekit_worker._RuntimeApiKeys(
                    speech="inworld-test-key",
                    llm="openai-test-key",
                ),
                livekit_worker._RuntimeKnowledgePin(),
            )
        ),
    )
    monkeypatch.setattr(livekit_worker, "_admit_inbound_call", AsyncMock())
    monkeypatch.setattr(livekit_worker.inworld, "STT", lambda **_kwargs: object())
    monkeypatch.setattr(livekit_worker.inworld, "TTS", lambda **_kwargs: object())
    monkeypatch.setattr(livekit_worker.openai, "LLM", lambda **_kwargs: object())
    monkeypatch.setattr(livekit_worker.inference, "TurnDetector", lambda **_kwargs: object())
    monkeypatch.setattr(livekit_worker, "AgentSession", build_session)
    monkeypatch.setattr(livekit_worker, "prepare_greeting_audio", lambda **_kwargs: prepared)
    monkeypatch.setattr(livekit_worker, "production_room_options", lambda: object())
    outbox_kick = Mock()
    monkeypatch.setattr(
        "app.tasks.campaign_tasks.dispatch_provider_callback_outbox.delay",
        outbox_kick,
    )

    with pytest.raises(RuntimeError, match="session start failed"):
        await livekit_worker.vav_inworld_session(Context())

    assert prepared.closed is True
    assert session_holder["session"].start_count == 1
    assert session_holder["session"].close_count == 1
    db.expire_all()
    failed_call = await db.scalar(
        select(Call).where(Call.id == call_id).execution_options(populate_existing=True)
    )
    assert failed_call is not None
    assert failed_call.status == "failed"
    runtime = failed_call.call_metadata["runtime"]
    assert runtime["media_stream_started"] is False
    assert runtime["external_tts_request_count"] == 1
    assert runtime["external_tts_characters"] == len(greeting)
    assert runtime["external_tts_sources"] == ["greeting_preparation"]
    assert runtime["external_tts_provider_reconciliation_required"] is True
    assert runtime["llm_input_tokens"] is None
    assert runtime["llm_output_tokens"] is None
    assert runtime["stt_audio_seconds"] is None
    assert failed_call.call_metadata["runtime_failure_type"] == "RuntimeError"
    outbox_kick.assert_called_once()

    response = await client.get("/api/v1/billing/cost-report?days=30", headers=auth_headers)

    assert response.status_code == 200
    row = next(item for item in response.json()["calls"] if item["call_id"] == str(call_id))
    services = {component["service"] for component in row["components"]}
    providers = {component["provider"] for component in row["components"]}
    assert "TTS 2" in services
    assert "Speech to text" not in services
    assert "OpenAI" not in providers
    assert "Inworld Router" not in providers
    tts = next(component for component in row["components"] if component["service"] == "TTS 2")
    assert tts["quantity"] == pytest.approx(len(greeting) / 1000)
    assert "direct prewarm characters only" in tts["basis"]
    assert row["cost_state"] == "pending_provider_billing_sync"
    assert "External TTS provider invoice reconciliation" in row["missing_cost_inputs"]
    assert "Provider invoice reconciliation" in row["missing_cost_inputs"]


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

    async def load_runtime(_agent_id, **kwargs):
        assert kwargs["call_id"] == call_id
        return (
            model,
            profile,
            livekit_worker._RuntimeApiKeys(
                speech="inworld-key",
                llm="tenant-openai-key",
            ),
            livekit_worker._RuntimeKnowledgePin(),
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
    assert "normally stop after one or two short sentences" in instructions
    assert 'Do not append routine closings such as "Is there anything else?"' in instructions
    assert "Never quote it" in instructions
    assert "current local date is" in instructions
    assert "caller is a search clue only" in instructions
    assert "verified founding year" in instructions


@pytest.mark.parametrize(
    ("utterance", "is_control"),
    [
        ("Can you hear me?", True),
        ("Please speak slower.", True),
        ("How do you pronounce Al Zaabi?", True),
        ("Can you help me with pricing?", False),
        ("What languages do your services support?", False),
        ("What is your delivery latency?", False),
    ],
)
def test_conversation_control_grammar_does_not_bypass_factual_grounding(
    utterance,
    is_control,
):
    assert livekit_worker._is_conversation_control_utterance(utterance) is is_control


@pytest.mark.parametrize(
    ("partial", "meaningful"),
    [
        ("yes", False),
        ("okay", False),
        ("can", False),
        ("phone", True),
        ("can you", True),
        ("stop", True),
    ],
)
def test_single_pass_interruption_waits_for_meaningful_transcript(partial, meaningful):
    assert livekit_worker._is_meaningful_single_pass_interruption(partial) is meaningful


def test_passive_single_pass_backchannel_is_consumed_before_fragment_cancellation():
    controller = SimpleNamespace(on_suppressed_final_transcript=Mock())
    runtime_agent = SimpleNamespace(should_expand_single_pass_backchannel=Mock(return_value=False))

    # "Okay" is deliberately a general incomplete-fragment candidate. The
    # single-pass gate consumes it first and never requests active cancellation.
    assert livekit_worker._is_incomplete_barge_in_fragment("Okay") is True
    assert (
        livekit_worker._consume_passive_single_pass_backchannel(
            transcript="Okay",
            runtime_agent=runtime_agent,
            controller=controller,
        )
        is True
    )
    controller.on_suppressed_final_transcript.assert_called_once_with(cancel_active=False)


def test_affirmative_answer_to_explicit_offer_reaches_single_pass_turn():
    controller = SimpleNamespace(on_suppressed_final_transcript=Mock())
    runtime_agent = SimpleNamespace(should_expand_single_pass_backchannel=Mock(return_value=True))

    assert (
        livekit_worker._consume_passive_single_pass_backchannel(
            transcript="Yes",
            runtime_agent=runtime_agent,
            controller=controller,
        )
        is False
    )
    controller.on_suppressed_final_transcript.assert_not_called()


def test_passive_backchannel_does_not_create_a_fake_turn_or_steal_grounding():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Who is the chairman?")
    telemetry.record_knowledge_lookup(elapsed_ms=10, result="no_match")
    original_trace = telemetry.current_turn_trace
    telemetry.on_agent_state(new_state="speaking")
    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="speaking")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="speaking")
    telemetry.on_final_transcript("Okay")

    controller = SimpleNamespace(on_suppressed_final_transcript=Mock())
    runtime_agent = SimpleNamespace(should_expand_single_pass_backchannel=Mock(return_value=False))
    assert livekit_worker._consume_passive_single_pass_backchannel(
        transcript="Okay",
        runtime_agent=runtime_agent,
        controller=controller,
        telemetry=telemetry,
    )

    assert telemetry.current_turn_trace is None
    assert runtime_metrics["turn_diagnostics"] == [original_trace]
    assert telemetry.pending_grounding_trace is original_trace
    telemetry.on_assistant_content("I couldn't verify that from the approved information.")
    assert original_trace["grounding_outcome"] == "no_match_correctly_refused"


def test_late_interrupted_assistant_item_cannot_consume_newer_grounding_verdict():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Where is branch B?")
    telemetry.record_knowledge_lookup(elapsed_ms=10, result="no_match")
    trace = telemetry.current_turn_trace

    telemetry.on_assistant_content(
        "The old answer is unsupported.",
        item_id="old-response",
        interrupted=True,
    )

    assert trace is not None
    assert "grounding_outcome" not in trace
    assert telemetry.pending_grounding_trace is trace
    telemetry.on_assistant_content(
        "I couldn't verify that from the approved information.",
        item_id="current-response",
    )
    assert trace["grounding_response_item_id"] == "current-response"
    assert trace["grounding_outcome"] == "no_match_correctly_refused"


def test_verified_retrieval_is_linked_at_first_audio_when_completion_event_is_late():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("When was the group established?")
    telemetry.record_knowledge_lookup(
        elapsed_ms=30,
        result="verified",
        details={
            "knowledge_retrieval_path": "exact_fact",
            "exact_fact_action": "answer",
        },
    )

    telemetry.on_agent_state(new_state="speaking")

    trace = runtime_metrics["turn_diagnostics"][0]
    assert trace["grounding_outcome"] == "response_after_verified_retrieval"
    assert trace["response_action"] == "response_started_after_verified_retrieval"
    assert trace["grounding_response_observation"] == "audio_started"
    grounding = summarize_runtime_grounding({"runtime": runtime_metrics})
    assert grounding["response_after_verified_retrieval"] == 1
    assert grounding["answered_without_grounding"] == 0


def test_provider_interrupted_flag_without_caller_barge_in_preserves_audible_grounding_link():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("When was the group established?")
    telemetry.record_knowledge_lookup(elapsed_ms=30, result="verified")
    telemetry.on_agent_state(new_state="speaking")
    trace = runtime_metrics["turn_diagnostics"][0]

    telemetry.on_assistant_content(
        "According to the approved source, the group was established in 2003.",
        interrupted=True,
    )

    assert trace["outcome"] == "answered"
    assert trace["grounding_outcome"] == "response_after_verified_retrieval"
    assert trace["response_action"] == "response_started_after_verified_retrieval"
    assert trace["grounding_response_observation"] == "audio_started"
    grounding = summarize_runtime_grounding({"runtime": runtime_metrics})
    assert grounding["response_after_verified_retrieval"] == 1
    assert grounding["answered_without_grounding"] == 0


def test_meaningful_barge_in_supersedes_old_trace_before_grounded_replacement():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Tell me about the company")
    telemetry.record_knowledge_lookup(elapsed_ms=10, result="verified")
    first_trace = telemetry.current_turn_trace
    telemetry.on_agent_state(new_state="speaking")
    assert first_trace is not None
    assert first_trace["outcome"] == "answered"

    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="speaking")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="listening")
    telemetry.on_final_transcript("Just give me the phone number")
    telemetry.commit_suspended_interruption()
    assert first_trace["outcome"] == "superseded_by_caller"

    telemetry.record_knowledge_lookup(elapsed_ms=8, result="verified")
    telemetry.on_agent_state(new_state="speaking")
    telemetry.on_assistant_content("The phone number is +971 2 665 9998.")

    grounding = summarize_runtime_grounding(
        {"runtime": {"turn_diagnostics": telemetry.turn_diagnostics}}
    )
    assert grounding["answered_without_grounding"] == 0
    assert grounding["response_after_verified_retrieval"] == 1


def test_barge_in_pause_resume_preserves_the_original_suspended_trace():
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics={"barge_in_count": 0},
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Tell me about the company")
    telemetry.record_knowledge_lookup(elapsed_ms=10, result="verified")
    original_trace = telemetry.current_turn_trace
    telemetry.on_agent_state(new_state="speaking")

    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="speaking")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="listening")
    # A VAD pause/resume occurs before one meaningful final transcript.
    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="listening")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="listening")
    telemetry.on_final_transcript("Just give me the phone number")
    telemetry.commit_suspended_interruption()

    assert original_trace is not None
    assert original_trace["outcome"] == "superseded_by_caller"
    assert (
        summarize_runtime_grounding({"runtime": {"turn_diagnostics": telemetry.turn_diagnostics}})[
            "answered_without_grounding"
        ]
        == 0
    )


def test_runtime_date_context_uses_agent_timezone_and_falls_back_safely():
    observed = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)

    assert _runtime_date_context("Asia/Dubai", now_utc=observed) == (
        "2026-09-04",
        "Asia/Dubai",
    )
    assert _runtime_date_context("not/a-timezone", now_utc=observed) == (
        "2026-09-03",
        "UTC",
    )


def test_inworld_recognition_terms_never_truncate_a_business_name():
    long_term = "A" * 1495
    selected = _inworld_recognition_terms(("Al Zaabi Group", long_term, "Saeed Al Zaabi"))

    assert selected == ("Al Zaabi Group",)
    assert len(", ".join(selected)) <= 1500


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("I cannot verify that from the approved information.", "no_match_correctly_refused"),
        ("Which location do you mean?", "no_match_clarification"),
        ("The chairman is the person you named.", "no_match_unverified_response"),
    ],
)
def test_no_match_response_outcomes_distinguish_safe_handling(content, expected):
    assert _no_match_response_outcome(content) == expected


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
    serving_revision_id = uuid4()
    knowledge_base_id = uuid4()
    agent = livekit_worker.VAVInworldAgent(
        model=model,
        knowledge_serving_revision_id=serving_revision_id,
        knowledge_base_id=knowledge_base_id,
    )
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
    assert retrieval.await_args.kwargs["serving_revision_id"] == serving_revision_id
    assert retrieval.await_args.kwargs["knowledge_base_id"] == knowledge_base_id
    assert "Verified group companies" in turn_ctx.messages()[-1].text_content


@pytest.mark.asyncio
async def test_native_realtime_agent_uses_tool_without_duplicate_context_injection(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Al Zaabi Group Receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    retrieval = AsyncMock(return_value="Source: Management\nInception year: 2003")
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    agent = livekit_worker.VAVInworldRealtimeAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    message = turn_ctx.add_message(role="user", content="When was this company formed?")

    await agent.on_user_turn_completed(turn_ctx, message)

    retrieval.assert_not_awaited()
    assert len(turn_ctx.messages()) == 1
    assert "alternative semantic query" in agent.instructions

    canary_agent = livekit_worker.VAVInworldRealtimeAgent(
        model=model,
        single_pass=True,
    )
    assert "automatically added to the current turn" in canary_agent.instructions
    assert "call\n  `search_approved_knowledge` before answering" not in (canary_agent.instructions)


@pytest.mark.asyncio
async def test_native_realtime_tool_passes_semantic_terms_to_shared_retrieval(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Al Zaabi Group Receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    agent = livekit_worker.VAVInworldRealtimeAgent(model=model)
    retrieval = AsyncMock(return_value="Source: Management\nInception year: 2003")
    monkeypatch.setattr(agent, "_retrieve_approved_knowledge", retrieval)

    result = await agent.search_approved_knowledge(
        "When was Al Zaabi Group formed?",
        "What is the Al Zaabi Group inception year?",
    )

    assert result.endswith("2003")
    retrieval.assert_awaited_once_with(
        query="When was Al Zaabi Group formed?",
        query_variants=("What is the Al Zaabi Group inception year?",),
    )


@pytest.mark.asyncio
async def test_single_pass_backchannel_expands_only_after_an_explicit_offer(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Al Zaabi Group Receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    agent = livekit_worker.VAVInworldRealtimeAgent(model=model, single_pass=True)
    retrieval = AsyncMock(return_value="Approved evidence")
    monkeypatch.setattr(agent, "_retrieve_approved_knowledge", retrieval)

    assert (
        await agent.retrieve_single_pass_evidence("Where are you located?") == "Approved evidence"
    )
    agent.observe_single_pass_assistant_content("We are on Example Road.")
    assert await agent.retrieve_single_pass_evidence("Okay") == (
        livekit_worker.NO_KNOWLEDGE_REQUIRED
    )
    assert retrieval.await_count == 1

    assert await agent.retrieve_single_pass_evidence("What are your hours?") == (
        "Approved evidence"
    )
    assert retrieval.await_count == 2
    assert retrieval.await_args.kwargs["query"] == "What are your hours?"

    agent.observe_single_pass_assistant_content("Would you like me to give you the weekend hours?")
    assert await agent.retrieve_single_pass_evidence("Yes") == "Approved evidence"
    assert retrieval.await_count == 3
    assert retrieval.await_args.kwargs["query"] == "What are your hours. Yes"


@pytest.mark.asyncio
async def test_single_pass_keeps_governed_subject_for_unqualified_followups(monkeypatch):
    model = Agent(
        tenant_id=uuid4(),
        name="Reusable production QA agent",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    agent = livekit_worker.VAVInworldRealtimeAgent(model=model, single_pass=True)
    agent._single_pass_active_subject = "Future Example Holdings"
    retrieval = AsyncMock(return_value="Approved evidence")
    monkeypatch.setattr(agent, "_retrieve_approved_knowledge", retrieval)

    await agent.retrieve_single_pass_evidence("What about the chairman?")

    retrieval.assert_awaited_once_with(
        query="What about the chairman?",
        query_variants=("Future Example Holdings. What about the chairman?",),
    )


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
async def test_livekit_agent_uses_cached_knowledge_terminology_and_conversation_variants(
    monkeypatch,
):
    model = Agent(
        tenant_id=uuid4(),
        name="Cosmetic centre receptionist",
        system_prompt="Answer from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
    )
    terminology = AsyncMock(return_value=("Chemical Peeling", "Platelet Rich Plasma"))
    retrieval = AsyncMock(return_value="Source: Treatments\nVerified treatment information.")
    monkeypatch.setattr(livekit_worker, "load_agent_knowledge_terminology", terminology)
    monkeypatch.setattr(livekit_worker, "retrieve_knowledge_context", retrieval)
    monkeypatch.setattr(livekit_worker, "async_session_factory", session_factory)
    agent = livekit_worker.VAVInworldAgent(model=model)
    turn_ctx = livekit_worker.llm.ChatContext.empty()
    turn_ctx.add_message(role="user", content="Tell me about facial treatments.")
    turn_ctx.add_message(role="assistant", content="Which treatment would you like?")
    follow_up = turn_ctx.add_message(role="user", content="What about chemical feeling?")

    await agent.on_user_turn_completed(turn_ctx, follow_up)
    await agent.on_user_turn_completed(turn_ctx, follow_up)

    terminology.assert_awaited_once()
    assert retrieval.await_count == 2
    assert retrieval.await_args.kwargs["terminology"] == (
        "Chemical Peeling",
        "Platelet Rich Plasma",
    )
    assert (
        "Tell me about facial treatments. What about chemical feeling?"
        in (retrieval.await_args.kwargs["query_variants"])
    )


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
            SimpleNamespace(
                type="llm_usage",
                input_tokens=1200,
                output_tokens=300,
                input_audio_tokens=700,
                output_audio_tokens=120,
                input_text_tokens=500,
                output_text_tokens=180,
                session_duration=42.5,
            ),
            SimpleNamespace(type="tts_usage", characters_count=640, audio_duration=9.25),
            SimpleNamespace(type="stt_usage", audio_duration=42.5),
        ]
    )

    assert _usage_snapshot(usage) == {
        "llm_input_tokens": 1200,
        "llm_output_tokens": 300,
        "llm_input_audio_tokens": 700,
        "llm_output_audio_tokens": 120,
        "llm_input_text_tokens": 500,
        "llm_output_text_tokens": 180,
        "realtime_session_seconds": 42.5,
        "tts_characters": 640,
        "tts_audio_seconds": 9.25,
        "stt_audio_seconds": 42.5,
        "llm_tokens": 1500,
        "usage_source": "livekit_session_usage",
        "runtime_usage_components_complete": True,
        "usage_components_expected": ["llm", "tts", "stt"],
        "usage_components_reported": ["llm", "stt", "tts"],
    }


def test_livekit_usage_snapshot_preserves_unreported_usage_as_unknown():
    assert _usage_snapshot(SimpleNamespace(model_usage=[])) == {
        "llm_input_tokens": None,
        "llm_output_tokens": None,
        "llm_input_audio_tokens": None,
        "llm_output_audio_tokens": None,
        "llm_input_text_tokens": None,
        "llm_output_text_tokens": None,
        "realtime_session_seconds": None,
        "tts_characters": None,
        "tts_audio_seconds": None,
        "stt_audio_seconds": None,
        "llm_tokens": None,
        "usage_source": "livekit_session_usage",
        "runtime_usage_components_complete": False,
        "usage_components_expected": ["llm", "tts", "stt"],
        "usage_components_reported": [],
    }


def test_native_usage_completeness_means_expected_metric_presence_not_billing():
    usage = SimpleNamespace(model_usage=[SimpleNamespace(type="llm_usage", session_duration=12.5)])

    snapshot = _usage_snapshot(usage, expected_components=("llm",))

    assert snapshot["usage_components_expected"] == ["llm"]
    assert snapshot["usage_components_reported"] == ["llm"]
    assert snapshot["runtime_usage_components_complete"] is True
    assert snapshot["llm_tokens"] is None
    assert snapshot["tts_audio_seconds"] is None
    assert snapshot["stt_audio_seconds"] is None


def test_native_external_tts_is_a_separate_observed_component():
    usage = SimpleNamespace(model_usage=[SimpleNamespace(type="llm_usage", session_duration=12.5)])
    snapshot = _usage_snapshot(usage, expected_components=("llm",))

    _record_external_tts_request(
        snapshot,
        "The verified phone number is +971 2 665 9998.",
        "single_pass_deterministic",
    )

    assert snapshot["usage_components_expected"] == ["external_tts", "llm"]
    assert snapshot["usage_components_reported"] == ["external_tts", "llm"]
    assert snapshot["runtime_usage_components_complete"] is True
    assert snapshot["external_tts_request_count"] == 1
    assert snapshot["external_tts_characters"] == 45
    assert snapshot["external_tts_provider_reconciliation_required"] is True


def test_external_tts_cannot_make_missing_native_llm_usage_look_complete():
    snapshot = _usage_snapshot(SimpleNamespace(model_usage=[]), expected_components=("llm",))

    _record_external_tts_request(snapshot, "Welcome", "greeting_preparation")

    assert snapshot["usage_components_expected"] == ["external_tts", "llm"]
    assert snapshot["usage_components_reported"] == ["external_tts"]
    assert snapshot["runtime_usage_components_complete"] is False


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


def test_multilingual_auto_stt_does_not_pin_provider_to_primary_language():
    model = SimpleNamespace(
        language="en-GB",
        supported_languages=["en-GB", "ar-AE", "hi-IN"],
        language_switching_enabled=True,
    )
    profile = SimpleNamespace(stt_language="auto", runtime_config=None)

    assert livekit_worker._effective_stt_language(model=model, profile=profile) == "auto"
    assert _inworld_stt_model(model=model, profile=profile) == "soniox/stt-rt-v4"


def test_explicit_english_stt_keeps_wrong_script_guard_narrow_when_agent_is_multilingual():
    model = SimpleNamespace(
        language="en-GB",
        supported_languages=["en-GB", "ar-AE", "hi-IN"],
        language_switching_enabled=True,
    )
    profile = SimpleNamespace(stt_language="en", runtime_config=None)

    allowed = livekit_worker.resolved_stt_script_languages(model=model, profile=profile)
    assessment = livekit_worker.detect_unexpected_script(
        "The chairman is सईद अल ज़ाबी",
        expected_language="en",
        allowed_languages=allowed,
    )

    assert allowed == ("en",)
    assert assessment.is_unexpected is True
    assert "DEVANAGARI" in assessment.unexpected_scripts


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


@pytest.mark.asyncio
async def test_native_inworld_realtime_model_uses_one_grounded_speech_session(monkeypatch):
    async def no_network_main(_session):
        return None

    monkeypatch.setattr(
        inworld_realtime_adapter.InworldRealtimeSession,
        "_main_task",
        no_network_main,
    )
    model = SimpleNamespace(
        name="Al Zaabi Group Support",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
        speech_rate=0.95,
    )
    profile = SimpleNamespace(
        stt_language="en-GB",
        llm_model="openai/gpt-4o-mini",
        runtime_config={"voice_runtime": "inworld_realtime", "stt_model": "auto"},
    )

    wire_telemetry = {}
    realtime = _build_inworld_realtime_model(
        model=model,
        profile=profile,
        api_key="inworld-key",
        terminology=("Al Zaabi Group", "Saeed Al Zaabi Tire Factory"),
        wire_telemetry=wire_telemetry,
    )

    assert _inworld_voice_runtime(profile) == "inworld_realtime"
    assert realtime.provider == "inworld"
    assert realtime.model == "openai/gpt-4o-mini"
    assert realtime._opts.voice == "Ashley"
    assert realtime._opts.speed == 0.95
    assert realtime._opts.input_audio_transcription.model == "assemblyai/u3-rt-pro"
    assert "Al Zaabi Group Support" in realtime._opts.input_audio_transcription.prompt
    assert "Saeed Al Zaabi Tire Factory" in realtime._opts.input_audio_transcription.prompt
    assert realtime._opts.turn_detection.type == "semantic_vad"
    assert realtime._opts.turn_detection.create_response is True
    assert realtime._opts.turn_detection.interrupt_response is True
    assert realtime.output_tts_model == "inworld-tts-1.5-max"

    session = realtime.session()
    serialized_before_explicit_update = int(
        wire_telemetry.get("stt_session_update_serialized_sequence") or 0
    )
    update = session._create_session_update_event()
    transcription = update["session"]["audio"]["input"]["transcription"]
    assert update["session"]["audio"]["output"]["model"] == "inworld-tts-1.5-max"
    assert wire_telemetry["realtime_tts_session_update_serialized_model"] == ("inworld-tts-1.5-max")
    assert transcription["model"] == "assemblyai/u3-rt-pro"
    assert transcription["language"] == "en-GB"
    assert wire_telemetry["stt_session_update_serialized_model"] == "assemblyai/u3-rt-pro"
    assert wire_telemetry["stt_session_update_serialized_language"] == "en-GB"
    assert wire_telemetry["stt_session_update_serialized_lexicon_count"] == 2
    assert wire_telemetry["stt_session_update_serialized_prompt_chars"] == len(
        transcription["prompt"]
    )
    assert wire_telemetry["stt_session_update_serialized_sequence"] == (
        serialized_before_explicit_update + 1
    )
    assert transcription["prompt"] not in repr(wire_telemetry)
    assert not any("hash" in key or "sha" in key for key in wire_telemetry)
    assert wire_telemetry["stt_session_update_provider_acknowledgement_observed"] is False
    assert wire_telemetry["stt_session_update_serialized_complete"] is True
    await session.aclose()

    canary = _build_inworld_realtime_model(
        model=model,
        profile=profile,
        api_key="inworld-key",
        terminology=("Al Zaabi Group",),
        wire_telemetry={},
        single_pass=True,
    )
    assert canary._opts.turn_detection.create_response is False
    assert canary._opts.turn_detection.interrupt_response is False
    assert canary.capabilities.turn_detection is False
    canary_session = canary.session()
    canary_update = canary_session._create_session_update_event()
    assert canary_update["session"]["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "medium",
        "create_response": False,
        "interrupt_response": False,
    }
    await canary_session.aclose()


def test_inworld_stt_serialization_diagnostics_reset_atomically_for_every_update():
    private_prompt = "Private company names and caller vocabulary"
    wire_telemetry = {
        "stt_session_update_serialized_model": "stale-model",
        "stt_session_update_serialized_language": "stale-language",
        "stt_session_update_serialized_prompt_chars": 999,
        "stt_session_update_serialized_lexicon_count": 999,
        "stt_session_update_serialized_complete": True,
        "stt_session_update_serialized_sequence": 8,
    }
    model = SimpleNamespace(
        _wire_telemetry=wire_telemetry,
        _recognition_lexicon_count=3,
    )
    session = object.__new__(inworld_realtime_adapter.InworldRealtimeSession)
    session._realtime_model = model

    session._record_wire_telemetry(
        {
            "session": {
                "audio": {
                    "input": {
                        "transcription": {
                            "model": "assemblyai/u3-rt-pro",
                            "language": "en-GB",
                            "prompt": private_prompt,
                        }
                    }
                }
            }
        }
    )
    assert wire_telemetry["stt_session_update_serialized_sequence"] == 9
    assert wire_telemetry["stt_session_update_serialized_prompt_chars"] == len(private_prompt)
    assert private_prompt not in repr(wire_telemetry)
    assert not any("hash" in key or "sha" in key for key in wire_telemetry)

    # A later update with no transcription block must clear the previous
    # observation. Carrying it forward would falsely describe the new payload.
    session._record_wire_telemetry({"session": {"audio": {"input": {}}}})
    assert wire_telemetry["stt_session_update_serialized_sequence"] == 10
    assert wire_telemetry["stt_session_update_serialized_model"] is None
    assert wire_telemetry["stt_session_update_serialized_language"] is None
    assert wire_telemetry["stt_session_update_serialized_prompt_chars"] is None
    assert wire_telemetry["stt_session_update_serialized_lexicon_count"] is None
    assert wire_telemetry["stt_session_update_serialized_complete"] is False
    assert wire_telemetry["stt_session_update_provider_acknowledgement_observed"] is False


def test_legacy_runtime_without_voice_mode_stays_on_pipeline():
    assert _inworld_voice_runtime(SimpleNamespace()) == "pipeline"


@pytest.mark.asyncio
async def test_inworld_realtime_adapter_uses_inworld_endpoint_and_basic_auth(monkeypatch):
    captured = {}

    class WebSocket:
        async def receive_json(self):
            return {"type": "session.created", "session": {"id": "provider-session"}}

        async def close(self):
            captured["closed"] = True

    class HttpSession:
        async def ws_connect(self, **kwargs):
            captured.update(kwargs)
            return WebSocket()

    model = SimpleNamespace(_ensure_http_session=lambda: HttpSession())
    session = object.__new__(inworld_realtime_adapter.InworldRealtimeSession)
    session._realtime_model = model
    session._opts = SimpleNamespace(
        base_url="https://api.inworld.ai",
        api_key="base64-inworld-key",
        conn_options=SimpleNamespace(timeout=2.0),
    )
    session._report_connection_acquired = lambda duration: captured.update(duration=duration)
    monkeypatch.setattr(
        inworld_realtime_adapter,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-session-id"),
    )

    websocket = await session._create_ws_conn()

    assert isinstance(websocket, WebSocket)
    assert captured["url"] == (
        "wss://api.inworld.ai/api/v1/realtime/session?key=vav-fixed-session-id&protocol=realtime"
    )
    assert captured["headers"]["Authorization"] == "Basic base64-inworld-key"
    assert captured["duration"] >= 0


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
        metrics={
            "end_of_turn_delay": 0.15,
            "transcription_delay": 0.08,
            "on_user_turn_completed_delay": 0.04,
        },
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
    )

    assert runtime_metrics == {
        "turn_count": 2,
        "last_llm_first_token_ms": 200,
        "last_tts_first_byte_ms": 300,
        "last_speech_end_to_first_audio_ms": 1200,
        "last_end_of_utterance_ms": 150,
        "last_transcription_delay_ms": 80,
        "last_knowledge_hook_ms": 40,
        "turn_latency_sample_count": 2,
        "turn_latency_p50_ms": 910,
        "turn_latency_p90_ms": 1200,
        "turn_latency_p95_ms": 1200,
    }


def test_livekit_native_chat_metrics_keep_supported_ttft_without_duplicate_e2e():
    runtime_metrics = {"turn_count": 0}
    samples = [875]

    _capture_turn_latency(
        role="assistant",
        metrics={"llm_node_ttft": 0.24, "e2e_latency": 0.91},
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
        include_end_to_end=False,
    )

    assert runtime_metrics == {
        "turn_count": 1,
        "last_llm_first_token_ms": 240,
    }
    assert samples == [875]


def test_pipeline_server_speaking_event_does_not_duplicate_chat_e2e_sample(monkeypatch):
    timestamps = iter([10.0, 10.2])
    monkeypatch.setattr(livekit_worker.time, "monotonic", lambda: next(timestamps))
    runtime_metrics = {"barge_in_count": 0}
    samples = [910]
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
        opened_at=9.0,
    )

    telemetry.on_user_state(
        old_state="speaking",
        new_state="listening",
        agent_state="listening",
    )
    telemetry.on_agent_state(new_state="speaking", capture_end_to_end=False)

    assert samples == [910]
    assert "last_speech_end_to_first_audio_ms" not in runtime_metrics
    assert telemetry.last_user_speech_end_at is None


def test_livekit_native_events_capture_exact_turn_and_interruption_metrics(monkeypatch):
    timestamps = iter([10.0, 10.2, 10.7, 11.0, 12.0, 12.4])
    monkeypatch.setattr(livekit_worker.time, "monotonic", lambda: next(timestamps))
    runtime_metrics = {"barge_in_count": 0}
    samples: list[int] = []
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=samples,
        opened_at=9.5,
    )

    telemetry.mark_session_started()
    telemetry.on_agent_state(new_state="speaking")
    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="speaking")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="listening")
    telemetry.on_final_transcript()
    telemetry.on_agent_state(new_state="speaking")
    telemetry.on_metrics(
        SimpleNamespace(
            type="realtime_model_metrics",
            ttft=0.31,
        )
    )
    telemetry.on_metrics(
        SimpleNamespace(
            type="eou_metrics",
            end_of_utterance_delay=0.22,
            transcription_delay=0.14,
            on_user_turn_completed_delay=0.08,
        )
    )
    telemetry.on_metrics(
        SimpleNamespace(
            type="interruption_metrics",
            num_interruptions=2,
            detection_delay=0.18,
        )
    )

    assert runtime_metrics == {
        "barge_in_count": 2,
        "turn_diagnostics": [
            {
                "turn": 1,
                "user_speech_ms": 300,
                "barge_in": True,
                "transcript_words": 0,
                "transcript_after_speech_ms": 1000,
                "transcript_to_first_audio_ms": 400,
                "speech_end_to_first_audio_ms": 1400,
                "outcome": "answered",
                "llm_first_token_ms": 310,
                "end_of_utterance_ms": 220,
                "transcription_delay_ms": 140,
                "knowledge_hook_ms": 80,
                "interruption_detection_ms": 180,
            }
        ],
        "call_open_to_greeting_ms": 700,
        "session_start_to_greeting_ms": 200,
        "last_transcript_to_first_audio_ms": 400,
        "last_speech_end_to_first_audio_ms": 1400,
        "turn_latency_sample_count": 1,
        "turn_latency_p50_ms": 1400,
        "turn_latency_p90_ms": 1400,
        "turn_latency_p95_ms": 1400,
        "last_llm_first_token_ms": 310,
        "last_end_of_utterance_ms": 220,
        "last_transcription_delay_ms": 140,
        "last_knowledge_hook_ms": 80,
        "last_interruption_detection_ms": 180,
    }


def test_launch_latency_includes_work_before_runtime_admission(monkeypatch):
    timestamps = iter([12.0, 12.3, 12.7])
    monkeypatch.setattr(livekit_worker.time, "monotonic", lambda: next(timestamps))
    runtime_metrics: dict[str, object] = {}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=11.5,
        worker_job_started_at=8.0,
        participant_active_at=9.0,
    )

    telemetry.mark_session_started()
    telemetry.mark_session_ready()
    telemetry.on_agent_state(new_state="speaking")

    assert runtime_metrics["session_connection_ms"] == 300
    assert runtime_metrics["worker_job_entry_to_session_ready_ms"] == 4300
    assert runtime_metrics["participant_active_to_session_ready_ms"] == 3300
    assert runtime_metrics["worker_job_entry_to_first_server_speaking_ms"] == 4700
    assert runtime_metrics["participant_active_to_first_server_speaking_ms"] == 3700
    assert runtime_metrics["call_open_to_greeting_ms"] == 1200
    assert runtime_metrics["session_start_to_greeting_ms"] == 700


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("See.", True),
        ("Hop.", True),
        ("Thank you.", True),
        ("What is", True),
        ("Tell me the", True),
        ("Phone", False),
        ("Repeat", False),
        ("Why?", False),
        ("What is the group's phone number?", False),
        ("Stop. Just give me the mobile number.", False),
    ],
)
def test_adaptive_barge_in_fragment_detection(transcript, expected):
    assert livekit_worker._is_incomplete_barge_in_fragment(transcript) is expected


def test_adaptive_barge_in_accepts_single_word_from_bound_knowledge():
    assert (
        livekit_worker._is_incomplete_barge_in_fragment(
            "Healthcare",
            terminology=("Healthcare", "Al Zaabi Group"),
        )
        is False
    )


def test_livekit_native_fragment_is_consumed_once_and_recorded(monkeypatch):
    timestamps = iter([20.0])
    monkeypatch.setattr(livekit_worker.time, "monotonic", lambda: next(timestamps))
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=19.0,
    )

    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="thinking")
    assert telemetry.consume_barge_in_transcript() is True
    assert telemetry.consume_barge_in_transcript() is False
    telemetry.record_suppressed_fragment("See.")

    assert runtime_metrics == {
        "barge_in_count": 1,
        "turn_diagnostics": [
            {
                "turn": 1,
                "transcript_words": 1,
                "stabilization_ms": 500,
                "outcome": "fragment_suppressed",
            }
        ],
        "suppressed_fragment_count": 1,
        "last_suppressed_fragment_words": 1,
        "fragment_continuation_window_ms": 500,
    }


def test_livekit_single_pass_stage_timings_are_attached_to_the_exact_turn():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("What is the phone number?")
    telemetry.mark_single_pass_turn(7)

    telemetry.record_single_pass_timing(
        livekit_worker.SinglePassTurnTiming(
            sequence=7,
            outcome=SinglePassTurnOutcome.COMPLETED,
            transcript_chars=25,
            evidence_chars=180,
            retrieval_ms=12.5,
            generation_dispatch_ms=1.5,
            generation_ms=240.0,
            total_ms=252.5,
        )
    )

    assert runtime_metrics["single_pass_turn_count"] == 1
    assert runtime_metrics["last_single_pass_retrieval_ms"] == 12.5
    assert runtime_metrics["last_single_pass_generation_dispatch_ms"] == 1.5
    assert runtime_metrics["last_single_pass_generation_ms"] == 240.0
    assert runtime_metrics["last_single_pass_total_ms"] == 252.5
    assert runtime_metrics["last_single_pass_outcome"] == "completed"
    trace = telemetry.current_turn_trace
    assert trace is not None
    assert trace["inworld_turn_mode"] == "single_pass_experimental"
    assert trace["single_pass_sequence"] == 7
    assert trace["single_pass_outcome"] == "completed"
    assert trace["single_pass_evidence_chars"] == 180


def test_livekit_grounding_telemetry_links_tool_result_to_spoken_answer():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Who is the chairman?")
    telemetry.record_knowledge_lookup(
        elapsed_ms=43,
        result="no_match",
        query_variant_count=2,
        fallback_used=True,
    )
    telemetry.on_agent_state(new_state="speaking")
    telemetry.on_final_transcript("A newer caller turn")
    telemetry.on_assistant_content("The chairman is the person you named.")

    assert runtime_metrics["knowledge_lookup_count"] == 1
    assert runtime_metrics["knowledge_no_match_count"] == 1
    assert runtime_metrics["unsupported_knowledge_response_count"] == 1
    trace = runtime_metrics["turn_diagnostics"][0]
    assert trace["tool_call"] is True
    assert trace["knowledge_tool_ms"] == 43
    assert trace["knowledge_result"] == "no_match"
    assert trace["retrieval_result"] == "no_match"
    assert trace["knowledge_query_variant_count"] == 2
    assert trace["knowledge_fallback_used"] is True
    assert trace["grounding_outcome"] == "no_match_unverified_response"
    assert trace["response_action"] == "answered_without_verified_evidence"
    assert "grounding_outcome" not in telemetry.current_turn_trace


def test_late_knowledge_result_stays_on_originating_turn_and_cannot_ground_next_turn():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("What is the phone number?")
    originating_trace = telemetry.begin_knowledge_lookup()
    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="thinking")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="listening")
    telemetry.on_final_transcript("Where are you located?")
    next_trace = telemetry.current_turn_trace

    telemetry.record_knowledge_lookup(
        elapsed_ms=900,
        result="verified",
        evidence_chars=50,
        originating_trace=originating_trace,
    )
    telemetry.on_assistant_content("The location is Example Road.")

    assert originating_trace["knowledge_result"] == "verified"
    assert originating_trace["knowledge_result_late"] is True
    assert runtime_metrics["late_knowledge_result_count"] == 1
    assert next_trace is telemetry.current_turn_trace
    assert "knowledge_result" not in next_trace
    assert "grounding_outcome" not in next_trace


def test_late_knowledge_completion_cannot_overwrite_newer_last_metrics():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("First question")
    first_trace = telemetry.begin_knowledge_lookup()
    telemetry.on_user_state(old_state="listening", new_state="speaking", agent_state="thinking")
    telemetry.on_user_state(old_state="speaking", new_state="listening", agent_state="listening")
    telemetry.on_final_transcript("Second question")
    second_trace = telemetry.begin_knowledge_lookup()

    telemetry.record_knowledge_lookup(
        elapsed_ms=20,
        result="verified",
        details={"exact_fact_total_ms": 19.5},
        originating_trace=second_trace,
    )
    telemetry.record_knowledge_lookup(
        elapsed_ms=900,
        result="no_match",
        details={"exact_fact_total_ms": 899.5},
        originating_trace=first_trace,
    )

    assert runtime_metrics["knowledge_lookup_count"] == 2
    assert runtime_metrics["last_knowledge_tool_ms"] == 20
    assert runtime_metrics["last_exact_fact_total_ms"] == 19.5
    assert first_trace["knowledge_tool_ms"] == 900
    assert second_trace["knowledge_tool_ms"] == 20


def test_late_single_pass_completion_cannot_overwrite_newer_last_metrics():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("First question")
    telemetry.mark_single_pass_turn(1)
    telemetry._finish_trace("superseded_by_caller")
    telemetry.on_final_transcript("Second question")
    telemetry.mark_single_pass_turn(2)

    telemetry.record_single_pass_timing(
        livekit_worker.SinglePassTurnTiming(
            sequence=2,
            outcome=SinglePassTurnOutcome.COMPLETED,
            transcript_chars=15,
            evidence_chars=50,
            retrieval_ms=10,
            generation_dispatch_ms=2,
            generation_ms=100,
            total_ms=112,
        )
    )
    telemetry.record_single_pass_timing(
        livekit_worker.SinglePassTurnTiming(
            sequence=1,
            outcome=SinglePassTurnOutcome.CANCELLED,
            transcript_chars=14,
            evidence_chars=40,
            retrieval_ms=800,
            generation_dispatch_ms=3,
            generation_ms=0,
            total_ms=803,
        )
    )

    assert runtime_metrics["single_pass_turn_count"] == 2
    assert runtime_metrics["single_pass_cancelled_count"] == 1
    assert runtime_metrics["last_single_pass_outcome"] == "completed"
    assert runtime_metrics["last_single_pass_total_ms"] == 112
    assert telemetry.turn_diagnostics[0]["single_pass_total_ms"] == 803
    assert telemetry.current_turn_trace is not None
    assert telemetry.current_turn_trace["single_pass_total_ms"] == 112


def test_deterministic_no_match_contraction_is_classified_as_safe_refusal():
    from app.livekit_runtime.inworld_single_pass import (
        NO_VERIFIED_KNOWLEDGE_MATCH,
        deterministic_grounded_reply,
    )

    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Who is the chairman?")
    telemetry.record_knowledge_lookup(elapsed_ms=10, result="no_match")
    reply = deterministic_grounded_reply(NO_VERIFIED_KNOWLEDGE_MATCH)
    assert reply is not None

    telemetry.on_assistant_content(reply)

    trace = telemetry.current_turn_trace
    assert trace is not None
    assert trace["grounding_outcome"] == "no_match_correctly_refused"
    assert trace["response_action"] == "refused_unverified"
    assert runtime_metrics.get("unsupported_knowledge_response_count", 0) == 0


def test_grounded_fact_with_routine_follow_up_is_not_misclassified_as_clarification():
    runtime_metrics = {"barge_in_count": 0}
    telemetry = _LiveKitRuntimeTelemetry(
        runtime_metrics=runtime_metrics,
        end_to_end_samples=[],
        opened_at=1.0,
    )
    telemetry.on_final_transcript("Who is the chairman?")
    telemetry.record_knowledge_lookup(
        elapsed_ms=10,
        result="verified",
        details={"exact_fact_action": "answer"},
    )

    telemetry.on_assistant_content(
        "The chairman is Saeed Yousif Ibrahim Al Zaabi. Is there anything else?"
    )

    trace = telemetry.current_turn_trace
    assert trace is not None
    assert trace["grounding_outcome"] == "response_after_verified_retrieval"
    assert trace["response_action"] == "responded_after_verified_retrieval"
    grounding = summarize_runtime_grounding({"runtime": runtime_metrics})
    assert grounding["clarified_despite_verified_exact_fact"] == 0


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
