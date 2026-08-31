import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.endpoints import agents as agents_endpoint
from app.core.config import settings
from app.schemas.agent import AgentAIDraftResponse, AgentCreate
from app.services.agent_ai_wizard import (
    AgentAIWizardError,
    KnowledgeBaseSummary,
    _knowledge_recommendation,
    generate_agent_ai_draft,
)


class FakeCompletions:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(self.payload)),
                )
            ]
        )


def fake_openai(payload: dict):
    completions = FakeCompletions(payload)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_ai_wizard_does_not_recommend_an_unidentified_business_knowledge_base():
    knowledge = KnowledgeBaseSummary(
        id=uuid4(),
        name="Adam & Eve Cosmetic Medical Centre",
        description="Approved treatments and appointment information.",
    )

    assert (
        _knowledge_recommendation(
            knowledge.name,
            [knowledge],
            "Create a customer support agent for a fictional clinic.",
        )
        is None
    )
    assert (
        _knowledge_recommendation(
            knowledge.name,
            [knowledge],
            "Create an Adam and Eve cosmetic support concierge.",
        )
        == knowledge
    )


@pytest.mark.asyncio
async def test_ai_wizard_generates_valid_review_only_draft_with_catalog_voice():
    knowledge_id = uuid4()
    client, completions = fake_openai(
        {
            "name": "Clinic Care Concierge",
            "description": "Answers approved clinic questions and captures appointment requests.",
            "system_prompt": (
                "You are the clinic care concierge. Use only approved knowledge for factual "
                "answers, confirm appointment details, never invent availability, and offer "
                "human follow-up when the request cannot be verified. Keep replies concise."
            ),
            "greeting_message": "Hello, how may I help you with the clinic today?",
            "provider": "smallest",
            "speech_rate": 0.95,
            "voice_gender": "female",
            "voice_accent": "Indian",
            "voice_style": "warm support",
            "rationale": "A concise multilingual healthcare support configuration.",
            "assumptions": ["Appointment booking is captured for staff confirmation."],
            "recommended_knowledge_base_name": "Approved Clinic Knowledge",
        }
    )
    result = await generate_agent_ai_draft(
        api_key="test-key",
        brief=(
            "Create a warm support agent using Approved Clinic Knowledge for appointment enquiries."
        ),
        provider_preference="auto",
        primary_language="en",
        timezone="Asia/Dubai",
        voices=[
            {
                "provider": "smallest",
                "id": "jordan",
                "name": "Jordan",
                "languages": ["en", "hi"],
                "accent": "Indian",
                "gender": "female",
                "use_cases": ["support"],
                "synthesizer_model": "waves_lightning_v3_1",
                "voice_pool": "standard",
                "unavailability_reason": None,
            }
        ],
        available_languages=["en", "hi"],
        knowledge_bases=[
            KnowledgeBaseSummary(
                id=knowledge_id,
                name="Approved Clinic Knowledge",
                description="Approved services, doctors, hours and locations.",
            )
        ],
        client=client,
    )

    assert result.draft.voice_id == "jordan"
    assert result.draft.supported_languages == ["en"]
    assert result.draft.language_switching_enabled is False
    assert result.recommended_knowledge_base_id == knowledge_id
    assert result.model == "gpt-4o-mini"
    response_format = completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.asyncio
async def test_ai_wizard_fails_closed_for_unavailable_provider():
    with pytest.raises(AgentAIWizardError, match="Sarvam is not currently available"):
        await generate_agent_ai_draft(
            api_key="test-key",
            brief="Create a customer support voice agent for our approved business information.",
            provider_preference="sarvam",
            primary_language="en",
            timezone="Asia/Dubai",
            voices=[
                {
                    "provider": "smallest",
                    "id": "jordan",
                    "languages": ["en"],
                    "synthesizer_model": "waves_lightning_v3_1",
                    "unavailability_reason": None,
                }
            ],
            available_languages=["en"],
            knowledge_bases=[],
        )


@pytest.mark.asyncio
async def test_ai_draft_endpoint_uses_server_credential_without_creating_agent(
    client,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(settings, "openai_api_key", "platform-openai-test-key")
    captured: dict = {}

    async def catalog(**_kwargs):
        return SimpleNamespace(
            voices=[SimpleNamespace(model_dump=lambda: {"provider": "smallest"})],
            languages=[SimpleNamespace(code="en")],
        )

    async def generated(**kwargs):
        captured.update(kwargs)
        return AgentAIDraftResponse(
            draft=AgentCreate(
                name="Support Concierge",
                description="A safe reviewable support draft.",
                system_prompt="Answer approved support questions and never invent business facts.",
                greeting_message="Hello, how may I help?",
            ),
            rationale="A grounded support configuration for the supplied brief.",
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(agents_endpoint, "get_provider_catalog", catalog)
    monkeypatch.setattr(agents_endpoint, "generate_agent_ai_draft", generated)

    response = await client.post(
        "/api/v1/agents/ai-draft",
        headers=auth_headers,
        json={
            "brief": "Create a grounded customer support agent for approved company information.",
            "provider_preference": "auto",
            "primary_language": "en",
            "timezone": "Asia/Dubai",
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["name"] == "Support Concierge"
    assert captured["api_key"] == "platform-openai-test-key"
    agents = await client.get("/api/v1/agents", headers=auth_headers)
    assert agents.json() == []


@pytest.mark.asyncio
async def test_ai_draft_endpoint_requires_openai_credential(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")

    response = await client.post(
        "/api/v1/agents/ai-draft",
        headers=auth_headers,
        json={
            "brief": "Create a grounded customer support agent for approved company information.",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Add an OpenAI API key in Settings first."
