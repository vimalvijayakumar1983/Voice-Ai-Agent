"""Regression coverage for natural deviations in a multi-company voice call."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.livekit_runtime import worker
from app.livekit_runtime.inworld_single_pass import (
    SUPPRESS_REPLY,
    InworldSinglePassController,
    deterministic_grounded_reply,
)
from app.models.agent import KnowledgeSource
from app.services.conversation_scope import KnowledgeCompany, KnowledgeCompanyScope
from app.services.conversation_state import (
    LIST_CONTROLS,
    ConversationState,
    explicit_attribute_request,
    match_people,
)
from app.services.knowledge_collections import collection_reply, decode_collection
from app.services.knowledge_query_interpreter import SearchRepair, SearchRepairResult
from tests.test_conversation_routing import medical_runtime
from tests.test_inworld_single_pass import _FakeSession
from tests.test_knowledge_collections import fact
from tests.test_speech_lexicon import _entry


async def runtime_fixture(db, tenant, monkeypatch):
    runtime, medical = await medical_runtime(db, tenant, monkeypatch)
    runtime._conversation_state_v3 = runtime._collections_enabled = True
    runtime._telemetry = worker._LiveKitRuntimeTelemetry({}, [], 0)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant.id))
    source.structured_content = {
        "facts": [
            *source.structured_content["facts"],
            fact("Harbour Group", "person profile: Alice Jaykumar", "CEO & Managing Director"),
            fact("Harbour Group", "person profile: Beth Brown", "Director"),
            fact("Harbour Group", "person profile: Cara Harbour", "President"),
            fact("Harbour Group", "person profile: Dan Jones", "Chairman"),
            fact("Harbour Group", "person profile: Erin Smith", "Executive Director"),
            fact("Harbour Group", "person profile: Victor Jaykumar", "Director"),
            fact("Harbour Group", "business segment", "Trading"),
            fact("Harbour Group", "business segment", "Healthcare"),
        ]
    }
    await db.commit()
    return runtime, medical


async def ask(runtime, question):
    evidence = await runtime.retrieve_single_pass_evidence(question)
    reply = deterministic_grounded_reply(evidence, query=question)
    if reply:
        runtime.prepare_spoken_response(question, evidence)(reply)
    return reply or evidence


@pytest.mark.parametrize(
    "question",
    [
        "One minute, please.",
        "One minute, one minute, one minute, please.",
        "Give me a second, please.",
        "Just a moment please.",
    ],
)
async def test_hold_does_not_search_or_change_topic(db, tenant, monkeypatch, question):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "What is the phone number?")
    before = runtime._conversation_state.topic_query
    search = AsyncMock(side_effect=AssertionError("control must not search"))
    monkeypatch.setattr(runtime, "_retrieve_approved_knowledge", search)
    assert "Take your time" in await ask(runtime, question)
    assert runtime._conversation_state.topic_query == before


@pytest.mark.parametrize(
    "question",
    [
        "It's okay, thank you very much.",
        "That is all, thanks for your help today.",
    ],
)
async def test_natural_courtesy_bypasses_search(db, tenant, monkeypatch, question):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    monkeypatch.setattr(
        runtime, "_retrieve_approved_knowledge", AsyncMock(side_effect=AssertionError)
    )
    assert "welcome" in await ask(runtime, question)


@pytest.mark.parametrize(
    "question",
    [
        "Is this Cara Harbour?",
        "Is Cara Harbour someone in this company?",
        "I want to know more about Cara Harbour, uh, in Harbour Group.",
    ],
)
async def test_natural_person_question_uses_approved_identity(db, tenant, monkeypatch, question):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "List all directors of Harbour Group.")
    reply = await ask(runtime, question)
    assert "President" in reply and "Which company" not in reply


async def test_second_human_call_context_sequence(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    assert "Director" in await ask(runtime, "Who is Victor Jaykumar?")
    reply = await ask(runtime, "What does Alice hold?")
    assert "Managing Director" in reply and "Victor" not in reply
    await ask(runtime, "Stop, just give me Harbour Group phone number.")
    assert await runtime.retrieve_single_pass_evidence("Sorry.") == SUPPRESS_REPLY
    assert "551 3831" in await ask(runtime, "I mean about Harbour Trading.")
    assert "Which company" in await ask(runtime, "What about Sun and Moon?")
    assert "567 8000" in await ask(runtime, "So it's Sun & Moon Cosmetic Medical Center.")
    retrieve = AsyncMock(return_value="NO_VERIFIED_KNOWLEDGE_MATCH")
    monkeypatch.setattr(runtime, "_retrieve_approved_knowledge", retrieve)
    await ask(runtime, "Do they sell Botox?")
    assert retrieve.await_args.kwargs["query"] == "Do they sell Botox?"


def test_control_preserves_mixed_questions():
    from app.services.conversation_state import conversation_control

    for text in [
        "Thanks, what is the phone number?",
        "Wait, book me for tomorrow.",
        "One minute appointments are available?",
        "Is it okay to book?",
    ]:
        assert conversation_control(text) is None


def test_company_switch_preserves_extra_attributes_and_constraints():
    scope = KnowledgeCompanyScope(
        companies=[KnowledgeCompany(name="A Company"), KnowledgeCompany(name="B Company")]
    )
    state = ConversationState(company="A Company")
    state.plan("Give me A Company phone number and email", scope, {})
    result = state.plan("I meant B Company", scope, {})
    assert "email" in result.query and "phone" in result.query
    state.plan("Give me B Company doctor phone number", scope, {})
    result = state.plan("I meant A Company", scope, {})
    assert "doctor" in result.query


async def test_budget_exhaustion_still_allows_direct_facts_and_courtesy(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._company_scope.semantic_retrieval_enabled = True
    runtime._semantic_attempts = {("Harbour Group", str(i), ""): 1 for i in range(8)}
    interpreter = AsyncMock(side_effect=AssertionError("must respect existing cost cap"))
    monkeypatch.setattr(worker, "interpret_knowledge_question", interpreter)
    reply = await ask(runtime, "What is the moon excursion policy?")
    assert "contact reception" in reply and "clarify" not in reply
    assert "Take your time" in await ask(runtime, "One minute please.")
    assert "welcome" in await ask(runtime, "It's okay, thank you very much.")
    assert "551 3831" in await ask(runtime, "What's the phone number of Harbour Trading?")
    assert sum(runtime._semantic_attempts.values()) == 8


async def test_search_interpreter_does_not_receive_previous_person_answer(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._company_scope.semantic_retrieval_enabled = True
    await ask(runtime, "Who is Victor Jaykumar?")
    interpreter = AsyncMock(
        return_value=SearchRepairResult(None, "timeout", 2000, None, None, True)
    )
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(worker, "interpret_knowledge_question", interpreter)
    await ask(runtime, "What is the moon excursion policy?")
    assert interpreter.await_args.kwargs["previous_answer"] == ""


async def test_full_failed_call_natural_variations(db, tenant, monkeypatch):
    runtime, medical = await runtime_fixture(db, tenant, monkeypatch)
    planner = AsyncMock()
    monkeypatch.setattr(worker, "interpret_knowledge_question", planner)
    reply = await ask(runtime, "List all directors of Harbour Group.")
    assert all(n in reply for n in ["Alice", "Beth", "Erin", "Victor"])
    await ask(runtime, "It's Cara Harbour.")
    assert runtime._single_pass_active_subject == "Harbour Group"
    reply = await ask(runtime, "List the leadership team.")
    assert "Say next" in reply and "Which company" not in reply
    assert "Victor" in await ask(runtime, "Next.")
    assert "end of" in await ask(runtime, "Next.")
    assert "Healthcare" in await ask(runtime, "What are the business divisions?")
    assert "665 9998" in await ask(runtime, "What is the way to ring the Harbour Group office?")
    assert "551 3831" in await ask(runtime, "Sorry, I meant about Harbour Trading.")
    assert "Which company" in await ask(runtime, "What about Sun and Moon?")
    assert "567 8000" in await ask(runtime, "Cosmetic Center.")
    assert runtime._single_pass_active_subject == medical[1]
    assert "Victor Jaykumar" in await ask(runtime, "Who is Victor Jay Kumar?")
    assert runtime._single_pass_active_subject == "Harbour Group"
    assert "CEO" in await ask(runtime, "What position does Alice hold?")
    assert "don't have a published branches list" in await ask(
        runtime, "List all the group's branches."
    )
    reply = await ask(
        runtime, "What is the revenue? What is the annual revenue? Is it 1 billion dirhams?"
    )
    assert "branches" not in reply and "1 billion" not in reply
    assert "Say next" in await ask(runtime, "This is the leadership team.")
    assert await ask(runtime, "Thank you and goodbye.") == "You're welcome. Goodbye."
    planner.assert_not_awaited()


@pytest.mark.parametrize(
    "phrase",
    [
        "Sorry, I meant about Harbour Trading.",
        "I mean Harbour Trading.",
        "What about Harbour Trading?",
        "Harbour Trading.",
    ],
)
async def test_company_correction_retains_question_not_answer(db, tenant, monkeypatch, phrase):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "What is Harbour Group phone number?")
    reply = await ask(runtime, phrase)
    assert "551 3831" in reply and "665 9998" not in reply


async def test_unresolved_company_selection_keeps_request_and_recovers(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "List the leadership team.")
    await ask(runtime, "I meant Unknown Company.")
    reply = await ask(runtime, "What did I ask you before?")
    assert "leadership" in reply
    reply = await ask(runtime, "The company I asked about before.")
    assert "leadership list" in reply


async def test_clarification_resumes_non_phone_request(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "Sun and Moon branches?")
    reply = await ask(runtime, "Cosmetic Centre.")
    assert "Cosmetic Medical Center" in reply and "branches" in reply
    assert "567 8000" not in reply


@pytest.mark.parametrize(
    "question",
    [
        "What is annual revenue? Is it one billion?",
        "What are its opening hours?",
        "Is this service available tomorrow?",
        "What is their cancellation policy?",
    ],
)
async def test_pronoun_does_not_append_previous_topic(db, tenant, monkeypatch, question):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "List all the group's branches.")
    retrieve = AsyncMock(return_value="NO_VERIFIED_KNOWLEDGE_MATCH")
    monkeypatch.setattr(runtime, "_retrieve_approved_knowledge", retrieve)
    await runtime.retrieve_single_pass_evidence(question)
    assert retrieve.await_args.kwargs["query"] == question


@pytest.mark.parametrize("reference", ["Victor Jay Kumar", "Victor Jaykumar", "Victor"])
def test_person_spacing_is_normalization_not_fuzzy_permission(reference):
    assert match_people(reference, {"Victor Jaykumar": ("Harbour Group",)}) == ("Victor Jaykumar",)
    assert not match_people("Harbour", {"Cara Harbour": ("Harbour Group",)})


def test_ambiguous_person_is_not_assigned_to_first_company():
    scope = KnowledgeCompanyScope(
        companies=[KnowledgeCompany(name="A Company"), KnowledgeCompany(name="B Company")]
    )
    state = ConversationState(company="A Company")
    directory = {"Alex North": ("A Company",), "Alex South": ("B Company",)}
    plan = state.plan("Who is Alex?", scope, directory)
    assert (
        plan.action == "clarify" and "Alex North" in plan.message and "Alex South" in plan.message
    )
    plan = state.plan("Alex South", scope, directory)
    assert plan.company == "B Company" and plan.query == "Who is Alex South?"


def test_person_with_two_approved_owners_requires_explicit_choice():
    scope = KnowledgeCompanyScope(
        companies=[KnowledgeCompany(name="A Company"), KnowledgeCompany(name="B Company")]
    )
    state = ConversationState(company="A Company")
    directory = {"Alex North": ("A Company", "B Company")}
    assert state.plan("Who is Alex North?", scope, directory).action == "clarify"
    plan = state.plan("B Company", scope, directory)
    assert plan.company == "B Company" and plan.query == "who is alex north"


async def test_caller_claim_never_enters_directory_or_becomes_a_fact(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "It's Cara Harbour.")
    assert runtime._conversation_state.company == "Harbour Group"
    reply = await ask(runtime, "Who is Cara Harbour?")
    assert "President" in reply
    await ask(runtime, "The chairman is Unknown Person.")
    assert "Unknown Person" not in runtime._person_company_directory


async def test_negated_company_never_selects_forbidden_or_negated_subject(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    result = await ask(runtime, "Not Harbour Trading.")
    assert "Please name" in result and runtime._single_pass_active_subject == "Harbour Group"
    assert "configured companies" in await ask(runtime, "I meant Private Company.")
    assert runtime._single_pass_active_subject == "Harbour Group"


async def test_partial_list_continues_from_last_completed_entry(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    evidence = await runtime.retrieve_single_pass_evidence("List the leadership team.")
    first = decode_collection(evidence)
    commit = runtime.prepare_spoken_response("List the leadership team.", evidence)
    assert runtime._collection_playback is not None
    spoken = collection_reply(first).split(";")[0] + "; Beth"
    commit(spoken)
    second = decode_collection(await runtime.retrieve_single_pass_evidence("Next."))
    assert second.offset == 1 and second.items[0].name == "Beth Brown"
    assert all(i.name != "Alice Jaykumar" for i in second.items)


async def test_next_before_commit_conservatively_repeats_not_skips(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    evidence = await runtime.retrieve_single_pass_evidence("List the leadership team.")
    old_commit = runtime.prepare_spoken_response("List the leadership team.", evidence)
    second_evidence = await runtime.retrieve_single_pass_evidence("Next.")
    second = decode_collection(second_evidence)
    assert second.offset == 0
    runtime.prepare_spoken_response("Next.", second_evidence)
    old_commit(collection_reply(decode_collection(evidence)))
    assert runtime._collection_playback.confirmed_offset == 0


async def test_start_again_and_repeat_are_scoped_list_controls(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "List the leadership team.")
    assert "Victor" in await ask(runtime, "Next.")
    assert "Alice" not in await ask(runtime, "Repeat.")
    assert "Alice" in await ask(runtime, "Start again.")


@pytest.mark.parametrize(
    "replacement", ["What is the phone number?", "Harbour Trading phone number?"]
)
async def test_late_list_callback_cannot_restore_abandoned_topic(
    db, tenant, monkeypatch, replacement
):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    evidence = await runtime.retrieve_single_pass_evidence("List the leadership team.")
    commit = runtime.prepare_spoken_response("List the leadership team.", evidence)
    await ask(runtime, replacement)
    commit(collection_reply(decode_collection(evidence)))
    assert runtime._collection_playback is None
    assert "Which list" in await ask(runtime, "Next.")


async def test_controller_interrupted_handle_keeps_active_list(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    session = _FakeSession(auto_complete=False)
    controller = InworldSinglePassController(
        session=session,
        retrieve_evidence=runtime.retrieve_single_pass_evidence,
        prepare_spoken_response=runtime.prepare_spoken_response,
    )
    task = controller.on_final_transcript("List the leadership team.", turn_id="list")
    await asyncio.wait_for(session.generated.wait(), 3)
    handle = session.handles[-1]
    page = runtime._collection_playback.page
    handle.chat_items = [
        SimpleNamespace(
            role="assistant", text_content=collection_reply(page).split(";")[0] + "; Beth"
        )
    ]
    handle.interrupt(force=True)
    await task
    await asyncio.sleep(0)
    assert not worker._is_incomplete_barge_in_fragment("Next.", allow_list_controls=True)
    result = decode_collection(await runtime.retrieve_single_pass_evidence("Next."))
    assert result.offset == 1
    await controller.aclose()


@pytest.mark.parametrize("command", sorted(LIST_CONTROLS))
def test_complete_list_commands_survive_transcript_fragment_gate(command):
    assert not worker._is_incomplete_barge_in_fragment(command, allow_list_controls=True)


def test_list_control_exception_does_not_disable_fragment_guard_or_change_legacy_lane():
    assert worker._is_incomplete_barge_in_fragment("Next.")
    for fragment in ["The", "I want to", "Can you"]:
        assert worker._is_incomplete_barge_in_fragment(fragment, allow_list_controls=True)


async def test_cold_company_selection_updates_both_routing_and_retrieval(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._conversation_state.company = None
    runtime._single_pass_active_subject = None
    assert "Okay, Harbour Trading" in await ask(runtime, "Harbour Trading.")
    assert runtime._single_pass_active_subject == "Harbour Trading"
    assert "551 3831" in await ask(runtime, "What is the phone number?")


def test_clarification_loop_has_bounded_recovery_message():
    state = ConversationState(company="A Company")
    scope = KnowledgeCompanyScope(companies=[KnowledgeCompany(name="A Company")])
    for _ in range(3):
        plan = state.plan("I meant Unknown Company", scope, {})
    assert "haven't been able to resolve" in plan.message
    assert state.plan("What is A Company phone number?", scope, {}).action == "lookup"
    assert state.clarification_count == 0


async def test_approved_lexicon_spelling_hint_resolves_owner_before_search(db, tenant, monkeypatch):
    runtime, medical = await runtime_fixture(db, tenant, monkeypatch)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant.id))
    source.structured_content = {
        "facts": [
            *source.structured_content["facts"],
            fact("Harbour Group", "person profile: Andrew Vijayakumar", "Director"),
        ]
    }
    await db.commit()
    runtime._speech_lexicon_entries = (
        _entry("Andrew Vijayakumar", entry_id="andrew", entity_type="person", tier=1),
    )
    await ask(runtime, "What is " + medical[1] + " phone number?")
    reply = await ask(runtime, "Who is Andrew Vijay Kumar?")
    assert "Andrew Vijayakumar" in reply and "Director" in reply
    assert runtime._single_pass_active_subject == "Harbour Group"


def test_lexicon_hint_without_approved_owner_does_not_grant_company_access():
    scope = KnowledgeCompanyScope(companies=[KnowledgeCompany(name="A Company")])
    state = ConversationState(company="A Company")
    plan = state.plan("Who is Unknown Person?", scope, {}, person_hint="External Person")
    assert plan.company == "A Company" and "External Person" not in plan.query


@pytest.mark.parametrize(
    "query,clear",
    [
        ("What is annual revenue? Is it one billion?", True),
        ("What is their cancellation policy?", True),
        ("What is the price of it?", False),
        ("What is the number?", False),
        ("Who is the leader?", False),
        ("What is that?", False),
    ],
)
def test_clear_attribute_is_not_confused_with_ambiguous_reference(query, clear):
    assert explicit_attribute_request(query, "Harbour Group") is clear


async def test_clear_unsupported_attribute_does_not_loop_on_clarification(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._company_scope.semantic_retrieval_enabled = True
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(
        worker,
        "interpret_knowledge_question",
        AsyncMock(
            return_value=SearchRepairResult(
                SearchRepair(action="clarify", query=""), "completed", 1, 10, 5, True
            )
        ),
    )
    reply = await ask(runtime, "What is the annual revenue? Is it one billion dirhams?")
    assert "don't have verified" in reply and "clarify" not in reply


async def test_interpreter_timeout_is_not_called_a_missing_fact(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._company_scope.semantic_retrieval_enabled = True
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(
        worker,
        "interpret_knowledge_question",
        AsyncMock(return_value=SearchRepairResult(None, "timeout", 2000, None, None, True)),
    )
    reply = await ask(runtime, "What is the annual revenue?")
    assert "couldn't resolve" in reply and "don't have verified" not in reply


@pytest.mark.parametrize("prefix", ["Sorry.", "I meant.", "Well.", "Actually.", "Please."])
async def test_split_discourse_prefix_never_overwrites_phone_request(
    db, tenant, monkeypatch, prefix
):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "What is the phone number?")
    assert await runtime.retrieve_single_pass_evidence(prefix) == SUPPRESS_REPLY
    assert "551 3831" in await ask(runtime, "I meant about Harbour Trading.")
    assert "Which company" in await ask(runtime, "What about Sun and Moon?")
    assert "567 8000" in await ask(runtime, "Cosmetic Center.")


async def test_split_confirmation_is_bound_to_current_attribute_not_old_list(
    db, tenant, monkeypatch
):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    await ask(runtime, "List all branches.")
    retrieve = AsyncMock(return_value="NO_VERIFIED_KNOWLEDGE_MATCH")
    monkeypatch.setattr(runtime, "_retrieve_approved_knowledge", retrieve)
    await ask(runtime, "What is the annual revenue?")
    reply = await ask(runtime, "Is it one billion dirhams?")
    query = retrieve.await_args.kwargs["query"]
    assert "annual revenue" in query and "one billion" in query and "branches" not in query
    assert "don't have verified" in reply


@pytest.mark.parametrize("callback_delay,expected_offset", [(0.04, 1), (0.35, 0)])
async def test_next_waits_boundedly_for_delayed_speech_commit(
    db, tenant, monkeypatch, callback_delay, expected_offset
):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    session = _FakeSession(auto_complete=False)
    controller = InworldSinglePassController(
        session=session,
        retrieve_evidence=runtime.retrieve_single_pass_evidence,
        prepare_spoken_response=runtime.prepare_spoken_response,
        settle_interrupted_lists=True,
    )
    task = controller.on_final_transcript("List the leadership team.", turn_id="list")
    await asyncio.wait_for(session.generated.wait(), 3)
    old = session.handles[-1]
    old.chat_items = [
        SimpleNamespace(
            role="assistant",
            text_content=collection_reply(runtime._collection_playback.page).split(";")[0]
            + "; Beth",
        )
    ]

    def delayed_interrupt(*, force=False):
        old.interrupted = True
        asyncio.get_running_loop().call_later(callback_delay, old.complete)

    old.interrupt = delayed_interrupt
    session.generated.clear()
    next_task = controller.on_final_transcript("Next.", turn_id="next")
    await asyncio.wait_for(session.generated.wait(), 3)
    assert runtime._collection_playback.page.offset == expected_offset
    session.handles[-1].complete()
    await next_task
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(callback_delay)
    assert runtime._collection_playback.page.offset == expected_offset
    await controller.aclose()
