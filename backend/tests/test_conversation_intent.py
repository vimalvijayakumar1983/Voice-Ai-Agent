"""Intent contracts, cross-company isolation and natural multi-turn regressions."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.livekit_runtime import worker
from app.services.conversation_intent import (
    ConversationIntent,
    IntentResult,
    apply_intent,
    interpret_conversation_turn,
)
from app.services.conversation_scope import KnowledgeCompany, KnowledgeCompanyScope
from app.services.conversation_state import ConversationState
from tests.test_conversation_state import ask, runtime_fixture
from tests.test_knowledge_query_interpreter import client_reply

SCOPE = KnowledgeCompanyScope(
    companies=[
        KnowledgeCompany(name="Harbour Group"),
        KnowledgeCompany(name="Harbour Trading"),
        KnowledgeCompany(name="Sun & Moon Cosmetic Medical Center"),
        KnowledgeCompany(name="Sun & Moon Specialized Medical Center"),
    ]
)
PEOPLE = {"Cara Harbour": ("Harbour Group",), "Victor Jaykumar": ("Harbour Group",)}


def intent(**kw):
    return ConversationIntent(
        **{
            "intent": "question",
            "company": "Harbour Group",
            "person": None,
            "person_mention": None,
            "detail": "other",
            "query": "What services are offered?",
            **kw,
        }
    )


async def test_interpreter_reports_usage_and_does_not_modify_state():
    state = ConversationState(company="Harbour Trading", topic_query="What is the phone number?")
    before = replace(state)
    client = client_reply(
        intent(
            intent="select_company",
            company="Sun & Moon Cosmetic Medical Center",
            detail="phone",
            query="",
        ).model_dump_json()
    )
    result = await interpret_conversation_turn(
        api_key="test",
        question="I'm talking about Sun & Moon Cosmetic Medical Center",
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
        client=client,
    )
    assert state == before
    assert result.plan.intent == "select_company"
    assert (result.input_tokens, result.output_tokens, result.attempted) == (300, 20, True)
    request = client.chat.completions.create.call_args.kwargs
    assert request["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in request
    assert json.loads(request["messages"][1]["content"])["state"]["company"] == "Harbour Trading"
    assert (
        request["messages"][-1]["content"] == "I'm talking about Sun & Moon Cosmetic Medical Center"
    )
    context = json.loads(request["messages"][1]["content"])
    assert context["context_only"] is True
    assert context["explicit_companies_in_current_turn"] == ["Sun & Moon Cosmetic Medical Center"]


@pytest.mark.parametrize("query", ["", "What is the phone number?"])
def test_selection_uses_stored_request_not_model_supplied_facts(query):
    state = ConversationState(company="Harbour Trading", topic_query="What is the phone number?")
    result = apply_intent(
        intent(intent="select_company", company="Sun & Moon Cosmetic Medical Center", query=query),
        utterance="I'm referring to Sun & Moon Cosmetic Medical Center.",
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
    )
    assert result.company == "Sun & Moon Cosmetic Medical Center"
    assert result.query == "what is the phone number"


def test_selection_rejects_model_invented_replacement_question():
    state = ConversationState(company="Harbour Trading", topic_query="What is the phone number?")
    before = replace(state)
    with pytest.raises(ValueError):
        apply_intent(
            intent(
                intent="select_company",
                company="Sun & Moon Cosmetic Medical Center",
                query="What treatments are offered?",
            ),
            utterance="I'm referring to Sun & Moon Cosmetic Medical Center.",
            state=state,
            scope=SCOPE,
            directory=PEOPLE,
        )
    assert state == before


@pytest.mark.parametrize("payload", ["not JSON", '{"intent":"question","answer":"invented"}'])
async def test_malformed_output_keeps_usage_and_never_changes_state(payload):
    state = ConversationState(company="Harbour Group")
    result = await interpret_conversation_turn(
        api_key="test",
        question="Where does Cara work?",
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
        client=client_reply(payload),
    )
    assert result.plan is None and result.status == "error"
    assert result.input_tokens == 300 and state.company == "Harbour Group"


def test_person_role_confirmation_preserves_wrong_proposed_role():
    state = ConversationState(company="Harbour Group")
    result = apply_intent(
        intent(
            intent="confirm",
            person="Cara Harbour",
            person_mention="Cara Harbour",
            detail="person_role",
            query="Is Cara Harbour the chairman?",
        ),
        utterance="Is Cara Harbour the chairman?",
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
    )
    assert "chairman" in result.query


def test_negated_selection_is_not_a_company_switch():
    state = ConversationState(company="Harbour Group", topic_query="What is the phone number?")
    with pytest.raises(ValueError):
        apply_intent(
            intent(intent="select_company", company="Harbour Trading", query=""),
            utterance="Not Harbour Trading",
            state=state,
            scope=SCOPE,
            directory=PEOPLE,
        )
    assert state.company == "Harbour Group"


@pytest.mark.parametrize(
    "text",
    [
        "Yeah, I'm talking about Sun & Moon Cosmetic Medical Center.",
        "The centre I mean is Sun & Moon Cosmetic Medical Center.",
        "Please use Sun & Moon Cosmetic Medical Center instead.",
    ],
)
def test_company_selection_inherits_attribute_not_answer(text):
    state = ConversationState(
        company="Harbour Trading",
        topic_query="What is the phone number?",
        pending_query="What is the phone number?",
        requested_detail="phone",
    )
    result = apply_intent(
        intent(
            intent="select_company",
            company="Sun & Moon Cosmetic Medical Center",
            query="",
            detail="phone",
        ),
        utterance=text,
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
    )
    assert result.company == "Sun & Moon Cosmetic Medical Center"
    assert "phone" in result.query and "Harbour" not in result.query


@pytest.mark.parametrize(
    "text",
    [
        "So Cara Harbour is working where?",
        "Where does Cara Harbour work?",
        "Which organisation is Cara Harbour associated with?",
    ],
)
def test_person_affiliation_overrides_stale_company_only_with_approved_owner(text):
    state = ConversationState(company="Harbour Trading")
    result = apply_intent(
        intent(
            person="Cara Harbour",
            person_mention="Cara Harbour",
            detail="person_affiliation",
            query="Where does Cara Harbour work?",
        ),
        utterance=text,
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
    )
    assert result.company == "Harbour Group" and result.query == "Who is Cara Harbour?"


def test_explicit_other_company_never_confirms_an_unverified_affiliation():
    state = ConversationState(company="Harbour Group")
    result = apply_intent(
        intent(
            intent="confirm",
            company="Harbour Trading",
            person="Cara Harbour",
            person_mention="Cara Harbour",
            detail="person_affiliation",
            query="Is Cara Harbour in Harbour Trading?",
        ),
        utterance="Is Cara Harbour in Harbour Trading?",
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
    )
    assert result.company == "Harbour Trading" and "Trading" in result.query


@pytest.mark.parametrize(
    "plan,text",
    [
        (intent(company="Private Hospital"), "What services are offered?"),
        (intent(company="Harbour Group"), "What does Harbour Trading offer?"),
        (intent(person="Invented Person", person_mention="Cara Harbour"), "Who is Cara Harbour?"),
        (intent(person="Cara Harbour", person_mention="Cara Harbour"), "Who is the manager?"),
        (intent(company="Sun & Moon Cosmetic Medical Center"), "What about Sun and Moon?"),
        (intent(intent="courtesy", query=""), "Thank you, what is the phone number?"),
    ],
)
def test_rejected_plan_never_mutates_state(plan, text):
    state = ConversationState(company="Harbour Group", topic_query="Existing question")
    before = replace(state)
    with pytest.raises(ValueError):
        apply_intent(plan, utterance=text, state=state, scope=SCOPE, directory=PEOPLE)
    assert state == before


@pytest.mark.parametrize(
    "rewrite,utterance",
    [
        ("Who is the president?", "Who is the chairman?"),
        ("Can I book an appointment?", "Can I cancel an appointment tomorrow?"),
        ("What is the price?", "Is the price 400?"),
        ("", "Is the price 400?"),
        ("What is Harbour Trading phone number?", "What is Harbour Group phone number?"),
    ],
)
def test_lossy_rewrite_falls_back_to_original_without_another_model_pass(rewrite, utterance):
    state = ConversationState(company="Harbour Group")
    result = apply_intent(
        intent(query=rewrite), utterance=utterance, state=state, scope=SCOPE, directory=PEOPLE
    )
    assert result.query == utterance
    assert result.company == "Harbour Group"


def test_clear_identity_with_missing_company_evidence_is_lookup_not_entity_clarification():
    state = ConversationState(company="Harbour Group")
    result = apply_intent(
        intent(
            intent="clarify",
            company="Harbour Trading",
            person="Cara Harbour",
            person_mention="Cara Harbour",
            detail="person_affiliation",
            query="Who is Cara Harbour in Harbour Trading?",
        ),
        utterance="Tell me more about Cara Harbour in Harbour Trading.",
        state=state,
        scope=SCOPE,
        directory=PEOPLE,
    )
    assert result.action == "lookup" and result.company == "Harbour Trading"
    assert result.query != "Who is Cara Harbour?"


@pytest.mark.parametrize(
    "selection",
    [
        "Yeah, I'm talking about Sun & Moon Cosmetic Medical Center.",
        "I am referring to Sun & Moon Cosmetic Medical Center.",
        "Sun & Moon Cosmetic Medical Center, please.",
    ],
)
async def test_runtime_routes_known_selection_without_any_model_pass(
    db, tenant, monkeypatch, selection
):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._structured_intent_enabled = True
    await ask(runtime, "What is the phone number?")
    await ask(runtime, "What about Sun and Moon?")
    interpreter = AsyncMock(
        return_value=IntentResult(
            intent(
                intent="select_company",
                company="Sun & Moon Cosmetic Medical Center",
                query="",
                detail="phone",
            ),
            "completed",
            700,
            300,
            30,
            True,
        )
    )
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(worker, "interpret_conversation_turn", interpreter)
    repair = AsyncMock(side_effect=AssertionError("no second model pass"))
    monkeypatch.setattr(worker, "interpret_knowledge_question", repair)
    reply = await ask(runtime, selection)
    assert "567 8000" in reply and "treatment" not in reply
    assert interpreter.await_count == 0
    assert not runtime._telemetry.runtime_metrics.get("knowledge_interpretation_requests")


@pytest.mark.parametrize(
    "question",
    [
        "I'm talking about Sun & Moon Cosmetic Medical Center prices.",
        "I'm talking about Sun & Moon Cosmetic Medical Center, cancel tomorrow.",
        "I'm not talking about Sun & Moon Cosmetic Medical Center.",
    ],
)
def test_natural_selection_never_swallows_new_detail_action_or_negation(question):
    state = ConversationState(
        company="Harbour Trading",
        topic_query="What is the phone number?",
        pending_companies=(
            "Sun & Moon Cosmetic Medical Center",
            "Sun & Moon Specialized Medical Center",
        ),
        pending_query="What is the phone number?",
    )
    result = state.plan(question, SCOPE, PEOPLE, allow_natural_selection=True)
    assert result.query != "What is the phone number?"


async def test_runtime_person_question_after_other_company(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._structured_intent_enabled = True
    await ask(runtime, "What is Harbour Trading phone number?")
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    interpreter = AsyncMock(
        return_value=IntentResult(
            intent(
                person="Cara Harbour",
                person_mention="Cara Harbour",
                detail="person_affiliation",
                query="Where does Cara Harbour work?",
            ),
            "completed",
            700,
            300,
            30,
            True,
        )
    )
    monkeypatch.setattr(worker, "interpret_conversation_turn", interpreter)
    assert "President" in await ask(runtime, "So Cara Harbour is working where?")
    assert interpreter.await_count == 1


async def test_interpretation_timeout_preserves_topic_and_exact_lookup_still_works(
    db, tenant, monkeypatch
):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._structured_intent_enabled = True
    await ask(runtime, "What is the phone number?")
    before = runtime._conversation_state.topic_query
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(
        worker,
        "interpret_conversation_turn",
        AsyncMock(return_value=IntentResult(None, "timeout", 2000, attempted=True)),
    )
    assert "couldn't reliably interpret" in await ask(runtime, "Where should I head next?")
    assert runtime._conversation_state.topic_query == before
    runtime._semantic_attempts = {("a", str(i), ""): 1 for i in range(8)}
    assert "551 3831" in await ask(runtime, "What is Harbour Trading phone number?")


async def test_superseded_interpreter_cannot_revert_new_company(db, tenant, monkeypatch):
    runtime, _ = await runtime_fixture(db, tenant, monkeypatch)
    runtime._structured_intent_enabled = True
    await ask(runtime, "What is the phone number?")
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kw):
        started.set()
        await release.wait()
        return IntentResult(intent(query="What services are offered?"), "completed", 100)

    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(worker, "interpret_conversation_turn", delayed)
    old = asyncio.create_task(ask(runtime, "No, that's not what I meant"))
    await asyncio.wait_for(started.wait(), 3)
    assert "551 3831" in await ask(runtime, "What is Harbour Trading phone number?")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await old
    assert runtime._conversation_state.company == "Harbour Trading"
