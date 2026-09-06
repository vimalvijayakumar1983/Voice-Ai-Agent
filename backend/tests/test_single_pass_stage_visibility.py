import pytest

from app.livekit_runtime import worker
from app.livekit_runtime.inworld_single_pass import InworldSinglePassController
from tests.test_inworld_single_pass import _FakeSession


async def test_stage_offsets_separate_scheduling_retrieval_and_preparation():
    now = [100.0]
    stages = []
    session = _FakeSession()

    async def retrieve(_):
        now[0] += 0.020
        return "Verified content"

    def prepare(*_):
        now[0] += 0.030
        return None

    controller = InworldSinglePassController(
        session=session,
        retrieve_evidence=retrieve,
        clock=lambda: now[0],
        prepare_spoken_response=prepare,
        record_stage=lambda sequence, name, ms: stages.append((sequence, name, ms)),
    )
    task = controller.on_final_transcript("A question", turn_id="one")
    now[0] += 0.010
    await task
    values = {name: ms for _, name, ms in stages}
    assert list(values) == [
        "task_started",
        "retrieval_started",
        "retrieval_completed",
        "speech_gate_released",
        "reply_requested",
        "reply_dispatched",
    ]
    assert values["task_started"] == pytest.approx(10)
    assert values["retrieval_completed"] == pytest.approx(30)
    assert values["speech_gate_released"] == pytest.approx(30)
    assert values["reply_requested"] == pytest.approx(60)
    assert len(session.generate_calls) == 1
    await controller.aclose()


async def test_diagnostic_callback_failure_never_breaks_generation():
    session = _FakeSession()

    async def retrieve(_):
        return "Verified content"

    def fail(*_):
        raise RuntimeError("diagnostic failure")

    controller = InworldSinglePassController(
        session=session, retrieve_evidence=retrieve, record_stage=fail
    )
    await controller.on_final_transcript("A question")
    assert len(session.generate_calls) == 1
    await controller.aclose()


def test_reply_request_to_server_speaking_uses_matching_sequence_and_monotonic_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    telemetry = worker._LiveKitRuntimeTelemetry({}, [], 100.0)
    telemetry.on_final_transcript("A question")
    telemetry.mark_single_pass_turn(7)
    trace = telemetry.current_turn_trace
    telemetry.record_single_pass_stage(7, "reply_requested", 42)
    now[0] += 0.4
    telemetry.on_agent_state(new_state="speaking")
    assert trace["single_pass_reply_request_to_server_speaking_ms"] == pytest.approx(400)
    assert "single_pass_reply_requested_at_unix" not in trace

    telemetry.on_final_transcript("A new question")
    telemetry.mark_single_pass_turn(8)
    current = telemetry.current_turn_trace
    telemetry.record_single_pass_stage(7, "retrieval_completed", 12)
    assert trace["single_pass_retrieval_completed_offset_ms"] == 12
    assert "single_pass_retrieval_completed_offset_ms" not in current
    telemetry.on_agent_state(new_state="speaking")
    assert "single_pass_reply_request_to_server_speaking_ms" not in current


def test_invalid_or_unbound_stage_does_not_create_a_trace():
    telemetry = worker._LiveKitRuntimeTelemetry({}, [], 0)
    telemetry.record_single_pass_stage(9, "reply_requested", 10)
    assert telemetry.current_turn_trace is None
    telemetry.on_final_transcript("A question")
    telemetry.mark_single_pass_turn(9)
    for name, value in [
        ("untrusted_field", 10),
        ("reply_requested", float("nan")),
        ("reply_requested", float("inf")),
        ("reply_requested", -1),
    ]:
        telemetry.record_single_pass_stage(9, name, value)
    assert "single_pass_reply_requested_offset_ms" not in telemetry.current_turn_trace
    assert not telemetry.single_pass_reply_requested_at


def test_public_call_metadata_exposes_safe_stage_numbers_only():
    from app.services.call_metadata import public_call_metadata

    public = public_call_metadata(
        {
            "agent_configuration": {},
            "runtime": {
                "turn_diagnostics": [
                    {
                        "turn": 1,
                        "single_pass_reply_requested_offset_ms": 42.0,
                        "single_pass_reply_request_to_server_speaking_ms": 350,
                        "knowledge_entity_resolution_ms": 8,
                        "single_pass_reply_requested_at_unix": 123456,
                        "raw_provider_payload": "private",
                        "raw_transcript": "private",
                    }
                ]
            },
        }
    )
    trace = public["runtime"]["turn_diagnostics"][0]
    assert trace["single_pass_reply_requested_offset_ms"] == 42
    assert trace["single_pass_reply_request_to_server_speaking_ms"] == 350
    assert trace["knowledge_entity_resolution_ms"] == 8
    assert "raw_provider_payload" not in trace
    assert "raw_transcript" not in trace
    assert "single_pass_reply_requested_at_unix" not in trace
