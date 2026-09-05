"""Product acceptance: generic counterparts of failed live exploratory requests."""

import asyncio

import pytest

from app.livekit_runtime.inworld_single_pass import InworldSinglePassController
from app.services.call_disposition import (
    apply_grounding_quality_guard,
    normalize_call_analysis,
    summarize_runtime_grounding,
)
from app.services.call_metadata import public_call_metadata
from app.services.conversation_foundation import (
    RequestLedger,
    booking_decline,
    capability_question,
    company_alias_scope,
    contextual_plan,
    incomplete_request,
)
from tests.test_conversation_state import ask, runtime_fixture
from tests.test_inworld_single_pass import _FakeSession


async def foundation(db, tenant, monkeypatch):
    runtime, medical = await runtime_fixture(db, tenant, monkeypatch)
    runtime._foundation_enabled = True
    runtime._company_scope = company_alias_scope(runtime._company_scope)
    return runtime, medical


@pytest.mark.parametrize(
    "correction",
    [
        "No, I am in Harbour Group. Tell me his role, not the phone number.",
        "No, I mean Harbour Group. Tell me his role, not the phone number.",
        "Sorry, I mean Harbour Group. Tell me his position, not the telephone number.",
    ],
)
async def test_live_role_correction_variants(db, tenant, monkeypatch, correction):
    r, _ = await foundation(db, tenant, monkeypatch)
    await ask(r, "Who is the president of Harbour Group?")
    await ask(r, "Does he also work for Harbour Trading?")
    assert "President" in await ask(r, correction)


async def test_live_short_alias_correction_sequence(db, tenant, monkeypatch):
    r, medical = await foundation(db, tenant, monkeypatch)
    await ask(r, "Who is the chairman?")
    assert "567 8000" in await ask(r, "Give me the cosmetic medical center's phone number.")
    assert "123 4000" in await ask(
        r, "Sorry, not, uh, I'm talking about Sun & Moon Specialized Medical Center."
    )
    both = await ask(r, "Give me both medical centers' phone number and say which is which.")
    assert all(name in both for name in medical)
    assert "primary-telephone" not in both


async def test_resolved_company_correction_never_gets_reinterpreted(db, tenant, monkeypatch):
    from unittest.mock import AsyncMock

    r, medical = await foundation(db, tenant, monkeypatch)
    r._company_scope.semantic_retrieval_enabled = True
    interpreter = AsyncMock(side_effect=AssertionError("already resolved correction"))
    monkeypatch.setattr(r, "_interpret_turn_plan", interpreter)
    assert "567 8000" in await ask(r, "Give me the cosmetic medical center phone number.")
    assert "123 4000" in await ask(r, f"I mean {medical[0]}.")
    assert "123 4000" in await ask(r, f"Not the Cosmetic centre. I mean {medical[0]}.")
    interpreter.assert_not_awaited()


async def test_vad_without_final_does_not_silently_abandon_request(monkeypatch):
    from app.livekit_runtime import inworld_single_pass as module

    monkeypatch.setattr(module, "SPEECH_RESOLUTION_TIMEOUT_SECONDS", 0.01)
    release = asyncio.Event()

    async def retrieve(text):
        await release.wait()
        return "NO_VERIFIED_KNOWLEDGE_MATCH"

    session = _FakeSession()
    c = InworldSinglePassController(
        session=session, retrieve_evidence=retrieve, recover_untranscribed_speech=True
    )
    task = c.on_final_transcript("Who is the chairman?")
    c.on_user_speech_started()
    c.on_meaningful_user_speech()
    release.set()
    await asyncio.sleep(0.03)
    assert not session.say_calls
    c.on_user_speech_stopped()
    await task
    assert "repeat your question" in session.say_calls[0]["text"]
    await c.aclose()


async def test_lost_person_reference_never_returns_another_executive(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    r._conversation_state.person = None
    reply = await ask(r, "I mean Harbour Group. Tell me his role.")
    assert "Whose role" in reply
    assert "Managing Director" not in reply


def test_derived_aliases_do_not_cross_scope_or_assign_shared_suffix():
    from app.services.conversation_scope import (
        KnowledgeCompany,
        KnowledgeCompanyScope,
        mentioned_companies,
    )

    scope = KnowledgeCompanyScope(
        companies=[
            KnowledgeCompany(name="North Dental Clinic"),
            KnowledgeCompany(name="South Dental Clinic"),
            KnowledgeCompany(name="North Cosmetic Clinic"),
        ]
    )
    derived = company_alias_scope(scope)
    assert mentioned_companies("Cosmetic Clinic", derived) == ("North Cosmetic Clinic",)
    assert not mentioned_companies("Dental Clinic", derived)
    assert {c.name for c in derived.companies} == {c.name for c in scope.companies}
    from app.services.conversation_state import ConversationState

    state = ConversationState(company="North Cosmetic Clinic")
    plan = contextual_plan("Give me the Dental Clinic phone number", state, derived, {})
    assert plan.action == "clarify"
    assert set(state.pending_companies) == {"North Dental Clinic", "South Dental Clinic"}
    assert "phone" in state.pending_query


async def test_recovery_prompt_keeps_request_unresolved(db, tenant, monkeypatch):
    from app.services.conversation_scope import scope_reply

    r, _ = await foundation(db, tenant, monkeypatch)
    q = "Who is the chairman?"
    await r.retrieve_single_pass_evidence(q)
    e = scope_reply("I didn't catch that interruption. Please repeat your question.")
    r.prepare_spoken_response(q, e)(e.split(":", 1)[1])
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 1


async def test_final_replacement_wins_after_provisional_interim(monkeypatch):
    release = asyncio.Event()
    seen = []

    async def retrieve(text):
        seen.append(text)
        if text == "old question":
            await release.wait()
        return "NO_VERIFIED_KNOWLEDGE_MATCH"

    session = _FakeSession()
    c = InworldSinglePassController(
        session=session, retrieve_evidence=retrieve, recover_untranscribed_speech=True
    )
    first = c.on_final_transcript("old question")
    await asyncio.sleep(0)
    c.on_user_speech_started()
    c.on_meaningful_user_speech()
    assert c.active
    await c.on_final_transcript("new question")
    release.set()
    await first
    assert len(session.say_calls) == 1
    await c.aclose()


async def test_verified_person_pronoun_and_field_correction(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    assert "Cara" in await ask(r, "Who is the president of Harbour Group?")
    assert r._conversation_state.person == "Cara Harbour", (
        r._person_company_directory,
        r._conversation_state,
    )
    answer = await ask(r, "Does he also work for Harbour Trading?")
    assert "don't have verified" in answer and "phone" not in answer
    answer = await ask(r, "No, I mean Harbour Group. Tell me his role, not the phone number.")
    assert "President" in answer and "phone" not in answer


async def test_positive_company_correction_repeat_and_both(db, tenant, monkeypatch):
    r, medical = await foundation(db, tenant, monkeypatch)
    assert "567 8000" in await ask(r, f"Give me the phone number for {medical[1]}.")
    answer = await ask(r, f"Not the Cosmetic centre. I mean {medical[0]}.")
    assert medical[0] in answer and "567 8000" not in answer
    slow = await ask(r, "Repeat that number slowly, please.")
    assert medical[0] in slow and "clarify" not in slow
    assert "plus 9, 7, 1, 2, 1, 2, 3, 4, 0, 0, 0" in slow
    both = await ask(
        r, "Can you give me the phone numbers of both medical centres and say which is which?"
    )
    assert all(company in both for company in medical)
    assert "567 8000" in both


async def test_false_role_claim_uses_current_company_not_shared_person_surname(
    db, tenant, monkeypatch
):
    r, _ = await foundation(db, tenant, monkeypatch)
    first = await ask(r, "Who is the chairman of Harbour Group?")
    assert "Dan Jones" in first, first
    assert "Cara Harbour" in r._person_company_directory, r._person_company_directory
    answer = await ask(r, "I was told Cara Harbour is the chairman. Is that correct?")
    assert "Dan Jones" in answer and "Which company" not in answer, (
        r._conversation_state,
        r._company_scope,
        r._telemetry.runtime_metrics,
    )


async def test_capability_is_not_a_knowledge_gap_or_booking(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    answer = await ask(
        r,
        "Please do not book anything. Can you make an actual appointment, "
        "or only provide information?",
    )
    assert "cannot" in answer.lower() and "book" in answer.lower()
    assert "clarify" not in answer and "confirmed" not in answer


async def test_split_booking_decline_finishes_capability_request(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    q = "Can you actually book an appointment or only provide information?"
    e = await r.retrieve_single_pass_evidence(q)
    r.prepare_spoken_response(q, e)("I can provide")
    reply = await ask(r, "Don't book anything.")
    assert "cannot book" in reply and "clarify" not in reply
    assert r._request_ledger.metrics()["conversation_requests_total"] == 1
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "Don't book anything. What is the price?",
        "What is the policy if I do not book an appointment?",
        "Cancel my appointment.",
    ],
)
def test_booking_prohibition_does_not_swallow_questions_or_actions(text):
    assert not booking_decline(text)


async def test_incomplete_question_never_reaches_retrieval_and_joins_continuation():
    retrieved = []

    async def retrieve(text):
        retrieved.append(text)
        return "NO_VERIFIED_KNOWLEDGE_MATCH"

    session = _FakeSession()
    controller = InworldSinglePassController(
        session=session,
        retrieve_evidence=retrieve,
        incomplete_request=lambda t: t.strip(" .?!") == "Is Cara Harbour",
        fragment_wait_seconds=0.1,
    )
    first = controller.on_final_transcript("Is Cara Harbour.", turn_id="a")
    await asyncio.sleep(0.02)
    assert not retrieved and not session.say_calls
    controller.on_user_speech_started()
    controller.on_meaningful_user_speech()
    last = controller.on_final_transcript("in Harbour Trading?", turn_id="b")
    await last
    assert retrieved == ["Is Cara Harbour in Harbour Trading?"]
    assert len(session.say_calls) == 1
    assert first.cancelled()
    await controller.aclose()


async def test_fragment_expiry_clarifies_without_mutating_scope_or_query():
    retrieved = []

    async def retrieve(text):
        retrieved.append(text)

    session = _FakeSession()
    c = InworldSinglePassController(
        session=session,
        retrieve_evidence=retrieve,
        incomplete_request=lambda t: True,
        fragment_wait_seconds=0.01,
    )
    await c.on_final_transcript("What about")
    assert not retrieved
    assert "finish" in session.say_calls[0]["text"]
    await c.aclose()


async def test_complete_question_does_not_wait_for_fragment_window():
    event = asyncio.Event()

    async def retrieve(text):
        event.set()
        return "NO_VERIFIED_KNOWLEDGE_MATCH"

    c = InworldSinglePassController(
        session=_FakeSession(),
        retrieve_evidence=retrieve,
        incomplete_request=lambda t: False,
        fragment_wait_seconds=5,
    )
    task = c.on_final_transcript("Who is the chairman?")
    await asyncio.wait_for(event.wait(), 0.2)
    await task
    await c.aclose()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Is Cara Harbour", True),
        ("Who is Cara Harbour?", False),
        ("Is Cara Harbour the president?", False),
        ("What is the phone number?", False),
        ("What is the phone number of", True),
        ("Thank you and goodbye", False),
    ],
)
def test_only_incomplete_requests_wait(text, expected):
    assert incomplete_request(text, ("Cara Harbour",)) is expected


@pytest.mark.parametrize(
    "text",
    [
        "What is the appointment policy?",
        "Can you tell me the appointment cancellation policy?",
        "Can you give me the phone number?",
    ],
)
def test_information_question_is_not_an_action_capability_question(text):
    assert not capability_question(text)


async def test_new_question_does_not_join_abandoned_fragment():
    retrieved = []

    async def retrieve(text):
        retrieved.append(text)
        return "NO_VERIFIED_KNOWLEDGE_MATCH"

    c = InworldSinglePassController(
        session=_FakeSession(),
        retrieve_evidence=retrieve,
        incomplete_request=lambda t: t == "What about",
        fragment_wait_seconds=1,
    )
    first = c.on_final_transcript("What about", turn_id="a")
    assert c.on_final_transcript("What about", turn_id="a") is None
    await c.on_final_transcript("Who is the president?", turn_id="b")
    assert retrieved == ["Who is the president?"]
    assert first.cancelled()
    await c.aclose()


def test_ledger_clarification_is_resolved_only_by_its_own_answer():
    ledger = RequestLedger()
    a = ledger.begin()
    ledger.complete(a, "clarification")
    b = ledger.begin()
    ledger.complete(b, "answered")
    assert ledger.metrics()["conversation_requests_unresolved"] == 1
    ledger.complete(a, "answered")
    assert ledger.metrics()["conversation_requests_unresolved"] == 0
    c = ledger.begin()
    ledger.complete(c, "clarification")
    assert ledger.begin(resumes=True) == c
    ledger.complete(c, "refused")
    assert ledger.metrics()["conversation_requests_unresolved"] == 0


def test_unresolved_ledger_survives_public_metadata_and_caps_disposition_without_traces():
    runtime = RequestLedger()
    runtime.begin()
    metadata = public_call_metadata({"agent_configuration": {}, "runtime": runtime.metrics()})
    grounding = summarize_runtime_grounding(metadata)
    assert grounding["unresolved_requests"] == 1
    analysis = normalize_call_analysis(
        {
            "summary": "Caller left.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.95,
        },
        profile="receptionist",
    )
    details = apply_grounding_quality_guard(analysis, grounding=grounding)["disposition_details"]
    assert details["resolution"] == "partially_resolved" and details["needs_review"]


async def test_clarification_then_choice_resolves_request_but_courtesy_does_not(
    db, tenant, monkeypatch
):
    r, _ = await foundation(db, tenant, monkeypatch)
    await ask(r, "What is the phone number for Sun and Moon?")
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 1
    await ask(r, "Thank you")
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 1
    await ask(r, "The Cosmetic centre")
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 0


async def test_new_constraints_cannot_be_discarded_by_role_correction(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    await ask(r, "Who is the president of Harbour Group?")
    assert (
        contextual_plan(
            "Tell me his role and salary",
            r._conversation_state,
            r._company_scope,
            r._person_company_directory,
        )
        is None
    )


async def test_interrupted_answer_and_new_topic_remain_distinct_requests(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    query = "Who is the president of Harbour Group?"
    evidence = await r.retrieve_single_pass_evidence(query)
    r.prepare_spoken_response(query, evidence)("The president")
    await ask(r, "What is the phone number of Harbour Group?")
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 1


async def test_continuing_speech_preserves_fragment_after_initial_wait():
    seen = []

    async def retrieve(text):
        seen.append(text)
        return "NO_VERIFIED_KNOWLEDGE_MATCH"

    c = InworldSinglePassController(
        session=_FakeSession(),
        retrieve_evidence=retrieve,
        incomplete_request=lambda t: t == "Is Cara Harbour",
        fragment_wait_seconds=0.01,
    )
    c.on_final_transcript("Is Cara Harbour")
    c.on_user_speech_started()
    await asyncio.sleep(0.03)
    assert not seen
    await c.on_final_transcript("in Harbour Trading?")
    assert seen == ["Is Cara Harbour in Harbour Trading?"]
    await c.aclose()


async def test_negated_company_and_extra_affiliation_constraint_are_not_dropped(
    db, tenant, monkeypatch
):
    r, _ = await foundation(db, tenant, monkeypatch)
    await ask(r, "Who is the president of Harbour Group?")
    assert (
        contextual_plan(
            "Not Harbour Group. Tell me his role.",
            r._conversation_state,
            r._company_scope,
            r._person_company_directory,
        )
        is None
    )
    plan = contextual_plan(
        "Where does Cara Harbour work and what is his salary?",
        r._conversation_state,
        r._company_scope,
        r._person_company_directory,
    )
    assert "salary" in plan.query


async def test_generic_role_query_stays_the_requested_role(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    await ask(r, "Who is the president of Harbour Group?")
    assert "Dan Jones" in await ask(r, "Who is the chairman?")


async def test_fragmented_role_correction_keeps_current_requested_detail(db, tenant, monkeypatch):
    r, _ = await foundation(db, tenant, monkeypatch)
    await ask(r, "Who is the president of Harbour Group?")
    await ask(r, "Does he also work for Harbour Trading?")
    assert await r.retrieve_single_pass_evidence("No.") == "SUPPRESS_REPLY"
    assert "President" in await ask(r, "I mean Harbour Group. Tell me his role.")
    assert "President" in await ask(r, "Not the phone number.")
    assert "Which detail" in await ask(r, "Not the role.")


async def test_negative_company_fragment_preserves_detail_until_positive_choice(
    db, tenant, monkeypatch
):
    r, medical = await foundation(db, tenant, monkeypatch)
    await ask(r, f"Give me the phone number for {medical[1]}.")
    prior = r._conversation_state.topic_query
    assert r.is_incomplete_request("Not the cosmetic center.")
    assert await r.retrieve_single_pass_evidence("Not the cosmetic center.") == "SUPPRESS_REPLY"
    assert r._conversation_state.topic_query == prior
    assert "123 4000" in await ask(r, f"Not the cosmetic center I mean {medical[0]}.")


async def test_clear_unknown_identity_after_search_repair_is_not_an_unknown_detail(
    db, tenant, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.livekit_runtime import worker
    from app.services.knowledge_query_interpreter import SearchRepair, SearchRepairResult

    r, _ = await foundation(db, tenant, monkeypatch)
    r._company_scope.semantic_retrieval_enabled = True
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
    reply = await ask(r, "Who is Dr. Noura Example at Harbour Group?")
    assert "don't have verified" in reply and "clarify" not in reply


async def test_explicit_stop_cancels_request_without_hiding_other_unanswered_requests(
    db, tenant, monkeypatch
):
    r, _ = await foundation(db, tenant, monkeypatch)
    q = "List the leadership team of Harbour Group."
    e = await r.retrieve_single_pass_evidence(q)
    late_commit = r.prepare_spoken_response(q, e)
    await r.retrieve_single_pass_evidence("Stop.")
    late_commit("The published leadership list")
    await ask(r, "Just tell me the chairman name.")
    assert r._request_ledger.metrics()["conversation_requests_unresolved"] == 0
    assert r._request_ledger.metrics()["conversation_requests_cancelled"] == 1
