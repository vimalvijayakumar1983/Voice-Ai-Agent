import json
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints import knowledge as knowledge_endpoint
from app.core.config import settings
from app.schemas.knowledge import (
    KnowledgeAIDraftRequest,
    KnowledgeAIDraftResponse,
    KnowledgeBaseCreate,
)
from app.services.knowledge_ai_wizard import generate_knowledge_ai_draft


class FakeCompletions:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload)))]
        )


def fake_openai(payload: dict):
    completions = FakeCompletions(payload)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


@pytest.mark.asyncio
async def test_knowledge_ai_wizard_generates_strict_reviewable_metadata():
    client, completions = fake_openai(
        {
            "name": "Clinic Service Knowledge",
            "description": (
                "Approved clinic service descriptions, practitioner information, locations, "
                "hours, policies, and customer FAQs."
            ),
            "scope_type": "department",
            "scope_label": "Patient Services",
            "tags": ["services", "doctors", "appointments", "policies"],
            "rationale": "A focused, governed source boundary for clinic support conversations.",
            "assumptions": ["Only approved public business content will be added."],
            "recommended_sources": [
                "Official service pages",
                "Approved doctor directory",
                "Appointment and cancellation policies",
            ],
        }
    )

    result = await generate_knowledge_ai_draft(
        api_key="test-key",
        brief="Create governed knowledge for the clinic patient services department.",
        scope_preference="workspace",
        languages=["en", "ar"],
        client=client,
    )

    assert result.draft.scope_type == "workspace"
    assert result.draft.scope_label is None
    assert result.draft.languages == ["en", "ar"]
    prompt_payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert prompt_payload["content_languages"] == ["en", "ar"]
    assert result.recommended_sources[0] == "Official service pages"
    response_format = completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.asyncio
async def test_knowledge_ai_endpoint_uses_server_key_without_creating_knowledge(
    client,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(settings, "openai_api_key", "platform-openai-test-key")
    captured: dict = {}

    async def generated(**kwargs):
        captured.update(kwargs)
        return KnowledgeAIDraftResponse(
            draft=KnowledgeBaseCreate(
                name="Support Knowledge",
                description="Approved customer support policies and frequently asked questions.",
                tags=["support", "policies"],
            ),
            rationale="A governed support knowledge structure for review.",
            recommended_sources=["Official support FAQs"],
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(knowledge_endpoint, "generate_knowledge_ai_draft", generated)

    response = await client.post(
        "/api/v1/knowledge/ai-draft",
        headers=auth_headers,
        json={
            "brief": "Create governed support knowledge from approved company information.",
            "scope_preference": "workspace",
            "languages": ["en", "ar"],
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["name"] == "Support Knowledge"
    assert captured["api_key"] == "platform-openai-test-key"
    assert captured["languages"] == ["en", "ar"]
    knowledge = await client.get("/api/v1/knowledge", headers=auth_headers)
    assert knowledge.json() == []


@pytest.mark.asyncio
async def test_knowledge_ai_endpoint_requires_openai_key(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")

    response = await client.post(
        "/api/v1/knowledge/ai-draft",
        headers=auth_headers,
        json={"brief": "Create governed support knowledge from approved company information."},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Add an OpenAI API key in Settings first."


def test_knowledge_ai_languages_accept_lists_and_legacy_comma_separated_value():
    current = KnowledgeAIDraftRequest.model_validate(
        {
            "brief": "Create governed multilingual support knowledge for callers.",
            "languages": ["EN", "ar", "en"],
        }
    )
    legacy = KnowledgeAIDraftRequest.model_validate(
        {
            "brief": "Create governed multilingual support knowledge for callers.",
            "primary_language": "en, ar",
        }
    )

    assert current.languages == ["en", "ar"]
    assert legacy.languages == ["en", "ar"]
