"""Cold-session regression tests: real runtime routing and real scoped retrieval."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.livekit_runtime import worker
from app.livekit_runtime.inworld_single_pass import (
    InworldSinglePassController,
    deterministic_grounded_reply,
)
from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource
from app.schemas.agent import AgentUpdate
from app.services.conversation_scope import KnowledgeCompanyScope, mentioned_companies
from app.services.exact_fact_retrieval import retrieve_exact_fact
from app.services.knowledge_retrieval import _source_retrieval_documents, retrieve_knowledge_context
from tests.conftest import test_session_factory as session_factory
from tests.test_inworld_single_pass import _FakeSession


def scope(group="Northstar Group", trading="Northstar Trading"):
    return {
        "semantic_retrieval_enabled": False,
        "default_company": group,
        "companies": [
            {"name": group, "aliases": ["the group", "head office"]},
            {"name": trading, "aliases": ["trading"]},
        ],
    }


def phone_fact(subject, number):
    return {
        "subject": subject,
        "predicate": "primary telephone",
        "value": number,
        "evidence": f"{subject} telephone: {number}.",
        "search_phrases": [f"What is the phone number for {subject}?"],
    }


async def setup_runtime(db, tenant, monkeypatch, group, trading):
    agent = Agent(
        tenant_id=tenant.id,
        name="Generic QA (not a company)",
        system_prompt="Answer from approved knowledge only.",
        voice_provider="inworld",
        knowledge_company_scope=scope(group, trading),
    )
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name="Mixed directory",
        provider="local",
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, kb])
    await db.flush()
    db.add(AgentKnowledgeBinding(tenant_id=tenant.id, agent_id=agent.id, knowledge_base_id=kb.id))
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=kb.id,
            source_type="text",
            name="Mixed company contact page",
            status="indexed",
            content="Shared footer and directory",
            structured_content={
                "facts": [
                    phone_fact(trading, "+971 2 551 3831"),
                    phone_fact(group, "+971 2 665 9998"),
                ]
            },
        )
    )
    await db.commit()
    monkeypatch.setattr(worker, "async_session_factory", session_factory)
    return worker.VAVInworldRealtimeAgent(
        model=agent, single_pass=True, knowledge_terminology=(group, trading)
    ), agent


@pytest.mark.parametrize(
    "group,trading",
    [("Northstar Group", "Northstar Trading"), ("Harbour Holdings", "Harbour Supplies")],
)
async def test_cold_call_switch_correction_and_repeat(db, tenant, monkeypatch, group, trading):
    runtime, _ = await setup_runtime(db, tenant, monkeypatch, group, trading)
    query = "What is the phone number?"
    evidence = await runtime.retrieve_single_pass_evidence(query)
    reply = deterministic_grounded_reply(evidence, query=query)
    assert "+971 2 665 9998" in reply
    assert "551" not in reply
    # Retrieval alone is not spoken memory.
    assert "haven't given" in await runtime.retrieve_single_pass_evidence(
        "Repeat the number slowly"
    )
    runtime.prepare_spoken_response(query, evidence)(reply)
    repeated = await runtime.retrieve_single_pass_evidence("Can you repeat slowly?")
    assert "6, 6, 5" in deterministic_grounded_reply(repeated)
    await runtime.retrieve_single_pass_evidence(f"About {trading}.")
    assert "haven't given" in await runtime.retrieve_single_pass_evidence("Repeat the number")
    evidence = await runtime.retrieve_single_pass_evidence(query)
    reply = deterministic_grounded_reply(evidence, query=query)
    assert "+971 2 551 3831" in reply
    runtime.prepare_spoken_response(query, evidence)(reply)
    await runtime.retrieve_single_pass_evidence(f"No, I mean {group}.")
    evidence = await runtime.retrieve_single_pass_evidence(query)
    assert "+971 2 665 9998" in deterministic_grounded_reply(evidence, query=query)
    assert "551" not in deterministic_grounded_reply(evidence, query=query)


async def test_unknown_and_ambiguous_company_clear_context(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(
        db, tenant, monkeypatch, "Northstar Group", "Northstar Trading"
    )
    assert "Which company" in await runtime.retrieve_single_pass_evidence(
        "Northstar Group and Northstar Trading phone numbers?"
    )
    assert runtime._single_pass_active_subject is None
    assert "Which company" in await runtime.retrieve_single_pass_evidence(
        "What is the phone number?"
    )
    assert "configured companies" in await runtime.retrieve_single_pass_evidence(
        "No, I mean Unknown LLC"
    )


async def test_late_audio_cannot_poison_new_company_memory(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(
        db, tenant, monkeypatch, "Northstar Group", "Northstar Trading"
    )
    commit_old = runtime.prepare_spoken_response("What is the phone number?", "Source: approved")
    await runtime.retrieve_single_pass_evidence("About Northstar Trading")
    commit_old("The Group number is +971 2 665 9998.")
    assert runtime._last_spoken_answer is None
    assert not runtime._spoken_answers


async def test_scoped_database_paths_cannot_return_other_company(db, tenant, monkeypatch):
    _, agent = await setup_runtime(db, tenant, monkeypatch, "Northstar Group", "Northstar Trading")
    # Intentionally misleading query and variant cannot escape the explicit filter.
    for company, forbidden in [("Northstar Group", "551 3831"), ("Northstar Trading", "665 9998")]:
        exact = await retrieve_exact_fact(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            query="What is the phone number?",
            query_variants=("Northstar Trading phone",),
            company_subject=company,
        )
        assert forbidden not in (exact.evidence_context or "")
        context = await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            query="What is the phone number?",
            company_subject=company,
        )
        assert context and forbidden not in context


def test_raw_fallback_and_other_subjects_cannot_escape_scope():
    assert (
        _source_retrieval_documents(
            name="Northstar Group contact",
            content="Northstar Trading +971 2 551 3831. Footer: Northstar Group.",
            structured_content=None,
            company_subject="Northstar Group",
        )
        == []
    )
    docs = _source_retrieval_documents(
        name="Mixed",
        content="Raw wrong number",
        structured_content={
            "facts": [
                phone_fact("Northstar Trading", "123456789"),
                phone_fact("Northstar Group", "987654321"),
            ]
        },
        company_subject="Northstar Group",
    )
    assert "987654321" in str(docs) and "123456789" not in str(docs)


def test_scope_validation_and_metadata_roundtrip():
    data = AgentUpdate(knowledge_company_scope=scope()).model_dump(exclude_unset=True)
    agent = Agent(tenant_id=uuid4(), agent_metadata={"unrelated": True}, **data)
    assert agent.agent_metadata["unrelated"] is True
    assert agent.knowledge_company_scope == scope()
    with pytest.raises(ValidationError):
        KnowledgeCompanyScope.model_validate({**scope(), "default_company": "Unknown"})
    invalid = scope()
    invalid["companies"][1]["aliases"] = ["head office"]
    with pytest.raises(ValidationError):
        KnowledgeCompanyScope.model_validate(invalid)


def test_aliases_use_boundaries_and_do_not_fuzzy_assign_companies():
    config = KnowledgeCompanyScope.model_validate(scope())
    assert mentioned_companies("Northstar Trading telephone", config) == ("Northstar Trading",)
    assert not mentioned_companies("tradingham", config)
    assert not mentioned_companies("Nortstar", config)


async def test_editor_api_persists_scope_without_provider_call(
    client, auth_headers, db, tenant, monkeypatch
):
    _, model = await setup_runtime(db, tenant, monkeypatch, "Northstar Group", "Northstar Trading")
    payload = scope()
    payload["default_company"] = "Northstar Trading"
    response = await client.patch(
        f"/api/v1/agents/{model.id}",
        headers=auth_headers,
        json={"knowledge_company_scope": payload},
    )
    assert response.status_code == 200, response.text
    assert response.json()["knowledge_company_scope"] == payload
    await db.refresh(model)
    assert model.knowledge_company_scope == payload
    assert model.runtime_profile is None  # no provider or model change required


async def test_full_controller_records_only_committed_audio(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(
        db, tenant, monkeypatch, "Northstar Group", "Northstar Trading"
    )
    session = _FakeSession(auto_complete=False)
    controller = InworldSinglePassController(
        session=session,
        retrieve_evidence=runtime.retrieve_single_pass_evidence,
        prepare_spoken_response=runtime.prepare_spoken_response,
    )
    task = controller.on_final_transcript("What is the phone number?", turn_id="phone")
    await asyncio.wait_for(session.generated.wait(), timeout=3)
    assert not runtime._spoken_answers
    handle = session.handles[-1]
    # Provider committed only this portion. Never remember the unspoken suffix.
    handle.chat_items = [
        SimpleNamespace(role="assistant", text_content="The number is +971 2 665.")
    ]
    handle.interrupt(force=True)
    await task
    repeat = await runtime.retrieve_single_pass_evidence("Repeat slowly")
    spoken = deterministic_grounded_reply(repeat)
    assert "6, 6, 5" in spoken and "9998" not in spoken
    await controller.aclose()


async def test_older_same_company_audio_cannot_replace_newer_answer(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(
        db, tenant, monkeypatch, "Northstar Group", "Northstar Trading"
    )
    old = runtime.prepare_spoken_response("What is the phone number?", "Source: evidence")
    new = runtime.prepare_spoken_response("What is the address?", "Source: evidence")
    new("The address is Main Street.")
    old("The number is +971 2 665 9998.")
    reply = await runtime.retrieve_single_pass_evidence("Repeat slowly")
    assert deterministic_grounded_reply(reply) == "The address is Main Street."
