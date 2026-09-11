import json
import re
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
    assert result.structured["compiler"]["version"] == "vav-knowledge-compiler-14"


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


def test_failed_automatic_compilation_names_the_reason():
    from app.services.knowledge_compiler import _failure_reason

    class RateLimitedError(Exception):
        status_code = 429

    assert _failure_reason(RateLimitedError("too many requests")) == "RateLimitedError 429"
    assert _failure_reason(TimeoutError()) == "TimeoutError"


def _segment_payload(index: int) -> dict:
    return {
        "page_type": "directory",
        "entities": [],
        "facts": [
            {
                "subject": f"Dr Number{index}",
                "predicate": "specialty",
                "value": "General Practitioner",
                "evidence": f"Dr Number{index} | General Practitioner",
                "search_phrases": [f"Who is Dr Number{index}?"],
            }
        ],
    }


class _SegmentAwareCompletions:
    """Answers each segment with the fact for the doctor that segment contains."""

    def __init__(self):
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        text = json.loads(kwargs["messages"][1]["content"])["source_text"]
        indexes = [int(match) for match in re.findall(r"Dr Number(\d+)", text)]
        payload = {
            "page_type": "directory",
            "entities": [],
            "facts": [fact for index in indexes for fact in _segment_payload(index)["facts"]],
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=40),
        )


@pytest.mark.asyncio
async def test_large_pages_compile_in_record_aligned_segments(monkeypatch):
    from app.services import knowledge_compiler

    monkeypatch.setattr(knowledge_compiler, "_SEGMENT_CHARS", 120)
    records = [f"Dr Number{index} | General Practitioner" for index in range(1, 9)]
    text = "\n\n".join(records)
    completions = _SegmentAwareCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await compile_website_knowledge(
        title="Our Doctors",
        url="https://clinic.example/doctors",
        text=text,
        requested_mode="ai_verified",
        api_key="fake",
        client=client,
    )

    assert len(completions.requests) >= 3
    for request in completions.requests:
        segment = json.loads(request["messages"][1]["content"])["source_text"]
        assert len(segment) <= 120
        assert not segment.startswith("|") and not segment.endswith("|")  # records intact
    assert sorted(fact["subject"] for fact in result.structured["facts"]) == sorted(
        f"Dr Number{index}" for index in range(1, 9)
    )
    assert result.structured["validation"]["facts_accepted"] == 8
    assert result.structured["exact_fact_coverage"]["segments_processed"] >= 3


@pytest.mark.asyncio
async def test_cut_off_model_reply_is_reported_not_parsed():
    class TruncatedCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"page_type": "directory", "fac'),
                        finish_reason="length",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=16_000),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=TruncatedCompletions()))
    with pytest.raises(KnowledgeCompilerError) as failure:
        await compile_website_knowledge(
            title="Home",
            url="https://clinic.example/",
            text="Royal Medical Center | One Day Surgery",
            requested_mode="ai_verified",
            api_key="fake",
            client=client,
        )
    assert "cut off" in str(failure.value)

    from app.services.knowledge_compiler import _failure_reason

    assert _failure_reason(failure.value).startswith("KnowledgeCompilerError: AI reply was cut off")


def test_segments_after_the_first_open_with_the_active_heading_context():
    from app.services.knowledge_compiler import _record_segments

    doctors = [f"Dr Number{index} | General Practitioner" for index in range(1, 7)]
    nurses = [f"Nurse Number{index} | Paediatrics" for index in range(1, 7)]
    text = "\n\n".join(["Royal Medical Center", "Our Doctors", *doctors, "Our Nurses", *nurses])

    segments = _record_segments(text, limit=170)

    assert len(segments) >= 3
    assert segments[0].startswith("Royal Medical Center\n\nOur Doctors")
    for segment in segments[1:]:
        assert segment.startswith("Royal Medical Center\n\n")
    doctor_segments = [segment for segment in segments if "Dr Number" in segment]
    nurse_segments = [segment for segment in segments if "Nurse Number" in segment]
    assert all("Our Doctors" in segment for segment in doctor_segments)
    assert all("Our Nurses" in segment for segment in nurse_segments[1:] or nurse_segments)
    # Every record appears exactly once across the segments.
    joined = "\n\n".join(segments)
    for record in doctors + nurses:
        assert joined.count(record) == 1


class _SequencedCompletions:
    """Return the first payload to the main pass and the second to the focused pass."""

    def __init__(self, main: dict, focused: dict | None, *, fail_focused: bool = False):
        self.main = main
        self.focused = focused
        self.fail_focused = fail_focused
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        system_prompt = kwargs["messages"][0]["content"]
        if "FOCUSED PASS" in system_prompt:
            if self.fail_focused:
                raise TimeoutError("focused pass timed out")
            payload = self.focused
        else:
            payload = self.main
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def _departments_page():
    from app.services.knowledge_records import make_record, render_records

    records = [
        make_record("heading", ["Royal Medical Center Abu Dhabi"]),
        make_record("heading", ["Departments"], heading_path=("Royal Medical Center Abu Dhabi",)),
        make_record(
            "list_item",
            ["Radiology"],
            heading_path=("Royal Medical Center Abu Dhabi", "Departments"),
        ),
        make_record(
            "list_item",
            ["Dentistry"],
            heading_path=("Royal Medical Center Abu Dhabi", "Departments"),
        ),
    ]
    return records, render_records(records)


def _main_payload():
    return {
        "page_type": "service",
        "entities": [
            {
                "name": "Royal Medical Center Abu Dhabi",
                "entity_type": "organization",
                "evidence": "Royal Medical Center Abu Dhabi",
            }
        ],
        "facts": [
            {
                "subject": "Royal Medical Center Abu Dhabi",
                "predicate": "department",
                "value": "Radiology",
                "evidence": "Royal Medical Center Abu Dhabi\n\nDepartments\n\nRadiology",
                "search_phrases": ["Does Royal Medical Center have radiology?"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_focused_pass_compiles_only_the_uncovered_records_with_context():
    records, text = _departments_page()
    completions = _SequencedCompletions(
        _main_payload(),
        {
            "page_type": "service",
            "entities": [],
            "facts": [
                {
                    "subject": "Royal Medical Center Abu Dhabi",
                    "predicate": "department",
                    "value": "Dentistry",
                    "evidence": (
                        "Departments - Royal Medical Center Abu Dhabi › "
                        "Royal Medical Center Abu Dhabi › Departments › Dentistry"
                    ),
                    "search_phrases": ["Does Royal Medical Center have a dentistry department?"],
                }
            ],
        },
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await compile_website_knowledge(
        title="Departments | Royal Medical Center Abu Dhabi",
        url="https://royalmedical.example/departments",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
        records=records,
    )

    assert len(completions.requests) == 2
    focused_payload = json.loads(completions.requests[1]["messages"][1]["content"])
    # Only Dentistry was uncovered; Radiology was captured in the first pass.
    assert focused_payload["source_text"] == (
        "Departments - Royal Medical Center Abu Dhabi › "
        "Royal Medical Center Abu Dhabi › Departments › Dentistry"
    )
    values = {fact["value"] for fact in result.structured["facts"]}
    assert values == {"Radiology", "Dentistry"}
    assert result.structured["focused_pass"] == {
        "uncovered_records": 1,
        "facts_added": 1,
        "segments_processed": 1,
    }
    assert result.structured["validation"]["facts_accepted"] == 2
    assert result.input_tokens == 200 and result.output_tokens == 100
    from app.services.knowledge_records import coverage_report

    coverage = coverage_report(
        records, result.structured, requested_mode="ai_verified", effective_mode="ai_verified"
    )
    assert coverage["status"] == "complete"
    assert coverage["records_covered"] == 2


@pytest.mark.asyncio
async def test_focused_pass_is_skipped_when_every_record_is_covered():
    records, text = _departments_page()
    payload = _main_payload()
    payload["facts"].append(
        {
            "subject": "Royal Medical Center Abu Dhabi",
            "predicate": "department",
            "value": "Dentistry",
            "evidence": "Royal Medical Center Abu Dhabi\n\nDepartments\n\nRadiology\n\nDentistry",
            "search_phrases": ["Does Royal Medical Center have dentistry?"],
        }
    )
    completions = _SequencedCompletions(payload, None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await compile_website_knowledge(
        title="Departments | Royal Medical Center Abu Dhabi",
        url="https://royalmedical.example/departments",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
        records=records,
    )

    assert len(completions.requests) == 1
    assert "focused_pass" not in result.structured
    assert len(result.structured["facts"]) == 2


@pytest.mark.asyncio
async def test_focused_pass_failure_keeps_the_first_pass_facts():
    records, text = _departments_page()
    completions = _SequencedCompletions(_main_payload(), None, fail_focused=True)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await compile_website_knowledge(
        title="Departments | Royal Medical Center Abu Dhabi",
        url="https://royalmedical.example/departments",
        text=text,
        requested_mode="ai_verified",
        api_key="test-key",
        client=client,
        records=records,
    )

    assert len(completions.requests) == 2
    assert result.effective_mode == "ai_verified"
    assert [fact["value"] for fact in result.structured["facts"]] == ["Radiology"]
    assert result.structured["focused_pass"] == {
        "uncovered_records": 1,
        "facts_added": 0,
        "error": "TimeoutError",
    }
