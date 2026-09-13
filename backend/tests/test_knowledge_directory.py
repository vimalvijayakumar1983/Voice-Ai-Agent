import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent, KnowledgeServingRevisionSource
from app.services.knowledge_directory import published_doctor_names, requests_doctor_count
from app.services.knowledge_retrieval import retrieve_knowledge_context
from tests.knowledge_test_utils import publish_test_knowledge


def person(name, evidence=None, kind="person"):
    return {
        "name": name,
        "evidence": evidence or f"{name} | General Practitioner",
        "entity_type": kind,
    }


def test_counts_source_names_not_top_k_or_inferred_medical_roles():
    sources = [
        (
            "Directory",
            {
                "entities": [
                    person("Dr. Amina Ali"),
                    person("Dr Ben Jones"),
                    person("Mr Mo"),
                    person("Dr Fabricated", "No name here"),
                ]
            },
        ),
        ("Text", {"entities": [person("Doctor Amina Ali"), person("Dr Chloe Lee")]}),
    ]
    result = json.loads(published_doctor_names(sources))
    assert result["published_name_count"] == 3
    assert result["complete_current_staff_count_verified"] is False
    assert result["entries"][0]["sources"] == ["Directory", "Text"]


@pytest.mark.parametrize(
    "question", ["How many doctors do you have?", "What is the total number of doctors?"]
)
def test_count_request(question):
    # 'What is' is deliberately kept explicit as normal count framing.
    assert requests_doctor_count(question, "Example Clinic")


@pytest.mark.parametrize(
    "question",
    [
        "How many doctors in Dubai?",
        "How many doctors were there in 2020?",
        "How many female doctors?",
        "How many doctors at Other Clinic?",
    ],
)
def test_does_not_drop_filters(question):
    assert not requests_doctor_count(question, "Example Clinic")


def test_no_names_is_not_zero_doctors():
    assert published_doctor_names([("Empty", {"entities": []})]) is None


def test_large_count_fits_voice_budget_without_counting_only_displayed_entries():
    source = {"entities": [person(f"Dr Person Number {i}") for i in range(350)]}
    encoded = published_doctor_names([("A long directory title " * 20, source)], max_chars=1200)
    assert len(encoded) <= 1200
    data = json.loads(encoded)
    assert data["published_name_count"] == 350
    assert data["entries_omitted"] == 350 - len(data["entries"])


@pytest.mark.asyncio
async def test_count_uses_pinned_release_not_draft_and_honours_tenant(db, tenant):
    agent = Agent(tenant_id=tenant.id, name="Example receptionist", system_prompt="Use knowledge.")
    db.add(agent)
    await db.flush()
    knowledge, revision = await publish_test_knowledge(db, tenant_id=tenant.id, agent=agent)
    revision.manifest = {**(revision.manifest or {}), "owner_company": "Example Clinic"}
    source = await db.scalar(
        select(KnowledgeServingRevisionSource).where(
            KnowledgeServingRevisionSource.serving_revision_id == revision.id,
        )
    )
    source.structured_content = {"entities": [person("Dr Amina Ali"), person("Dr Ben Jones")]}
    knowledge.sources[0].structured_content = {"entities": [person("Dr Draft Only")]}
    await db.flush()
    args = dict(
        agent_id=agent.id,
        query="How many doctors do you have?",
        serving_revision_id=revision.id,
        knowledge_base_id=knowledge.id,
    )
    result = await retrieve_knowledge_context(db, tenant_id=tenant.id, **args)
    assert json.loads(result)["published_name_count"] == 2
    assert "Draft Only" not in result
    filtered = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        query_variants=("How many doctors in Dubai?",),
        **args,
    )
    assert not filtered or "published_name_count" not in filtered
    assert await retrieve_knowledge_context(db, tenant_id=uuid4(), **args) is None
    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            company_subject="Other Clinic",
            **args,
        )
        is None
    )
