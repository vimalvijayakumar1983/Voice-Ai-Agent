"""Replacement requests must keep scope, not abandoned answer content."""

from unittest.mock import AsyncMock

import pytest

from app.services.conversation_scope import scope_reply
from tests.test_conversation_foundation import foundation
from tests.test_conversation_state import ask


@pytest.mark.parametrize(
    "replacement",
    [
        "Just give me the phone number.",
        "Stop. Just give me the phone number.",
        "Please stop, just tell me the phone number.",
        "Only give me the phone number.",
    ],
)
async def test_replacement_after_partial_list_uses_exact_fact(db, tenant, monkeypatch, replacement):
    runtime, _ = await foundation(db, tenant, monkeypatch)
    runtime._company_scope.semantic_retrieval_enabled = True
    question = "List the leadership of Harbour Group."
    evidence = await runtime.retrieve_single_pass_evidence(question)
    runtime.prepare_spoken_response(question, evidence)("The published leadership list")
    # As in the real STT stream, stop can also arrive as a separate final.
    await runtime.retrieve_single_pass_evidence("Stop.")
    repair = AsyncMock(
        side_effect=AssertionError("An explicit phone request must not need AI repair")
    )
    monkeypatch.setattr(runtime, "_interpret_turn_plan", repair)
    monkeypatch.setattr(runtime, "_repair_knowledge_search", repair)
    response = await ask(runtime, replacement)
    assert "phone number" in response.lower() and "+" in response
    assert "leadership" not in response.lower()
    assert runtime._collection_playback is None
    assert runtime._conversation_state.company == "Harbour Group"


async def test_repeat_number_never_repeats_prior_list_when_phone_lookup_failed(
    db, tenant, monkeypatch
):
    runtime, _ = await foundation(db, tenant, monkeypatch)
    await ask(runtime, "List the leadership of Harbour Group.")
    runtime._conversation_state.requested_detail = "phone"
    runtime._conversation_state.topic_query = "What is the phone number?"
    runtime._last_spoken_answer = ("Harbour Group", "The published leadership list")
    # Simulate an operational failure, not absence of a published fact.
    retrieve = AsyncMock(return_value=scope_reply("I could not check the number."))
    monkeypatch.setattr(runtime, "_retrieve_approved_knowledge", retrieve)
    response = await ask(runtime, "Repeat that number slowly please.")
    assert "leadership" not in response.lower()
    assert retrieve.await_count == 1
    assert "phone" in retrieve.await_args.kwargs["query"].lower()


async def test_repeat_unselected_number_clarifies_instead_of_assuming_phone(
    db, tenant, monkeypatch
):
    runtime, _ = await foundation(db, tenant, monkeypatch)
    await ask(runtime, "List the leadership of Harbour Group.")
    runtime._last_spoken_answer = ("Harbour Group", "The published leadership list")
    response = await ask(runtime, "Repeat that number slowly please.")
    assert "leadership list" not in response.lower()
    assert "Which" in response or "which" in response


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "Stop. Just give me the phone number for the Dubai branch.",
            "give me the phone number for the Dubai branch.",
        ),
        (
            "Only tell me the address, not the phone number.",
            "tell me the address, not the phone number.",
        ),
        ("What is the address just outside Dubai?", "What is the address just outside Dubai?"),
        ("Tell me about Just Medical Centre.", "Tell me about Just Medical Centre."),
        ("Stop calling me.", "Stop calling me."),
        ("Don't give me the old phone number.", "Don't give me the old phone number."),
    ],
)
def test_framing_preserves_constraints_and_actions(query, expected):
    from app.services.conversation_scope import routing_text

    assert routing_text(query) == expected


async def test_failed_branch_lookup_does_not_repeat_cached_head_office_number(
    db, tenant, monkeypatch
):
    from app.services.exact_fact_retrieval import ExactFactType

    runtime, _ = await foundation(db, tenant, monkeypatch)
    await ask(runtime, "What is the phone number for Harbour Group?")
    assert runtime._spoken_answers[("Harbour Group", ExactFactType.PHONE)]
    requested = "What is the phone number for the Dubai branch?"
    runtime._conversation_state.topic_query = requested
    runtime._conversation_state.requested_detail = "phone"
    retrieval = AsyncMock(return_value=scope_reply("Which Dubai branch do you mean?"))
    monkeypatch.setattr(runtime, "_retrieve_approved_knowledge", retrieval)
    response = await ask(runtime, "Repeat that number slowly please.")
    assert "Dubai branch" in response and "+" not in response
    assert retrieval.await_args.kwargs["query"] == requested


async def test_replacement_selects_new_company_and_repeat_stays_there(db, tenant, monkeypatch):
    runtime, _ = await foundation(db, tenant, monkeypatch)
    await ask(runtime, "List the leadership of Harbour Group.")
    response = await ask(runtime, "Stop. Just give me the phone number for Harbour Trading.")
    assert "551 3831" in response and "665 9998" not in response
    assert runtime._conversation_state.company == "Harbour Trading"
    response = await ask(runtime, "Repeat that number slowly please.")
    assert "5, 5, 1, 3, 8, 3, 1" in response
