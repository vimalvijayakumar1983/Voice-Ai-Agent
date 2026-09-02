"""LiveKit SIP transport tests."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from livekit import api

from app.livekit_runtime import worker as livekit_worker
from app.livekit_runtime.worker import _usage_snapshot, _worker_http_port, _worker_idle_processes
from app.telephony.livekit_provider import LiveKitSIPError, LiveKitSIPProvider


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
async def test_worker_failure_after_call_persistence_finalizes_once_and_keeps_auto_stt(
    monkeypatch,
):
    agent_id = uuid4()
    call_id = uuid4()
    shutdown_callbacks = []
    stt_options = {}
    model = SimpleNamespace(
        id=agent_id,
        tenant_id=uuid4(),
        voice_id="inworld:Ashley",
        language="en-GB",
        speech_rate=1.0,
        system_prompt="Answer with approved knowledge.",
        greeting_message="Hello",
        max_call_duration_seconds=60,
    )
    profile = SimpleNamespace(stt_language="auto", llm_model="openai/gpt-4o-mini")

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
        return model, profile, "inworld-key"

    def stt(**kwargs):
        stt_options.update(kwargs)
        return object()

    finalize = AsyncMock()
    monkeypatch.setattr(livekit_worker, "_load_runtime", load_runtime)
    monkeypatch.setattr(livekit_worker, "_open_call", AsyncMock(return_value=call_id))
    monkeypatch.setattr(livekit_worker, "_finish_call", finalize)
    monkeypatch.setattr(livekit_worker.inworld, "STT", stt)
    monkeypatch.setattr(livekit_worker.inworld, "TTS", lambda **_kwargs: object())
    monkeypatch.setattr(livekit_worker.openai, "LLM", lambda **_kwargs: object())
    monkeypatch.setattr(
        livekit_worker,
        "AgentSession",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("session construction failed")),
    )

    with pytest.raises(RuntimeError, match="session construction failed"):
        await livekit_worker.vav_inworld_session(Context())

    assert "language" not in stt_options
    assert len(shutdown_callbacks) == 1
    assert finalize.await_count == 1
    assert isinstance(finalize.await_args.kwargs["failure"], RuntimeError)
    await shutdown_callbacks[0]()
    assert finalize.await_count == 1


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
