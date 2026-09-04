import json
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.services import audio_replay_canary
from app.services.audio_replay_canary import (
    AudioReplayManifest,
    LiveKitPublishReceipt,
    ReplayExecutionError,
    ReplayManifestError,
    _AgentResponseQuiescence,
    evaluate_audio_replay,
    load_audio_fixture,
    load_audio_replay_manifest,
    manifest_environment_names,
    resolve_replay_target,
    run_live_audio_replay,
    word_error_rate,
)
from app.services.call_metadata import public_transport_identity_ref


def _manifest_payload(*, mode: str = "browser_session") -> dict:
    target = {
        "mode": mode,
        "api_base_url_env": "TEST_VAV_API_URL",
        "api_token_env": "TEST_VAV_API_TOKEN",
        "agent_id_env": "TEST_QA_AGENT_ID",
        "variables": {"qa_replay": "fixture-v1"},
    }
    safety = {
        "test_only": True,
        "allowlisted_agent_ids_env": "TEST_ALLOWED_AGENT_IDS",
        "required_agent_name_markers": ["qa", "canary"],
        "max_fixture_duration_seconds": 5,
        "max_fixture_bytes": 100_000,
    }
    if mode == "sip_fixture":
        target.update(
            {
                "livekit_url_env": "TEST_LIVEKIT_URL",
                "livekit_access_token_env": "TEST_LIVEKIT_TOKEN",
                "room_name_env": "TEST_LIVEKIT_ROOM",
                "call_id_env": "TEST_CALL_ID",
                "sip_fixture_id_env": "TEST_SIP_FIXTURE_ID",
            }
        )
        safety["allowlisted_sip_fixture_ids_env"] = "TEST_ALLOWED_SIP_FIXTURE_IDS"
    return {
        "schema_version": "vav-audio-replay-canary-v1",
        "case_id": "proper-name-canary",
        "description": "Synthetic proper-name QA case.",
        "target": target,
        "audio": {
            "path_env": "TEST_AUDIO_PATH",
            "format": "pcm_s16le",
            "sample_rate_hz": 16_000,
            "channels": 1,
            "frame_ms": 20,
        },
        "expectations": {
            "terminal_status": "completed",
            "transcript": {
                "utterances": [
                    {
                        "reference_text": "When was Al Zaabi Group established?",
                        "max_word_error_rate": 0.15,
                        "required_phrases": ["when was"],
                        "required_entities": ["Al Zaabi Group"],
                    }
                ],
                "max_extra_user_turns": 0,
            },
            "latency": {
                "max_participant_active_to_first_server_speaking_ms": 1_200,
                "max_turn_latency_p50_ms": 1_200,
                "max_turn_latency_p90_ms": 1_800,
                "max_turn_latency_p95_ms": 2_000,
            },
            "grounding": {
                "require_turn_diagnostics": True,
                "min_responses_after_verified_retrieval": 1,
                "max_unverified_answers": 0,
                "max_knowledge_errors": 0,
                "max_unexpected_script_turns": 0,
                "max_transcription_clarifications": 0,
            },
        },
        "timing": {
            "wait_for_remote_seconds": 1,
            "pre_roll_seconds": 0,
            "post_roll_seconds": 0.1,
            "finalization_timeout_seconds": 5,
            "poll_interval_seconds": 0.1,
        },
        "safety": safety,
    }


def _manifest(*, mode: str = "browser_session") -> AudioReplayManifest:
    return AudioReplayManifest.model_validate(_manifest_payload(mode=mode))


def _environment(tmp_path: Path, *, mode: str = "browser_session") -> tuple[dict[str, str], UUID]:
    agent_id = uuid4()
    audio_path = tmp_path / "caller.pcm"
    audio_path.write_bytes(b"\x00\x00" * 1_600)
    environ = {
        "TEST_VAV_API_URL": "https://qa.example.test",
        "TEST_VAV_API_TOKEN": "private-vav-token",
        "TEST_QA_AGENT_ID": str(agent_id),
        "TEST_ALLOWED_AGENT_IDS": str(agent_id),
        "TEST_AUDIO_PATH": str(audio_path),
    }
    if mode == "sip_fixture":
        environ.update(
            {
                "TEST_LIVEKIT_URL": "wss://qa.livekit.example.test",
                "TEST_LIVEKIT_TOKEN": "private-livekit-token",
                "TEST_LIVEKIT_ROOM": "qa-sip-room",
                "TEST_CALL_ID": str(uuid4()),
                "TEST_SIP_FIXTURE_ID": "qa-loopback-1",
                "TEST_ALLOWED_SIP_FIXTURE_IDS": "qa-loopback-1",
            }
        )
    return environ, agent_id


def _completed_call(call_id: UUID, agent_id: UUID) -> dict:
    return {
        "id": str(call_id),
        "agent_id": str(agent_id),
        "provider": "livekit_webrtc",
        "provider_call_sid": f"vav-browser-{call_id}",
        "status": "completed",
        "call_metadata": {
            "livekit_room": f"vav-browser-{call_id}",
            "runtime": {
                "participant_active_to_first_server_speaking_ms": 800,
                "turn_latency_p50_ms": 900,
                "turn_latency_p90_ms": 1_200,
                "turn_latency_p95_ms": 1_400,
                "turn_diagnostics": [
                    {
                        "turn": 1,
                        "transcript_words": 6,
                        "speech_end_to_first_audio_ms": 850,
                        "grounding_outcome": "response_after_verified_retrieval",
                        "knowledge_result": "verified",
                        "response_action": "responded_after_verified_retrieval",
                        "private_transcript": "must-not-leak",
                    }
                ],
            },
        },
    }


def test_manifest_rejects_inline_credentials_and_unsafe_mode():
    payload = _manifest_payload()
    payload["target"]["api_token"] = "inline-secret"
    with pytest.raises(ValidationError):
        AudioReplayManifest.model_validate(payload)

    payload = _manifest_payload()
    payload["safety"]["test_only"] = False
    with pytest.raises(ValidationError):
        AudioReplayManifest.model_validate(payload)


def test_resolve_target_requires_explicit_agent_and_sip_fixture_allowlists(tmp_path):
    browser_manifest = _manifest()
    environ, _agent_id = _environment(tmp_path)
    environ["TEST_ALLOWED_AGENT_IDS"] = str(uuid4())
    with pytest.raises(ReplayManifestError, match="QA replay allowlist"):
        resolve_replay_target(browser_manifest, environ=environ)

    sip_manifest = _manifest(mode="sip_fixture")
    environ, _agent_id = _environment(tmp_path, mode="sip_fixture")
    environ["TEST_ALLOWED_SIP_FIXTURE_IDS"] = "qa-other-fixture"
    with pytest.raises(ReplayManifestError, match="SIP fixture"):
        resolve_replay_target(sip_manifest, environ=environ)


def test_load_audio_fixture_supports_bounded_wav_and_pcm(tmp_path):
    manifest = _manifest()
    environ, _agent_id = _environment(tmp_path)
    pcm_fixture = load_audio_fixture(manifest, environ=environ)
    assert pcm_fixture.sample_rate_hz == 16_000
    assert pcm_fixture.channels == 1
    assert pcm_fixture.duration_seconds == pytest.approx(0.1)

    wav_path = tmp_path / "caller.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * 2_400)
    payload = manifest.model_dump(mode="json")
    payload["audio"] = {
        "path_env": "TEST_AUDIO_PATH",
        "format": "wav",
        "frame_ms": 20,
    }
    wav_manifest = AudioReplayManifest.model_validate(payload)
    environ["TEST_AUDIO_PATH"] = str(wav_path)
    wav_fixture = load_audio_fixture(wav_manifest, environ=environ)
    assert wav_fixture.sample_rate_hz == 24_000
    assert wav_fixture.duration_seconds == pytest.approx(0.1)


def test_word_error_rate_is_case_and_punctuation_insensitive():
    assert word_error_rate("Al Zaabi Group?", "al zaabi group") == 0
    assert word_error_rate("Al Zaabi Group", "Al Big Group") == pytest.approx(1 / 3)


def _remote_participant(
    identity: str,
    *,
    agent: bool = True,
    state: str | None = None,
) -> SimpleNamespace:
    attributes = {} if state is None else {"lk.agent.state": state}
    return SimpleNamespace(
        identity=identity,
        kind=(
            audio_replay_canary.rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            if agent
            else audio_replay_canary.rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
        ),
        attributes=attributes,
    )


@pytest.mark.asyncio
async def test_response_quiescence_waits_for_post_fixture_speaking_then_listening(monkeypatch):
    monkeypatch.setattr(audio_replay_canary, "_RESPONSE_QUIESCENCE_GRACE_SECONDS", 0)
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa-agent", state="listening")

    # Greeting state transitions happen before the caller fixture and must not
    # satisfy its response gate.
    observer.observe_participant(agent)
    observer.observe_attributes({"lk.agent.state": "speaking"}, agent)
    observer.observe_attributes({"lk.agent.state": "listening"}, agent)
    await observer.wait_until_listening(0.1)
    observer.arm([agent])
    assert not observer.response_started
    assert not observer.response_completed.is_set()

    observer.observe_transcription(
        [SimpleNamespace(final=True)],
        _remote_participant("caller", agent=False),
    )
    observer.observe_attributes({"lk.agent.state": "thinking"}, agent)
    observer.observe_attributes({"lk.agent.state": "speaking"}, agent)
    assert observer.response_started
    assert not observer.response_completed.is_set()
    observer.observe_attributes({"lk.agent.state": "listening"}, agent)

    await observer.wait(0.1)
    assert observer.response_completed.is_set()


@pytest.mark.asyncio
async def test_response_quiescence_preserves_response_completed_during_fixture(monkeypatch):
    monkeypatch.setattr(audio_replay_canary, "_RESPONSE_QUIESCENCE_GRACE_SECONDS", 0)
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa-agent", state="listening")

    observer.observe_participant(agent)
    observer.arm([agent])
    observer.observe_transcription(
        [SimpleNamespace(final=True)],
        _remote_participant("caller", agent=False),
    )
    # The response can complete while trailing silence is still being
    # published. Waiting later must observe, not discard, these edges.
    observer.observe_attributes({"lk.agent.state": "speaking"}, agent)
    assert observer.response_started
    observer.observe_attributes({"lk.agent.state": "listening"}, agent)

    await observer.wait(0.1)


@pytest.mark.asyncio
async def test_response_quiescence_uses_final_transcripts_when_speaking_edge_is_coalesced(
    monkeypatch,
):
    monkeypatch.setattr(audio_replay_canary, "_RESPONSE_QUIESCENCE_GRACE_SECONDS", 0)
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa-agent", state="listening")
    caller = _remote_participant("caller", agent=False)
    final_segment = SimpleNamespace(final=True)

    observer.observe_participant(agent)
    observer.arm([agent])
    observer.observe_transcription([final_segment], caller)
    observer.observe_transcription([final_segment], agent)

    await observer.wait(0.1)
    assert observer.response_started
    assert observer.response_completed.is_set()


@pytest.mark.asyncio
async def test_response_quiescence_discards_late_greeting_until_caller_final(monkeypatch):
    monkeypatch.setattr(audio_replay_canary, "_RESPONSE_QUIESCENCE_GRACE_SECONDS", 0)
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa-agent", state="listening")
    caller = _remote_participant("caller", agent=False)
    final_segment = SimpleNamespace(final=True)

    observer.observe_participant(agent)
    observer.arm([agent])
    # A delayed greeting starts and finishes after arm, before the caller's
    # final transcript. Neither its state edges nor transcript can satisfy the
    # fixture response gate.
    observer.observe_attributes({"lk.agent.state": "speaking"}, agent)
    observer.observe_transcription([final_segment], agent)
    observer.observe_attributes({"lk.agent.state": "listening"}, agent)
    assert not observer.response_started
    assert not observer.response_completed.is_set()

    observer.observe_transcription([final_segment], caller)
    assert not observer.response_completed.is_set()
    observer.observe_transcription([final_segment], agent)

    await observer.wait(0.1)


@pytest.mark.asyncio
async def test_response_quiescence_ignores_other_participants_and_fails_closed():
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa-agent", state="listening")
    caller = _remote_participant("caller", agent=False, state="speaking")
    other_agent = _remote_participant("other-agent", state="speaking")
    observer.observe_participant(agent)
    observer.arm([agent, caller, other_agent])

    observer.observe_attributes({"lk.agent.state": "speaking"}, caller)
    observer.observe_attributes({"lk.agent.state": "speaking"}, other_agent)
    with pytest.raises(ReplayExecutionError, match="did not start a response"):
        await observer.wait(0.01)

    observer.observe_transcription([SimpleNamespace(final=True)], caller)
    observer.observe_attributes({"lk.agent.state": "speaking"}, agent)
    with pytest.raises(ReplayExecutionError, match="did not finish its response"):
        await observer.wait(0.01)


def test_evaluator_passes_and_never_serializes_transcript_or_private_trace(tmp_path):
    manifest = _manifest()
    environ, agent_id = _environment(tmp_path)
    fixture = load_audio_fixture(manifest, environ=environ)
    call_id = uuid4()
    transcript_text = "When was Al Zaabi Group established"
    report = evaluate_audio_replay(
        manifest,
        call=_completed_call(call_id, agent_id),
        transcript={"turns": [{"role": "user", "content": transcript_text}]},
        fixture=fixture,
    )
    public = report.public_dict()
    serialized = json.dumps(public)
    assert report.passed
    assert public["grounding_counts"] == {"response_after_verified_retrieval": 1}
    assert transcript_text not in serialized
    assert "must-not-leak" not in serialized
    assert "private_transcript" not in serialized


def test_evaluator_fails_closed_on_recognition_latency_and_grounding_regressions(tmp_path):
    manifest = _manifest()
    environ, agent_id = _environment(tmp_path)
    fixture = load_audio_fixture(manifest, environ=environ)
    call_id = uuid4()
    call = _completed_call(call_id, agent_id)
    runtime = call["call_metadata"]["runtime"]
    runtime.pop("turn_latency_p95_ms")
    runtime["turn_diagnostics"] = [
        {
            "turn": 1,
            "grounding_outcome": "no_match_unverified_response",
            "response_action": "asked_transcription_clarification",
            "unexpected_script": True,
        }
    ]
    report = evaluate_audio_replay(
        manifest,
        call=call,
        transcript={"turns": [{"role": "user", "content": "Was also a big group?"}]},
        fixture=fixture,
    )
    failed = {check.name for check in report.checks if not check.passed}
    assert not report.passed
    assert "transcript.turn[0].word_error_rate" in failed
    assert "transcript.turn[0].required_entity[0]" in failed
    assert "latency.turn_latency_p95_ms" in failed
    assert "grounding.responses_after_verified_retrieval" in failed
    assert "grounding.unverified_answers" in failed
    assert "recognition.unexpected_script_turns" in failed
    assert "recognition.transcription_clarifications" in failed


def test_evaluator_gates_barge_in_and_fragment_handling_metrics(tmp_path):
    payload = _manifest_payload()
    payload["expectations"]["interaction"] = {
        "min_barge_in_count": 1,
        "max_barge_in_count": 2,
        "max_suppressed_fragment_count": 0,
        "max_last_interruption_detection_ms": 250,
    }
    manifest = AudioReplayManifest.model_validate(payload)
    environ, agent_id = _environment(tmp_path)
    fixture = load_audio_fixture(manifest, environ=environ)
    call_id = uuid4()
    call = _completed_call(call_id, agent_id)
    call["call_metadata"]["runtime"].update(
        {
            "barge_in_count": 0,
            "suppressed_fragment_count": 2,
            "last_interruption_detection_ms": 310,
        }
    )
    report = evaluate_audio_replay(
        manifest,
        call=call,
        transcript={"turns": [{"role": "user", "content": "When was Al Zaabi Group established?"}]},
        fixture=fixture,
    )
    failed = {check.name for check in report.checks if not check.passed}
    assert "interaction.barge_in_count.minimum" in failed
    assert "interaction.suppressed_fragment_count.maximum" in failed
    assert "interaction.last_interruption_detection_ms" in failed


class _FakeAPI:
    def __init__(self, *, agent_id: UUID, call_id: UUID) -> None:
        self.agent_id = agent_id
        self.call_id = call_id
        self.call_reads = 0
        self.closed = False

    async def get_agent(self, agent_id: UUID) -> dict:
        assert agent_id == self.agent_id
        return {"id": str(agent_id), "name": "Al Zaabi Real KB QA", "is_active": True}

    async def issue_browser_session(self, *, agent_id: UUID, variables: dict) -> dict:
        assert agent_id == self.agent_id
        assert variables == {"qa_replay": "fixture-v1"}
        return {
            "call_id": str(self.call_id),
            "url": "wss://qa.livekit.example.test",
            "access_token": "private-room-token",
            "room_name": f"vav-browser-{self.call_id}",
        }

    async def get_call(self, call_id: UUID) -> dict:
        assert call_id == self.call_id
        self.call_reads += 1
        if self.call_reads == 1:
            return {
                **_completed_call(call_id, self.agent_id),
                "status": "initiated",
            }
        return _completed_call(call_id, self.agent_id)

    async def get_transcript(self, call_id: UUID) -> dict:
        assert call_id == self.call_id
        return {"turns": [{"role": "user", "content": "When was Al Zaabi Group established?"}]}


class _FakePublisher:
    def __init__(self) -> None:
        self.arguments = None

    async def publish(self, **kwargs) -> LiveKitPublishReceipt:
        self.arguments = kwargs
        return LiveKitPublishReceipt(
            room_name=kwargs["room_name"],
            frame_count=5,
            published_audio_seconds=0.1,
            final_room_transcript_segments=1,
        )


@pytest.mark.asyncio
async def test_browser_replay_uses_governed_session_and_returns_redacted_report(tmp_path):
    manifest = _manifest()
    environ, agent_id = _environment(tmp_path)
    call_id = uuid4()
    api = _FakeAPI(agent_id=agent_id, call_id=call_id)
    publisher = _FakePublisher()
    report = await run_live_audio_replay(
        manifest,
        environ=environ,
        api=api,
        publisher=publisher,
    )
    assert report.passed
    assert report.call_id == str(call_id)
    assert publisher.arguments["access_token"] == "private-room-token"
    assert "access_token" not in report.public_dict()
    assert not api.closed


@pytest.mark.asyncio
async def test_browser_replay_accepts_legacy_public_room_projection(tmp_path):
    manifest = _manifest()
    environ, agent_id = _environment(tmp_path)
    call_id = uuid4()

    class PublicBrowserAPI(_FakeAPI):
        async def get_call(self, call_id: UUID) -> dict:
            call = await super().get_call(call_id)
            call["call_metadata"] = {"runtime": call["call_metadata"]["runtime"]}
            return call

    publisher = _FakePublisher()
    report = await run_live_audio_replay(
        manifest,
        environ=environ,
        api=PublicBrowserAPI(agent_id=agent_id, call_id=call_id),
        publisher=publisher,
    )

    assert report.passed
    assert publisher.arguments["room_name"] == f"vav-browser-{call_id}"


class _FakeSIPAPI(_FakeAPI):
    async def issue_browser_session(self, *, agent_id: UUID, variables: dict) -> dict:
        raise AssertionError("SIP fixture replay must not issue or originate a call")

    async def get_call(self, call_id: UUID) -> dict:
        call = await super().get_call(call_id)
        call["provider"] = "livekit_sip"
        # Inbound SIP persists the provider's SIP Call-ID here. The LiveKit
        # room is a separate, canonical transport identity in call metadata.
        call["provider_call_sid"] = "sip-call-id-full-123"
        call["call_metadata"]["livekit_room"] = "qa-sip-room"
        return call


class _PublicSIPAPI(_FakeSIPAPI):
    async def get_call(self, call_id: UUID) -> dict:
        call = await super().get_call(call_id)
        call["call_metadata"].pop("livekit_room")
        call["call_metadata"]["livekit_room_ref"] = public_transport_identity_ref(
            "livekit_room", "qa-sip-room"
        )
        return call


@pytest.mark.asyncio
async def test_sip_replay_only_joins_precreated_allowlisted_fixture(tmp_path):
    manifest = _manifest(mode="sip_fixture")
    environ, agent_id = _environment(tmp_path, mode="sip_fixture")
    call_id = UUID(environ["TEST_CALL_ID"])
    api = _FakeSIPAPI(agent_id=agent_id, call_id=call_id)
    publisher = _FakePublisher()
    report = await run_live_audio_replay(
        manifest,
        environ=environ,
        api=api,
        publisher=publisher,
    )
    assert report.passed
    assert publisher.arguments["room_name"] == "qa-sip-room"
    assert publisher.arguments["access_token"] == "private-livekit-token"


@pytest.mark.asyncio
async def test_sip_replay_accepts_public_hashed_room_projection(tmp_path):
    manifest = _manifest(mode="sip_fixture")
    environ, agent_id = _environment(tmp_path, mode="sip_fixture")
    call_id = UUID(environ["TEST_CALL_ID"])
    publisher = _FakePublisher()

    report = await run_live_audio_replay(
        manifest,
        environ=environ,
        api=_PublicSIPAPI(agent_id=agent_id, call_id=call_id),
        publisher=publisher,
    )

    assert report.passed


@pytest.mark.asyncio
async def test_replay_rejects_conflicting_raw_and_hashed_room_identities(tmp_path):
    manifest = _manifest()
    environ, agent_id = _environment(tmp_path)
    call_id = uuid4()

    class ConflictingRoomAPI(_FakeAPI):
        async def get_call(self, call_id: UUID) -> dict:
            call = await super().get_call(call_id)
            call["call_metadata"]["livekit_room"] = "qa-different-room"
            call["call_metadata"]["livekit_room_ref"] = public_transport_identity_ref(
                "livekit_room", f"vav-browser-{call_id}"
            )
            return call

    publisher = _FakePublisher()
    with pytest.raises(ReplayExecutionError, match="explicit test room"):
        await run_live_audio_replay(
            manifest,
            environ=environ,
            api=ConflictingRoomAPI(agent_id=agent_id, call_id=call_id),
            publisher=publisher,
        )
    assert publisher.arguments is None


@pytest.mark.asyncio
async def test_sip_replay_rejects_call_with_different_canonical_room(tmp_path):
    manifest = _manifest(mode="sip_fixture")
    environ, agent_id = _environment(tmp_path, mode="sip_fixture")
    call_id = UUID(environ["TEST_CALL_ID"])

    class WrongRoomSIPAPI(_FakeSIPAPI):
        async def get_call(self, call_id: UUID) -> dict:
            call = await super().get_call(call_id)
            call["call_metadata"]["livekit_room"] = "qa-different-room"
            return call

    publisher = _FakePublisher()
    with pytest.raises(ReplayExecutionError, match="explicit test room"):
        await run_live_audio_replay(
            manifest,
            environ=environ,
            api=WrongRoomSIPAPI(agent_id=agent_id, call_id=call_id),
            publisher=publisher,
        )
    assert publisher.arguments is None


@pytest.mark.asyncio
async def test_sip_replay_requires_canonical_room_metadata(tmp_path):
    manifest = _manifest(mode="sip_fixture")
    environ, agent_id = _environment(tmp_path, mode="sip_fixture")
    call_id = UUID(environ["TEST_CALL_ID"])

    class MissingRoomSIPAPI(_FakeSIPAPI):
        async def get_call(self, call_id: UUID) -> dict:
            call = await super().get_call(call_id)
            call["call_metadata"].pop("livekit_room")
            # Matching the old overloaded field must not bypass the
            # fail-closed canonical-room check.
            call["provider_call_sid"] = "qa-sip-room"
            return call

    publisher = _FakePublisher()
    with pytest.raises(ReplayExecutionError, match="canonical LiveKit room metadata"):
        await run_live_audio_replay(
            manifest,
            environ=environ,
            api=MissingRoomSIPAPI(agent_id=agent_id, call_id=call_id),
            publisher=publisher,
        )
    assert publisher.arguments is None


@pytest.mark.asyncio
async def test_replay_rejects_agent_without_test_marker_before_session_issue(tmp_path):
    manifest = _manifest()
    environ, agent_id = _environment(tmp_path)

    class ProductionNamedAPI(_FakeAPI):
        async def get_agent(self, agent_id: UUID) -> dict:
            return {"id": str(agent_id), "name": "Customer Receptionist", "is_active": True}

    with pytest.raises(ReplayExecutionError, match="QA/test/canary marker"):
        await run_live_audio_replay(
            manifest,
            environ=environ,
            api=ProductionNamedAPI(agent_id=agent_id, call_id=uuid4()),
            publisher=_FakePublisher(),
        )


def test_checked_in_examples_are_strict_and_reference_environment_only():
    manifests = Path(__file__).parent / "quality" / "manifests"
    for path in manifests.glob("*.example.json"):
        manifest = load_audio_replay_manifest(path)
        raw = path.read_text(encoding="utf-8")
        assert manifest_environment_names(manifest)
        assert "railway.app" not in raw
        assert "@" not in raw
        assert "+971" not in raw
        assert "api_key" not in raw.casefold()
