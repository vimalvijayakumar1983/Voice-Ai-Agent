"""Free-form, multi-company conversations; no hardcoded production facts."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.livekit_runtime import worker
from app.livekit_runtime.inworld_single_pass import deterministic_grounded_reply
from app.models.agent import KnowledgeSource
from app.services.conversation_scope import (
    KnowledgeCompany,
    KnowledgeCompanyScope,
    candidate_companies,
    person_company_directory,
    routing_text,
)
from app.services.knowledge_query_interpreter import SearchRepairResult
from tests.test_conversation_scope import phone_fact
from tests.test_conversation_scope import setup_runtime as base_setup_runtime


async def setup_runtime(*args, **kwargs):
    runtime, agent = await base_setup_runtime(*args, **kwargs)
    runtime._conversation_routing_v2 = True
    return runtime, agent


async def test_new_routing_is_opt_in(db, tenant, monkeypatch):
    runtime, _ = await base_setup_runtime(
        db, tenant, monkeypatch, "Harbour Group", "Harbour Trading"
    )
    assert runtime._conversation_routing_v2 is False


@pytest.mark.parametrize("prefix", ["Sorry, ", "Okay, uh, ", "Well, um, ", "", "Please, "])
async def test_fillers_cannot_lose_phone_followup(db, tenant, monkeypatch, prefix):
    runtime, _ = await setup_runtime(db, tenant, monkeypatch, "Harbour Group", "Harbour Trading")
    planner = AsyncMock()
    monkeypatch.setattr(worker, "interpret_knowledge_question", planner)
    runtime._company_scope.semantic_retrieval_enabled = True
    first = await runtime.retrieve_single_pass_evidence("What's the way to ring the office?")
    assert "665 9998" in first
    second = await runtime.retrieve_single_pass_evidence(prefix + "what about Harbour Trading?")
    assert "551 3831" in second and "665 9998" not in second
    planner.assert_not_awaited()


async def medical_runtime(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(db, tenant, monkeypatch, "Harbour Group", "Harbour Trading")
    names = ["Sun & Moon Specialized Medical Center", "Sun & Moon Cosmetic Medical Center"]
    runtime._company_scope.companies.extend(KnowledgeCompany(name=n) for n in names)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant.id))
    source.structured_content = {
        "facts": [
            *source.structured_content["facts"],
            phone_fact(names[0], "+971 2 123 4000"),
            phone_fact(names[1], "+971 2 567 8000"),
        ]
    }
    await db.commit()
    return runtime, names


async def test_pending_company_choice_completes_previous_request(db, tenant, monkeypatch):
    runtime, names = await medical_runtime(db, tenant, monkeypatch)
    await runtime.retrieve_single_pass_evidence("What is the phone number?")
    reply = await runtime.retrieve_single_pass_evidence("Okay, uh, what about Sun and Moon?")
    assert all(n in reply for n in names)
    assert runtime._single_pass_active_subject is None
    reply = await runtime.retrieve_single_pass_evidence("The Cosmetic Centre")
    assert "567 8000" in reply and "123 4000" not in reply
    assert runtime._single_pass_active_subject == names[1]


async def test_primary_number_is_not_confused_with_mobile_choices(db, tenant, monkeypatch):
    runtime, names = await medical_runtime(db, tenant, monkeypatch)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant.id))
    mobile = phone_fact(names[0], "+971 50 123 4567")
    mobile["predicate"] = "mobile"
    source.structured_content = {"facts": [*source.structured_content["facts"], mobile]}
    await db.commit()
    await runtime.retrieve_single_pass_evidence("Sun and Moon phone number?")
    reply = await runtime.retrieve_single_pass_evidence("The Specialized Centre")
    assert "123 4000" in reply and "50 123" not in reply


async def test_new_intent_overrides_pending_phone_request(db, tenant, monkeypatch):
    runtime, names = await medical_runtime(db, tenant, monkeypatch)
    await runtime.retrieve_single_pass_evidence("Sun and Moon phone number?")
    reply = await runtime.retrieve_single_pass_evidence("What is the address of " + names[1] + "?")
    assert "567 8000" not in reply


@pytest.mark.parametrize("question", ["Who is the director of", "What is the WhatsApp number of"])
async def test_pending_selection_does_not_replace_new_question(db, tenant, monkeypatch, question):
    runtime, names = await medical_runtime(db, tenant, monkeypatch)
    await runtime.retrieve_single_pass_evidence("Sun and Moon phone number?")
    reply = await runtime.retrieve_single_pass_evidence(question + " " + names[1] + "?")
    assert "567 8000" not in reply


def test_person_directory_requires_approved_company_owned_relationship():
    scope = KnowledgeCompanyScope(companies=[KnowledgeCompany(name="Harbour Group")])
    valid = {
        "subject": "Harbour Group",
        "predicate": "person profile: Jane Doe",
        "value": "Director",
        "evidence": "Jane Doe is Director of Harbour Group.",
    }
    assert person_company_directory([{"facts": [valid]}], scope) == {"Jane Doe": ("Harbour Group",)}
    for changed in [
        {"subject": "Other Group"},
        {"predicate": "person profile: John Smith"},
        {"evidence": "Harbour Group employs a Director."},
        {"value": ""},
        {"subject": "Jane Doe", "predicate": "Director of", "value": "Harbour Group"},
    ]:
        assert person_company_directory([{"facts": [{**valid, **changed}]}], scope) == {}


async def test_person_resolves_only_to_approved_owner_after_company_switch(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(db, tenant, monkeypatch, "Harbour Group", "Harbour Trading")
    runtime._person_company_directory = {"Jane Doe": ("Harbour Group",)}
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant.id))
    source.structured_content = {
        "facts": [
            *source.structured_content["facts"],
            {
                "subject": "Harbour Group",
                "predicate": "person profile: Jane Doe",
                "value": "Director",
                "evidence": "Jane Doe is Director of Harbour Group.",
                "search_phrases": ["Who is Jane Doe?"],
            },
        ]
    }
    await db.commit()
    await runtime.retrieve_single_pass_evidence("What is Harbour Trading phone number?")
    evidence = await runtime.retrieve_single_pass_evidence("Okay, uh, who is Jane Doe?")
    assert runtime._single_pass_active_subject == "Harbour Group"
    assert "Director" in evidence and "Jane Doe" in evidence


async def test_partial_names_only_propose_choices_not_permissions(db, tenant, monkeypatch):
    runtime, names = await medical_runtime(db, tenant, monkeypatch)
    assert candidate_companies("Sun and Moon", runtime._company_scope) == tuple(names)
    assert candidate_companies("Cosmetic", runtime._company_scope) == ()
    assert candidate_companies("not Cosmetic", runtime._company_scope, tuple(names)) == ()
    assert candidate_companies("Sun and Moon Unknown Hospital", runtime._company_scope) == tuple(
        names
    )


def test_noise_normalization_preserves_consequential_constraints():
    assert (
        routing_text("Sorry, do not cancel booking 123 tomorrow")
        == "do not cancel booking 123 tomorrow"
    )
    assert routing_text("Okay, uh, not the cosmetic centre") == "not the cosmetic centre"
    assert routing_text("Who’s the chairman?") == "Who is the chairman?"


async def test_budget_exhaustion_is_not_reported_as_provider_outage(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(db, tenant, monkeypatch, "Harbour Group", "Harbour Trading")
    runtime._company_scope.semantic_retrieval_enabled = True
    runtime._telemetry = worker._LiveKitRuntimeTelemetry({}, [], 0)
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    monkeypatch.setattr(
        worker,
        "interpret_knowledge_question",
        AsyncMock(return_value=SearchRepairResult(None, "timeout", 2000, None, None, True)),
    )
    for _ in range(3):
        evidence = await runtime.retrieve_single_pass_evidence("How do I get hold of the team?")
    reply = deterministic_grounded_reply(evidence)
    assert "service is having trouble" not in reply
    assert "couldn't find" in reply
    assert (
        runtime._telemetry.current_turn_trace["knowledge_interpretation_status"]
        == "retry_budget_exhausted"
    )
    # A cap on the optional model must never disable ordinary evidence lookup.
    assert "665 9998" in await runtime.retrieve_single_pass_evidence("What is the phone number?")
