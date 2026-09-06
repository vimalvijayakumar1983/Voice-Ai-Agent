from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.livekit_runtime.inworld_single_pass import NO_KNOWLEDGE_REQUIRED
from app.livekit_runtime.worker import VAVInworldRealtimeAgent, _LiveKitRuntimeTelemetry
from app.services.audio_replay_canary import _AgentResponseQuiescence
from app.services.conversation_foundation import conversational_request, incomplete_request
from tests.test_audio_replay_canary import _remote_participant
from tests.test_conversation_state import ask, runtime_fixture


@pytest.mark.parametrize(
    "text",
    [
        "Can you hear me?",
        "Hello. Can you hear me clearly?",
        "Which day did I ask for?",
        "No, Tuesday afternoon. Which day did I ask for?",
        "What time did I select?",
        "What did I mention?",
    ],
)
def test_conversation_not_an_unfinished_business_lookup(text):
    assert conversational_request(text)
    assert not incomplete_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Can you hear me and tell me the price?",
        "Which day is the clinic open?",
        "What is the phone number?",
        "What did I mention and is it correct?",
        "Which day did I ask for and book it?",
        "Please tell me about",
    ],
)
def test_mixed_business_and_action_requests_do_not_bypass_grounding(text):
    assert conversational_request(text) is None


async def test_social_and_memory_preserve_topic_without_search(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "What is the phone number?")
    before = runtime._conversation_state.topic_query
    monkeypatch.setattr(
        runtime, "_retrieve_approved_knowledge", AsyncMock(side_effect=AssertionError)
    )
    assert "hear you" in await ask(runtime, "Can you hear me?")
    assert (
        await runtime.retrieve_single_pass_evidence(
            "No, Tuesday afternoon. Which day did I ask for?"
        )
        == NO_KNOWLEDGE_REQUIRED
    )
    assert runtime._conversation_state.topic_query == before


async def test_native_qa_tool_uses_shared_scoped_router():
    router = AsyncMock(return_value="approved company evidence")
    raw = AsyncMock(side_effect=AssertionError("must not bypass company selection"))
    runtime = SimpleNamespace(
        _provider_native_turns_qa=True,
        _telemetry=None,
        _company_scope=object(),
        retrieve_single_pass_evidence=router,
        _retrieve_approved_knowledge=raw,
    )
    result = await VAVInworldRealtimeAgent.search_approved_knowledge(
        runtime, "Phone number for the other approved centre"
    )
    assert result == "approved company evidence"
    router.assert_awaited_once()


async def test_native_shared_router_selects_correct_company_evidence(db, tenant, monkeypatch):
    runtime, medical = await runtime_fixture(db, tenant, monkeypatch)
    runtime._foundation_enabled = True
    runtime._provider_native_turns_qa = True
    first = await VAVInworldRealtimeAgent.search_approved_knowledge(
        runtime, f"What is the phone number for {medical[1]}?"
    )
    assert "567 8000" in first and "123 4000" not in first
    second = await VAVInworldRealtimeAgent.search_approved_knowledge(
        runtime, f"Not the cosmetic centre. I mean {medical[0]}."
    )
    assert "123 4000" in second and "567 8000" not in second
    assert runtime._telemetry.runtime_metrics["native_request_ledger_available"] is False
    assert "conversation_requests_unresolved" not in runtime._telemetry.runtime_metrics


def test_barge_in_observer_consumed_once():
    telemetry = _LiveKitRuntimeTelemetry({}, [], 0)
    telemetry.pending_barge_in_transcript = True
    telemetry.on_final_transcript("Stop")
    assert telemetry.consume_barge_in_transcript()
    telemetry.on_final_transcript("Next question")
    assert not telemetry.consume_barge_in_transcript()


def test_replay_cannot_advance_on_response_to_first_fragment():
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa", state="listening")
    caller = _remote_participant("caller", agent=False)
    observer.observe_participant(agent)
    observer.arm([agent], expected_caller_tail="specialized medical center")

    def final(text, participant):
        observer.observe_transcription([SimpleNamespace(text=text, final=True)], participant)

    final("Not the cosmetic center", caller)
    final("I apologize.", agent)
    observer.observe_state("listening")
    assert not observer.response_completed.is_set()
    final("I mean the specialized medical center", caller)
    observer.observe_state("speaking")
    observer.observe_state("listening")
    assert not observer.response_completed.is_set()
    final("The number is in the approved directory.", agent)
    assert observer.response_completed.is_set()


def test_unrecognized_fixture_tail_is_not_a_success():
    observer = _AgentResponseQuiescence()
    agent = _remote_participant("qa", state="listening")
    caller = _remote_participant("caller", agent=False)
    observer.arm([agent], expected_caller_tail="which day please")
    observer.observe_transcription([SimpleNamespace(text="which bay please", final=True)], caller)
    observer.observe_transcription([SimpleNamespace(text="Hello", final=True)], agent)
    assert not observer.response_completed.is_set()
