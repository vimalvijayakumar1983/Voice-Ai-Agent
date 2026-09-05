import json
from types import SimpleNamespace

import pytest

from app.services.knowledge_compiler import (
    KnowledgeCompilerError,
    _Fact,
    _validated_fact,
    compile_website_knowledge,
)
from app.services.knowledge_retrieval import rank_knowledge


def test_person_profile_predicate_must_be_grounded_too():
    source = "Jane Doe is Director of Harbour Group."
    fields = dict(
        subject="Harbour Group",
        predicate="person profile: Jane Doe",
        value="Director",
        evidence=source,
        search_phrases=["Who is Jane Doe?"],
    )
    assert _validated_fact(source, _Fact(**fields)) is not None
    fields["predicate"] = "person profile: John Smith"
    assert _validated_fact(source, _Fact(**fields)) is None


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
    assert result.structured["exact_fact_coverage"] == {
        "complete": False,
        "reason": "deterministic_extraction_only",
    }


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
                    "search_phrases": ["Royal Clinic phone contact telephone number"],
                },
                {
                    "subject": "Royal Clinic",
                    "predicate": "chairman",
                    "value": "Invented Person",
                    "evidence": "Invented Person is the chairman.",
                    "search_phrases": ["Royal Clinic chairman leadership"],
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
    assert result.structured["exact_fact_coverage"] == {
        "complete": False,
        "absence_authoritative": False,
        "returned_facts_validated": False,
        "reason": "partial_or_rejected_ai_extraction",
    }
    assert result.structured["speech_entities"] == [
        {
            "canonical": "Royal Clinic",
            "entity_type": "organization",
            "language": "und",
            "critical": True,
            "aliases": [],
            "evidence_sha256": ("6ed39545b247ac9841a32a6cdd6e5df7e893a28ef885236c5c47d7a8ff78d6c8"),
        }
    ]
    assert "Invented Person" not in result.content
    assert result.input_tokens == 800
    assert result.output_tokens == 200
    assert result.estimated_cost_usd == pytest.approx(0.004)
    assert result.structured["compiler"]["estimated_cost_aed"] == pytest.approx(0.01469)
    assert "SUBJECT: Royal Clinic" in result.content
    assert "CONTACT VALUES FOUND ON THIS PAGE" not in result.content

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
        "search_phrases",
    }


@pytest.mark.asyncio
async def test_ai_compilation_keeps_natural_questions_with_verified_fact():
    text = "Al Zaabi Group has grown steadily since our inception in 2003."
    client = _FakeClient(
        {
            "page_type": "overview",
            "entities": [
                {
                    "name": "Al Zaabi Group",
                    "entity_type": "organization",
                    "evidence": "Al Zaabi Group",
                }
            ],
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "inception year",
                    "value": "2003",
                    "evidence": ("Al Zaabi Group has grown steadily since our inception in 2003."),
                    "search_phrases": [
                        "when was Al Zaabi Group formed",
                        "company founding year established inception",
                    ],
                }
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Management – Al Zaabi Group",
        url="https://alzaabigroup.example/management",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )
    matches = rank_knowledge(
        "When was Al Zaabi Group formed?",
        [("Management – Al Zaabi Group", result.content)],
    )

    assert matches
    assert result.structured["exact_fact_coverage"] == {
        "complete": False,
        "absence_authoritative": False,
        "returned_facts_validated": True,
        "reason": "validated_ai_facts_without_absence_audit",
    }
    assert "inception year: 2003" in matches[0].text
    assert "when was Al Zaabi Group formed" in matches[0].text


@pytest.mark.asyncio
async def test_ai_compilation_projects_explicit_role_message_heading():
    evidence = (
        "Chairman's Message\n\nA Winning Combination of Minds\n\n"
        "Al Zaabi Group will continue its strides towards excellence.\n\n"
        "T.R. Vijayakumar"
    )
    client = _FakeClient(
        {
            "page_type": "overview",
            "entities": [
                {
                    "name": "Al Zaabi Group",
                    "entity_type": "organization",
                    "evidence": "Al Zaabi Group",
                },
                {
                    "name": "T.R. Vijayakumar",
                    "entity_type": "person",
                    "evidence": "T.R. Vijayakumar",
                },
            ],
            "facts": [
                {
                    "subject": "T.R. Vijayakumar",
                    "predicate": "message title",
                    "value": "Chairman's Message",
                    "evidence": evidence,
                    "search_phrases": ["Who gave the Chairman's Message?"],
                }
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Management – Al Zaabi Group",
        url="https://alzaabigroup.example/management",
        text=evidence,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )

    assert any(
        fact["subject"] == "Al Zaabi Group"
        and fact["predicate"] == "chairman"
        and fact["value"] == "T.R. Vijayakumar"
        for fact in result.structured["facts"]
    )
    assert result.structured["compiler"]["version"] == "vav-knowledge-compiler-11"


@pytest.mark.asyncio
async def test_ai_compilation_accepts_pronoun_fact_from_same_subject_paragraph():
    text = (
        "Al Zaabi Group has grown throughout the UAE.\n\nPresident's Message\n\n"
        "Strive for Excellence\n\nThe market changes constantly. "
        "We have conducted fair business activities since our inception in 2003."
    )
    client = _FakeClient(
        {
            "page_type": "overview",
            "entities": [
                {
                    "name": "Al Zaabi Group",
                    "entity_type": "organization",
                    "evidence": "Al Zaabi Group",
                }
            ],
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "inception year",
                    "value": "2003",
                    "evidence": (
                        "We have conducted fair business activities since our inception in 2003."
                    ),
                    "search_phrases": [
                        "when was Al Zaabi Group formed",
                        "Al Zaabi Group founding year inception",
                    ],
                }
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Management – Al Zaabi Group",
        url="https://alzaabigroup.example/management",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )

    assert result.structured["validation"]["facts_accepted"] == 1
    assert result.structured["facts"][0]["value"] == "2003"
    assert result.structured["facts"][0]["evidence"].startswith("Al Zaabi Group")


@pytest.mark.asyncio
async def test_ai_compilation_separates_multi_organization_contact_facts():
    text = (
        "Al Zaabi Group. Office 403, Al Reem Plaza, Abu Dhabi. "
        "Tel: +971 2 665 9998.\n\n"
        "Adam and Eve Medical Center. Pink Building, Abu Dhabi. "
        "Tel: +971 2 6767 366."
    )
    client = _FakeClient(
        {
            "page_type": "contact",
            "entities": [],
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "address and telephone",
                    "value": "Office 403, Al Reem Plaza, Abu Dhabi; +971 2 665 9998",
                    "evidence": (
                        "Al Zaabi Group. Office 403, Al Reem Plaza, Abu Dhabi. "
                        "Tel: +971 2 665 9998."
                    ),
                    "search_phrases": ["Al Zaabi Group address phone contact"],
                },
                {
                    "subject": "Adam and Eve Medical Center",
                    "predicate": "address and telephone",
                    "value": "Pink Building, Abu Dhabi; +971 2 6767 366",
                    "evidence": (
                        "Adam and Eve Medical Center. Pink Building, Abu Dhabi. "
                        "Tel: +971 2 6767 366."
                    ),
                    "search_phrases": ["Adam and Eve Medical Center address phone"],
                },
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Contact – Al Zaabi Group",
        url="https://www.alzaabigroup.com/contact/",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )
    matches = rank_knowledge(
        "What is the phone number for Al Zaabi Group?",
        [("Contact – Al Zaabi Group", result.content)],
    )

    assert matches
    assert "+971 2 665 9998" in matches[0].text
    assert all("+971 2 6767 366" not in match.text for match in matches)


@pytest.mark.asyncio
async def test_contact_heading_context_keeps_address_and_phone_in_one_subject_chunk():
    text = (
        "Al Zaabi Group\n\n"
        "Office No 403 & 404 Al Reem Plaza, Electra Street, Abu Dhabi UAE "
        "Tel: +971 2 665 9998\n\n"
        "Adam & Eve Specialized Medical Center\n\n"
        "Pink Building, Abu Dhabi UAE Tel: +971 2 6767 366"
    )
    client = _FakeClient(
        {
            "page_type": "contact",
            "entities": [],
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "physical address",
                    "value": ("Office No 403 & 404, Al Reem Plaza, Electra Street, Abu Dhabi UAE"),
                    "evidence": (
                        "Office No 403 & 404 Al Reem Plaza, Electra Street, Abu Dhabi UAE"
                    ),
                    "search_phrases": ["Al Zaabi Group address location where based"],
                },
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "primary telephone",
                    "value": "+971 2 665 9998",
                    "evidence": (
                        "Office No 403 & 404 Al Reem Plaza, Electra Street, Abu Dhabi UAE "
                        "Tel: +971 2 665 9998"
                    ),
                    "search_phrases": ["Al Zaabi Group phone contact telephone number"],
                },
                {
                    "subject": "Adam & Eve Specialized Medical Center",
                    "predicate": "primary telephone",
                    "value": "+971 2 6767 366",
                    "evidence": "Pink Building, Abu Dhabi UAE Tel: +971 2 6767 366",
                    "search_phrases": ["Adam and Eve Medical Center phone contact"],
                },
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Contact – Al Zaabi Group",
        url="https://www.alzaabigroup.com/contact/",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )
    matches = rank_knowledge(
        "What is the contact address and phone number for Al Zaabi Group?",
        [("Contact – Al Zaabi Group", result.content)],
    )

    assert len(result.structured["facts"]) == 3
    assert matches
    assert "Office No 403 & 404, Al Reem Plaza" in matches[0].text
    assert "+971 2 665 9998" in matches[0].text
    assert "+971 2 6767 366" not in matches[0].text
    al_zaabi_section = result.content.split("SUBJECT: Adam & Eve Specialized Medical Center", 1)[0]
    assert "\n\nOffice No 403" not in al_zaabi_section


@pytest.mark.asyncio
async def test_contact_fact_rejects_unassociated_value_from_later_page_block():
    text = "Al Zaabi Group\n\nOffice 403, Abu Dhabi.\n\nOther Company\n\nTel: +971 2 111 2222"
    client = _FakeClient(
        {
            "page_type": "contact",
            "entities": [],
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "primary telephone",
                    "value": "+971 2 111 2222",
                    "evidence": "Tel: +971 2 111 2222",
                    "search_phrases": ["Al Zaabi Group phone contact"],
                }
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Contact – Al Zaabi Group",
        url="https://www.alzaabigroup.com/contact/",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )

    assert result.structured["facts"] == []
    assert result.structured["validation"]["facts_rejected"] == 1


@pytest.mark.asyncio
async def test_contact_fact_cannot_borrow_subject_from_earlier_flattened_directory_text():
    text = (
        "Al Zaabi Group provides corporate services. Other Company reception telephone "
        "is +971 2 111 2222 and serves Abu Dhabi."
    )
    client = _FakeClient(
        {
            "page_type": "directory",
            "entities": [],
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "primary telephone",
                    "value": "+971 2 111 2222",
                    "evidence": "Other Company reception telephone is +971 2 111 2222",
                    "search_phrases": ["Al Zaabi Group phone contact"],
                }
            ],
        }
    )

    result = await compile_website_knowledge(
        title="Flattened group directory",
        url="https://www.alzaabigroup.com/directory/",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
    )

    assert result.structured["facts"] == []
    assert result.structured["validation"]["facts_rejected"] == 1


@pytest.mark.asyncio
async def test_ai_verified_mode_requires_an_openai_key():
    with pytest.raises(KnowledgeCompilerError, match="OpenAI API key"):
        await compile_website_knowledge(
            title="Directory",
            url="https://clinic.example/directory",
            text="Approved directory content long enough to process.",
            requested_mode="ai_verified",
        )
