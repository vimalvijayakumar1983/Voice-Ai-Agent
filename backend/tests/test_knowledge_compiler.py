import json
from types import SimpleNamespace

import pytest

from app.services.knowledge_compiler import (
    KnowledgeCompilerError,
    compile_website_knowledge,
)


class _FakeCompletions:
    def __init__(self, payload: dict):
        self.payload = payload
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload)))],
            usage=SimpleNamespace(prompt_tokens=800, completion_tokens=200),
        )


class _FakeClient:
    def __init__(self, payload: dict):
        self.completions = _FakeCompletions(payload)
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.asyncio
async def test_fast_compilation_is_searchable_without_an_llm():
    result = await compile_website_knowledge(
        title="Clinic contact",
        url="https://clinic.example/contact",
        text="Call Royal Clinic on +971 2 555 0100 or email care@clinic.example.",
        requested_mode="fast",
    )

    assert result.model is None
    assert result.effective_mode == "fast"
    assert "+971 2 555 0100" in result.content
    assert "care@clinic.example" in result.content
    assert result.structured["compiler"]["estimated_cost_usd"] == 0


@pytest.mark.asyncio
async def test_ai_compilation_rejects_every_fact_without_verbatim_evidence():
    text = (
        "Royal Clinic\nRoyal Clinic phone number is +971 2 665 9998.\n"
        "Dr Kaveri Amal is a director of Royal Clinic."
    )
    client = _FakeClient(
        {
            "page_type": "directory",
            "entities": [
                {
                    "name": "Royal Clinic",
                    "entity_type": "organization",
                    "evidence": "Royal Clinic",
                }
            ],
            "facts": [
                {
                    "subject": "Royal Clinic",
                    "predicate": "telephone",
                    "value": "+971 2 665 9998",
                    "evidence": "Royal Clinic phone number is +971 2 665 9998.",
                },
                {
                    "subject": "Royal Clinic",
                    "predicate": "chairman",
                    "value": "Invented Person",
                    "evidence": "Invented Person is the chairman.",
                },
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Royal Clinic directory",
        url="https://clinic.example/directory",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )

    assert result.model == "gpt-5.6-terra"
    assert len(result.structured["facts"]) == 1
    assert result.structured["facts"][0]["value"] == "+971 2 665 9998"
    assert result.structured["validation"]["facts_rejected"] == 1
    assert "Invented Person" not in result.content
    assert result.input_tokens == 800
    assert result.output_tokens == 200
    assert result.estimated_cost_usd == pytest.approx(0.004)
    assert result.structured["compiler"]["estimated_cost_aed"] == pytest.approx(0.01469)

    schema = client.completions.requests[0]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"page_type", "entities", "facts"}
    assert set(schema["$defs"]["_Entity"]["required"]) == {
        "name",
        "entity_type",
        "evidence",
    }
    assert set(schema["$defs"]["_Fact"]["required"]) == {
        "subject",
        "predicate",
        "value",
        "evidence",
    }


@pytest.mark.asyncio
async def test_ai_verified_mode_requires_an_openai_key():
    with pytest.raises(KnowledgeCompilerError, match="OpenAI API key"):
        await compile_website_knowledge(
            title="Directory",
            url="https://clinic.example/directory",
            text="Approved directory content long enough to process.",
            requested_mode="ai_verified",
        )
