from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource
from app.services.exact_fact_retrieval import (
    DEFAULT_EVIDENCE_CONTEXT_CHARS,
    MAX_EVIDENCE_CONTEXT_CHARS,
    MAX_INDEX_FACTS,
    MAX_INDEX_SOURCES,
    MIN_EVIDENCE_CONTEXT_CHARS,
    ExactFactIndexCache,
    ExactFactResponseAction,
    ExactFactSource,
    ExactFactType,
    build_exact_fact_index,
    classify_exact_fact_intents,
    load_agent_exact_fact_index_with_diagnostics,
    resolve_exact_fact,
    retrieve_exact_fact,
)
from tests.quality.tier1_fixtures import tier1_exact_fact_index


def _service_source(number: int, *, fact_count: int = 1) -> ExactFactSource:
    facts = []
    for fact_number in range(1, fact_count + 1):
        value = f"Service {number}-{fact_number}"
        facts.append(
            {
                "fact_type": "services",
                "subject": f"Company {number}",
                "predicate": "service offering",
                "value": value,
                "evidence": f"Company {number} offers {value}.",
                "search_phrases": [f"What services does Company {number} offer?"],
            }
        )
    return ExactFactSource(
        source_id=f"source-{number:04d}",
        source_name=f"Source {number}",
        structured_content={"schema_version": "compiler-test-v1", "facts": facts},
    )


def test_index_is_stable_source_grounded_and_carries_provenance():
    source = ExactFactSource(
        source_id="11111111-1111-4111-8111-111111111111",
        source_name="Contact",
        source_url="https://example.test/contact",
        content_sha256="a" * 64,
        compiled_at="2026-09-04T00:00:00+00:00",
        structured_content={
            "schema_version": "compiler-test-v1",
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "primary telephone",
                    "value": "+971 2 665 9998",
                    "evidence": "Al Zaabi Group telephone: +971 2 665 9998.",
                    "search_phrases": ["How can I call Al Zaabi Group?"],
                },
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "business hours",
                    "value": "Open all night",
                    "evidence": "Al Zaabi Group welcomes customers.",
                    "search_phrases": ["Are you open all night?"],
                },
            ],
        },
    )

    first = build_exact_fact_index((source,), revision="revision-1")
    second = build_exact_fact_index((source,), revision="revision-1")

    assert first == second
    assert len(first.facts) == 1  # the ungrounded opening-hours value is rejected
    assert first.index_truncated is True
    assert first.truncation_reasons == ("invalid_structured_fact",)
    fact = first.facts[0]
    assert fact.evidence_id == ("ef1:11111111-1111-4111-8111-111111111111:c1683a776c6f689c")
    assert fact.provenance.source_url == "https://example.test/contact"
    assert fact.provenance.content_sha256 == "a" * 64
    assert fact.provenance.compiler_version == "compiler-test-v1"


@pytest.mark.parametrize(
    ("subject", "value", "evidence"),
    (
        (
            "Al Zaabi Group",
            "+971 2 665 9998",
            "Other Company primary telephone is +971 2 665 9998.",
        ),
        ("Al Zaabi Group", "---", "Al Zaabi Group telephone is unavailable."),
        ("---", "+971 2 665 9998", "The primary telephone is +971 2 665 9998."),
    ),
)
def test_runtime_boundary_rejects_wrong_entity_and_empty_normalized_exact_facts(
    subject,
    value,
    evidence,
):
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="unsafe-import",
                source_name="Imported directory",
                structured_content={
                    "schema_version": "legacy-import-v1",
                    "facts": [
                        {
                            "subject": subject,
                            "predicate": "primary telephone",
                            "value": value,
                            "evidence": evidence,
                            "search_phrases": ["Al Zaabi Group phone number"],
                        }
                    ],
                },
            ),
        )
    )
    result = resolve_exact_fact(index, query="What is Al Zaabi Group's phone number?")

    assert index.facts == ()
    assert index.truncation_reasons == ("invalid_structured_fact",)
    assert result.response_action == ExactFactResponseAction.FALLBACK


def test_index_covers_all_500_crawl_sources_including_sources_after_256():
    sources = tuple(_service_source(number) for number in range(1, MAX_INDEX_SOURCES + 1))

    index = build_exact_fact_index(sources)

    assert index.eligible_source_count == 500
    assert index.indexed_source_count == 500
    assert index.fact_count == 500
    assert index.index_truncated is False
    assert index.truncation_reasons == ()
    indexed_source_ids = {fact.provenance.source_id for fact in index.facts}
    assert "source-0257" in indexed_source_ids
    assert "source-0500" in indexed_source_ids
    result = resolve_exact_fact(
        index,
        query="What services does Company 500 offer?",
    )
    assert result.response_action == ExactFactResponseAction.ANSWER
    assert any(item.value == "Service 500-1" for item in result.evidence)


def test_source_overflow_is_observable_and_partial_index_never_refuses():
    sources = tuple(_service_source(number) for number in range(1, MAX_INDEX_SOURCES + 1))

    # The database loader fetches the bounded rows and separately supplies the
    # full eligible count, so exercise that exact overflow shape.
    index = build_exact_fact_index(sources, eligible_source_count=501)
    result = resolve_exact_fact(
        index,
        query="What services does Company 501 offer?",
    )

    assert index.eligible_source_count == 501
    assert index.indexed_source_count == 500
    assert index.fact_count == 500
    assert index.index_truncated is True
    assert index.truncation_reasons == ("source_limit",)
    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.reason == "exact_fact_index_truncated"


def test_unstructured_or_deterministic_only_source_never_creates_refusal_boundary():
    structured = _service_source(1)
    unstructured = ExactFactSource(
        source_id="pdf-only",
        source_name="Approved PDF",
        structured_content=None,
    )
    deterministic_only = ExactFactSource(
        source_id="fast-text",
        source_name="Fast text",
        structured_content={
            "schema_version": "compiler-test-v1",
            "facts": [],
            "deterministic_contacts": {"phones": ["+971 2 555 0100"]},
            "exact_fact_coverage": {
                "complete": False,
                "reason": "deterministic_extraction_only",
            },
        },
    )

    for incomplete_source in (unstructured, deterministic_only):
        index = build_exact_fact_index((structured, incomplete_source))
        result = resolve_exact_fact(
            index,
            query="What is the approved phone number?",
        )

        assert index.eligible_source_count == 2
        assert index.indexed_source_count == 1
        assert index.index_truncated is True
        assert "incomplete_structured_coverage" in index.truncation_reasons
        assert result.response_action == ExactFactResponseAction.FALLBACK
        assert result.reason == "no_exact_fact_match_use_approved_retrieval"


def test_named_grounded_facts_survive_soft_legacy_coverage_gaps():
    evidence = (
        "Al Zaabi Group operates its businesses in five segments: Healthcare, Trading, "
        "Contracting, Automotive and Transport."
    )
    management = ExactFactSource(
        source_id="management",
        source_name="Management – Al Zaabi Group",
        structured_content={
            "schema_version": "vav-knowledge-compiler-8",
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
                    "predicate": "business segment",
                    "value": value,
                    "evidence": evidence,
                    "search_phrases": [f"Does Al Zaabi Group operate in {value}?"],
                }
                for value in (
                    "Healthcare",
                    "Trading",
                    "Contracting",
                    "Automotive",
                    "Transport",
                )
            ],
            "validation": {
                "all_evidence_source_grounded": True,
                "facts_rejected": 0,
            },
        },
    )
    legacy = ExactFactSource(
        source_id="legacy-page",
        source_name="Legacy approved page",
        structured_content=None,
    )

    result = resolve_exact_fact(
        build_exact_fact_index((management, legacy)),
        query="What businesses or divisions does Al Zaabi Group operate?",
    )

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert result.reason == "verified_exact_fact"
    assert [item.value for item in result.evidence] == [
        "Healthcare, Trading, Contracting, Automotive and Transport"
    ]


def test_role_message_heading_projects_a_grounded_company_leadership_fact():
    evidence = (
        "Chairman's Message. Al Zaabi Group continues its strides towards excellence. "
        "T.R. Vijayakumar"
    )
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="management",
                source_name="Management – Al Zaabi Group",
                structured_content={
                    "schema_version": "vav-knowledge-compiler-8",
                    "entities": [
                        {
                            "name": "Al Zaabi Group",
                            "entity_type": "organization",
                            "evidence": "Al Zaabi Group",
                        }
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
                    "validation": {
                        "all_evidence_source_grounded": True,
                        "facts_rejected": 0,
                    },
                },
            ),
            ExactFactSource(
                source_id="legacy-page",
                source_name="Legacy approved page",
                structured_content=None,
            ),
        )
    )

    result = resolve_exact_fact(index, query="Who is the chairman of Al Zaabi Group?")

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert [(item.subject, item.predicate, item.value) for item in result.evidence] == [
        ("Al Zaabi Group", "chairman", "T.R. Vijayakumar")
    ]


def test_broad_leadership_wording_clarifies_roles_for_any_company():
    company = "Future Example Holdings"
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="future-company-leadership",
                source_name="Approved leadership directory",
                structured_content={
                    "schema_version": "compiler-test-v1",
                    "facts": [
                        {
                            "fact_type": "leadership",
                            "subject": company,
                            "predicate": "chairman",
                            "value": "Amal Rahman",
                            "evidence": f"The chairman of {company} is Amal Rahman.",
                        },
                        {
                            "fact_type": "leadership",
                            "subject": company,
                            "predicate": "president",
                            "value": "David Chen",
                            "evidence": f"The president of {company} is David Chen.",
                        },
                    ],
                },
            ),
        )
    )

    result = resolve_exact_fact(index, query=f"Who is in charge of {company}?")

    assert result.response_action == ExactFactResponseAction.CLARIFY
    assert {(item.predicate, item.value) for item in result.evidence} == {
        ("chairman", "Amal Rahman"),
        ("president", "David Chen"),
    }


def _future_company_leadership_index():
    company = "Future Example Holdings"
    return build_exact_fact_index(
        (
            ExactFactSource(
                source_id="future-company-leadership-corrections",
                source_name="Approved leadership directory",
                structured_content={
                    "schema_version": "compiler-test-v1",
                    "facts": [
                        {
                            "fact_type": "leadership",
                            "subject": company,
                            "predicate": "chairman",
                            "value": "Amal Rahman",
                            "evidence": f"The chairman of {company} is Amal Rahman.",
                        },
                        {
                            "fact_type": "leadership",
                            "subject": company,
                            "predicate": "president",
                            "value": "David Chen",
                            "evidence": f"The president of {company} is David Chen.",
                        },
                    ],
                },
            ),
        )
    )


@pytest.mark.parametrize(
    "query",
    (
        "So who is the chairman of Future Example Holdings?",
        "The chairman of Future Example Holdings is John Smith, right?",
        "I heard John Smith is the chairman of Future Example Holdings, is that correct?",
    ),
)
def test_leadership_followups_and_false_claims_return_the_governed_fact(query):
    result = resolve_exact_fact(_future_company_leadership_index(), query=query)

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert [(item.predicate, item.value) for item in result.evidence] == [
        ("chairman", "Amal Rahman")
    ]


def test_multi_role_caller_claim_returns_both_governed_roles():
    result = resolve_exact_fact(
        _future_company_leadership_index(),
        query=("The chairman and president of Future Example Holdings are both John Smith, right?"),
    )

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert {(item.predicate, item.value) for item in result.evidence} == {
        ("chairman", "Amal Rahman"),
        ("president", "David Chen"),
    }


def test_multi_role_question_first_claim_returns_both_governed_roles():
    result = resolve_exact_fact(
        _future_company_leadership_index(),
        query="Are the chairman and president both David Chen?",
        query_variants=(
            "Future Example Holdings. Are the chairman and president both David Chen?",
        ),
    )

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert {(item.predicate, item.value) for item in result.evidence} == {
        ("chairman", "Amal Rahman"),
        ("president", "David Chen"),
    }


@pytest.mark.parametrize(
    "query",
    (
        "Who oversees Future Example Holdings?",
        "Who heads Future Example Holdings?",
        "Who is responsible for Future Example Holdings?",
        "Who is the decision-maker at Future Example Holdings?",
    ),
)
def test_leadership_paraphrases_enter_the_verified_fact_route(query):
    assert classify_exact_fact_intents(query) == (ExactFactType.LEADERSHIP,)


def test_llm_claimed_complete_but_omitted_fact_never_creates_refusal_boundary():
    # The raw source could contain a chairman or phone value that a generative
    # compiler failed to return. Even a stale/self-reported complete flag must
    # not let the exact-fact accelerator declare that the fact is unavailable.
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="omitted-fact-source",
                source_name="Company profile",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [],
                    "exact_fact_coverage": {"complete": True},
                },
            ),
        )
    )

    result = resolve_exact_fact(index, query="Who is the chairman?")

    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.reason == "no_exact_fact_match_use_approved_retrieval"


def test_non_authoritative_ai_facts_answer_named_entity_but_not_generic_contact_query():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="partial-ai-source",
                source_name="Branch directory",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Branch A",
                            "predicate": "primary telephone",
                            "value": "+971 2 111 1111",
                            "evidence": "Branch A primary telephone is +971 2 111 1111.",
                            "search_phrases": ["Branch A phone number"],
                        }
                    ],
                    "exact_fact_coverage": {
                        "complete": False,
                        "absence_authoritative": False,
                        "returned_facts_validated": True,
                    },
                },
            ),
        )
    )

    named = resolve_exact_fact(index, query="What is the phone number for Branch A?")
    generic = resolve_exact_fact(index, query="What is the phone number?")

    assert index.index_truncated is False
    assert index.absence_authoritative is False
    assert named.response_action == ExactFactResponseAction.ANSWER
    assert generic.response_action == ExactFactResponseAction.FALLBACK
    assert generic.reason == "generic_query_requires_complete_exact_fact_coverage"


def test_authoritative_complete_sources_clarify_between_verified_generic_contacts():
    def contact(source_id: str, subject: str, value: str) -> ExactFactSource:
        evidence = f"{subject} primary telephone is {value}."
        return ExactFactSource(
            source_id=source_id,
            source_name=f"{subject} contact",
            structured_content={
                "schema_version": "compiler-v8",
                "facts": [
                    {
                        "subject": subject,
                        "predicate": "primary telephone",
                        "value": value,
                        "evidence": evidence,
                        "search_phrases": [f"{subject} phone number"],
                    }
                ],
                "exact_fact_coverage": {
                    "complete": True,
                    "absence_authoritative": True,
                },
            },
        )

    index = build_exact_fact_index(
        (
            contact("group-contact", "Al Zaabi Group", "+971 2 111 1111"),
            contact("clinic-contact", "Royal Clinic", "+971 2 222 2222"),
        )
    )
    result = resolve_exact_fact(index, query="What is the phone number?")

    assert index.absence_authoritative is True
    assert result.response_action == ExactFactResponseAction.CLARIFY
    assert result.reason == "ambiguous_verified_exact_facts"
    assert {item.value for item in result.evidence} == {
        "+971 2 111 1111",
        "+971 2 222 2222",
    }


def test_legacy_validated_facts_never_make_omitted_source_facts_absent():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="legacy-ai-source",
                source_name="Legacy company profile",
                structured_content={
                    "schema_version": "knowledge-compiler-v7",
                    "facts": [
                        {
                            "subject": "Branch A",
                            "predicate": "primary telephone",
                            "value": "+971 2 111 1111",
                            "evidence": "Branch A primary telephone is +971 2 111 1111.",
                            "search_phrases": ["Branch A phone number"],
                        }
                    ],
                    "validation": {
                        "all_evidence_source_grounded": True,
                        "facts_rejected": 0,
                    },
                },
            ),
        )
    )

    named = resolve_exact_fact(index, query="What is the phone number for Branch A?")
    omitted = resolve_exact_fact(index, query="Who is the chairman?")

    assert named.response_action == ExactFactResponseAction.ANSWER
    assert index.absence_authoritative is False
    assert omitted.response_action == ExactFactResponseAction.FALLBACK
    assert omitted.reason == "no_exact_fact_match_use_approved_retrieval"


def test_fact_safety_limits_fail_closed_with_observable_reasons():
    oversized_source = build_exact_fact_index((_service_source(1, fact_count=201),))
    globally_oversized = build_exact_fact_index(
        tuple(_service_source(number, fact_count=200) for number in range(1, 12))
    )

    assert oversized_source.fact_count == 200
    assert oversized_source.index_truncated is True
    assert oversized_source.truncation_reasons == ("per_source_fact_limit",)
    assert globally_oversized.fact_count == MAX_INDEX_FACTS
    assert globally_oversized.index_truncated is True
    assert globally_oversized.truncation_reasons == ("index_fact_limit",)
    assert (
        resolve_exact_fact(
            globally_oversized,
            query="What services does Company 11 offer?",
        ).response_action
        == ExactFactResponseAction.FALLBACK
    )


def test_intent_classification_and_combined_contact_answer_are_deterministic():
    index = tier1_exact_fact_index()

    assert classify_exact_fact_intents("Send the contact details for Al Zaabi Group") == (
        ExactFactType.PHONE,
        ExactFactType.ADDRESS,
    )

    result = resolve_exact_fact(
        index,
        query="What is Al Zaabi Group's phone number and address?",
    )

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert result.intents == (ExactFactType.PHONE, ExactFactType.ADDRESS)
    assert {item.fact_type for item in result.evidence} == {
        ExactFactType.PHONE,
        ExactFactType.ADDRESS,
    }


def test_founding_fact_on_partial_corpus_is_source_qualified_and_positive_only():
    partial_index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="management-page",
                source_name="Management",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "inception year",
                            "value": "2003",
                            "evidence": "Al Zaabi Group has operated since its inception in 2003.",
                            "search_phrases": [
                                "When was Al Zaabi Group established?",
                                "When was Al Zaabi Group founded?",
                            ],
                        }
                    ],
                    "exact_fact_coverage": {
                        "complete": True,
                        "absence_authoritative": True,
                    },
                },
            ),
            ExactFactSource(
                source_id="legacy-page",
                source_name="Legacy page without compiler output",
                structured_content=None,
            ),
        )
    )

    partial_match = resolve_exact_fact(
        partial_index,
        query="When was Al Zaabi Group established?",
    )
    complete_index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="management-page",
                source_name="Management",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "inception year",
                            "value": "2003",
                            "evidence": "Al Zaabi Group has operated since its inception in 2003.",
                            "search_phrases": [
                                "When was Al Zaabi Group established?",
                            ],
                        }
                    ],
                    "exact_fact_coverage": {
                        "complete": True,
                        "absence_authoritative": True,
                    },
                },
            ),
            ExactFactSource(
                source_id="other-complete-page",
                source_name="Other compiled page",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [],
                    "exact_fact_coverage": {
                        "complete": True,
                        "absence_authoritative": True,
                    },
                },
            ),
        )
    )
    matched = resolve_exact_fact(
        complete_index,
        query="When was Al Zaabi Group established?",
    )

    assert partial_index.index_truncated is True
    assert partial_index.truncation_reasons == ("incomplete_structured_coverage",)
    assert partial_match.response_action == ExactFactResponseAction.ANSWER
    assert partial_match.reason == "source_qualified_founding_fact"
    assert matched.response_action == ExactFactResponseAction.ANSWER
    assert matched.reason == "source_qualified_founding_fact"
    assert matched.intents == (ExactFactType.FOUNDING,)
    assert matched.evidence[0].value == "2003"


def test_founding_fact_accepts_source_scoped_first_person_corporate_statement():
    evidence = (
        "Al Zaabi Group has come to stay as an integral part of life in the UAE with "
        "its excellent performance and ever expanding presence in the emerging market "
        "segments.\n\nPresident’s Message\n\nStrive for Excellence\n\nThe world is "
        "constantly undergoing changes and market competition is more intense than "
        "ever. We unwaveringly uphold the basic policy of contributing to society "
        "through fair business activities since our inception in 2003."
    )
    fact = {
        "subject": "Al Zaabi Group",
        "predicate": "inception year",
        "value": "2003",
        "evidence": evidence,
    }

    scoped = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="management-page",
                source_name="Management - Al Zaabi Group",
                structured_content={"facts": [fact]},
            ),
        )
    )
    unscoped = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="generic-page",
                source_name="Management",
                structured_content={"facts": [fact]},
            ),
        )
    )
    deceptively_named = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="third-party-page",
                source_name="Beta Holdings profile mentioning Al Zaabi Group",
                structured_content={"facts": [fact]},
            ),
        )
    )

    scoped_result = resolve_exact_fact(
        scoped,
        query="When was Al Zaabi Group established?",
    )
    unscoped_result = resolve_exact_fact(
        unscoped,
        query="When was Al Zaabi Group established?",
    )
    deceptively_named_result = resolve_exact_fact(
        deceptively_named,
        query="When was Al Zaabi Group established?",
    )

    assert scoped_result.response_action == ExactFactResponseAction.ANSWER
    assert scoped_result.reason == "source_qualified_founding_fact"
    assert scoped_result.evidence[0].value == "2003"
    assert unscoped_result.response_action == ExactFactResponseAction.FALLBACK
    assert unscoped_result.evidence == ()
    assert deceptively_named_result.response_action == ExactFactResponseAction.FALLBACK
    assert deceptively_named_result.evidence == ()


def test_founding_classification_rejects_program_dates_and_invalid_conflicts():
    programme = ExactFactSource(
        source_id="programme-page",
        source_name="Programme",
        structured_content={
            "schema_version": "compiler-v8",
            "facts": [
                {
                    "subject": "Acme Group",
                    "predicate": "programme description",
                    "value": "The scholarship programme started in 2020",
                    "evidence": ("Acme Group offers The scholarship programme started in 2020."),
                    "search_phrases": ["When was Acme Group established?"],
                }
            ],
        },
    )
    invalid_conflict = ExactFactSource(
        source_id="conflict-page",
        source_name="Conflicting legacy page",
        structured_content={
            "schema_version": "compiler-v8",
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "inception year",
                    "value": "1999",
                    "evidence": "This excerpt does not ground the entity or date.",
                }
            ],
            "exact_fact_coverage": {
                "complete": True,
                "absence_authoritative": True,
            },
        },
    )
    valid = ExactFactSource(
        source_id="management-page",
        source_name="Management",
        structured_content={
            "schema_version": "compiler-v8",
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "inception year",
                    "value": "2003",
                    "evidence": "Al Zaabi Group has operated since its inception in 2003.",
                }
            ],
            "exact_fact_coverage": {
                "complete": True,
                "absence_authoritative": True,
            },
        },
    )

    programme_result = resolve_exact_fact(
        build_exact_fact_index((programme,)),
        query="When was Acme Group established?",
    )
    conflict_result = resolve_exact_fact(
        build_exact_fact_index((valid, invalid_conflict)),
        query="When was Al Zaabi Group established?",
    )

    assert programme_result.response_action == ExactFactResponseAction.FALLBACK
    assert programme_result.evidence == ()
    assert conflict_result.response_action == ExactFactResponseAction.ANSWER
    assert conflict_result.reason == "source_qualified_founding_fact"
    assert conflict_result.evidence[0].value == "2003"


@pytest.mark.parametrize(
    ("predicate", "value", "evidence"),
    (
        (
            "inception year",
            "scholarship programme began in 2020",
            "Acme Group scholarship programme began in 2020.",
        ),
        (
            "inception year",
            "2020",
            "Acme Group scholarship programme began in 2020.",
        ),
        (
            "programme inception year",
            "2020",
            "Acme Group programme inception year is 2020.",
        ),
    ),
)
def test_founding_fact_rejects_prose_values_subentities_and_non_allowlisted_predicates(
    predicate,
    value,
    evidence,
):
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="programme-page",
                source_name="Programme",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Acme Group",
                            "predicate": predicate,
                            "value": value,
                            "evidence": evidence,
                            "search_phrases": ["When was Acme Group established?"],
                        }
                    ],
                },
            ),
        )
    )

    result = resolve_exact_fact(index, query="When was Acme Group established?")

    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.evidence == ()


@pytest.mark.parametrize(
    "evidence",
    (
        "Al Zaabi Group founded a scholarship programme in 2020.",
        "Al Zaabi Group launched a new product in 2020.",
        "Al Zaabi Group provides services. Beta Holdings was founded in 2020.",
    ),
)
def test_founding_relationship_rejects_active_objects_and_cross_sentence_dates(evidence):
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="misattributed-page",
                source_name="Misattributed date",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "inception year",
                            "value": "2020",
                            "evidence": evidence,
                        }
                    ],
                },
            ),
        )
    )

    result = resolve_exact_fact(index, query="When was Al Zaabi Group established?")

    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.evidence == ()


def test_generated_search_phrase_cannot_redirect_founding_fact_to_another_company():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="management-page",
                source_name="Management",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "inception year",
                            "value": "2003",
                            "evidence": (
                                "Al Zaabi Group has operated since its inception in 2003."
                            ),
                            "search_phrases": ["When was Another Company established?"],
                        }
                    ],
                    "exact_fact_coverage": {
                        "complete": True,
                        "absence_authoritative": True,
                    },
                },
            ),
        )
    )

    result = resolve_exact_fact(index, query="When was Another Company established?")

    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.evidence == ()


def test_operation_duration_wording_resolves_the_verified_founding_year():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="management-page",
                source_name="Management – Al Zaabi Group",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "inception year",
                            "value": "2003",
                            "evidence": (
                                "Al Zaabi Group has operated since its inception in 2003."
                            ),
                        }
                    ],
                },
            ),
        )
    )

    result = resolve_exact_fact(
        index,
        query="How long has Al Zaabi Group been in operation?",
    )

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert result.evidence[0].value == "2003"


def test_governed_hours_paraphrase_does_not_depend_on_generated_search_phrases():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="hours-page",
                source_name="Hours",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "business hours",
                            "value": "Monday to Friday, 8:30 AM to 5:30 PM",
                            "evidence": (
                                "Al Zaabi Group business hours are Monday to Friday, "
                                "8:30 AM to 5:30 PM."
                            ),
                            # This untrusted retrieval hint names the wrong
                            # entity. It must not be needed for the valid
                            # paraphrase or redirect a different-company query.
                            "search_phrases": [
                                "What are Another Company's working timings?",
                            ],
                        }
                    ],
                    "exact_fact_coverage": {
                        "complete": True,
                        "absence_authoritative": True,
                    },
                },
            ),
        )
    )

    matched = resolve_exact_fact(
        index,
        query="What are Al Zaabi Group's working timings?",
    )
    redirected = resolve_exact_fact(
        index,
        query="What are Another Company's working timings?",
    )

    assert matched.response_action == ExactFactResponseAction.ANSWER
    assert matched.intents == (ExactFactType.HOURS,)
    assert matched.evidence[0].value == "Monday to Friday, 8:30 AM to 5:30 PM"
    assert redirected.response_action == ExactFactResponseAction.FALLBACK
    assert redirected.evidence == ()


def test_unknown_predicate_never_enters_exact_answer_lane():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="profile-page",
                source_name="Company profile",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "corporate principle",
                            "value": "Strive for Excellence",
                            "evidence": (
                                "Al Zaabi Group's corporate principle is Strive for Excellence."
                            ),
                            "search_phrases": [
                                "What is Al Zaabi Group's corporate principle?",
                            ],
                        }
                    ],
                },
            ),
        )
    )

    direct_phrase = resolve_exact_fact(
        index,
        query="What is Al Zaabi Group's corporate principle?",
    )
    ambiguous_short_query = resolve_exact_fact(
        index,
        query="What is Al Zaabi Group?",
    )

    assert direct_phrase.response_action == ExactFactResponseAction.FALLBACK
    assert direct_phrase.evidence == ()
    assert ambiguous_short_query.response_action == ExactFactResponseAction.FALLBACK
    assert ambiguous_short_query.evidence == ()


def test_search_phrase_cannot_invent_an_intent_or_bypass_dynamic_fact_policy():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="profile-page",
                source_name="Company profile",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "primary telephone",
                            "value": "+971 2 665 9998",
                            "evidence": ("Al Zaabi Group primary telephone is +971 2 665 9998."),
                            "search_phrases": [
                                "What is Al Zaabi Group's phone number?",
                            ],
                        },
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "current stock availability",
                            "value": "Available",
                            "evidence": "Al Zaabi Group current stock availability is Available.",
                            "search_phrases": [
                                "What is current Al Zaabi Group stock availability?",
                            ],
                        },
                    ],
                },
            ),
        )
    )

    underspecified = resolve_exact_fact(index, query="What is Al Zaabi Group?")
    dynamic = resolve_exact_fact(
        index,
        query="What is current Al Zaabi Group stock availability?",
    )

    assert underspecified.response_action == ExactFactResponseAction.FALLBACK
    assert underspecified.intents == ()
    assert dynamic.response_action == ExactFactResponseAction.FALLBACK
    assert dynamic.intents == ()


def test_partial_multi_intent_match_uses_approved_retrieval_instead_of_false_ambiguity():
    index = build_exact_fact_index(
        (
            ExactFactSource(
                source_id="phone-only",
                source_name="Phone directory",
                structured_content={
                    "schema_version": "compiler-v8",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "primary telephone",
                            "value": "+971 2 665 9998",
                            "evidence": "Al Zaabi Group primary telephone is +971 2 665 9998.",
                            "search_phrases": ["Al Zaabi Group phone number"],
                        }
                    ],
                    "exact_fact_coverage": {
                        "complete": True,
                        "absence_authoritative": True,
                    },
                },
            ),
        )
    )

    result = resolve_exact_fact(
        index,
        query="What is Al Zaabi Group's phone number and address?",
    )

    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.reason == "partial_exact_fact_match_use_approved_retrieval"
    assert result.evidence == ()


@pytest.mark.parametrize(
    "limit",
    (MIN_EVIDENCE_CONTEXT_CHARS, DEFAULT_EVIDENCE_CONTEXT_CHARS, MAX_EVIDENCE_CONTEXT_CHARS),
)
def test_evidence_bundle_respects_configured_bound_and_keeps_value(limit):
    long_prefix = "Verified reception and contact information. " * 30
    phone = "+971 2 665 9998"
    source = ExactFactSource(
        source_id="source-long",
        source_name="A very long but verified contact page title",
        source_url="https://example.test/contact?source=verified",
        structured_content={
            "facts": [
                {
                    "subject": "Al Zaabi Group",
                    "predicate": "primary telephone",
                    "value": phone,
                    "evidence": f"{long_prefix} Al Zaabi Group telephone: {phone}.",
                    "search_phrases": ["What is Al Zaabi Group's phone number?"],
                }
            ]
        },
    )
    result = resolve_exact_fact(
        build_exact_fact_index((source,)),
        query="What is Al Zaabi Group's phone number?",
        max_evidence_chars=limit,
    )

    assert result.response_action == ExactFactResponseAction.ANSWER
    assert result.evidence_context is not None
    assert len(result.evidence_context) <= limit
    assert len(result.evidence[0].quote) <= limit
    assert phone in result.evidence_context
    assert result.evidence[0].evidence_id in result.evidence_context


@pytest.mark.parametrize("limit", (599, 901))
def test_evidence_bundle_rejects_out_of_contract_limits(limit):
    with pytest.raises(ValueError, match="between 600 and 900"):
        resolve_exact_fact(
            tier1_exact_fact_index(),
            query="What is the Al Zaabi Group phone number?",
            max_evidence_chars=limit,
        )


def test_exact_fact_cache_is_ttl_and_lru_bounded():
    now = [100.0]
    cache = ExactFactIndexCache(max_entries=2, ttl_seconds=10, clock=lambda: now[0])
    one = tier1_exact_fact_index()
    two = build_exact_fact_index((), revision="two")
    three = build_exact_fact_index((), revision="three")

    cache.remember("one", one)
    cache.remember("two", two)
    assert cache.get("one") is one
    cache.remember("three", three)

    assert cache.get("two") is None
    assert cache.get("three") is three
    now[0] = 110.0
    assert cache.get("one") is None


class _DatabaseMustNotBeUsed:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("ordinary non-Tier-1 query touched the database")


@pytest.mark.parametrize(
    "query",
    (
        "Please summarize the latest revenue report.",
        "Please call me tomorrow.",
        "Can you open an account?",
        "Who is your insurance provider?",
    ),
)
@pytest.mark.asyncio
async def test_non_tier1_query_skips_database_and_reports_diagnostics(query):
    result = await retrieve_exact_fact(
        _DatabaseMustNotBeUsed(),  # type: ignore[arg-type]
        tenant_id="11111111-1111-4111-8111-111111111111",  # type: ignore[arg-type]
        agent_id="22222222-2222-4222-8222-222222222222",  # type: ignore[arg-type]
        query=query,
    )

    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.diagnostics is not None
    assert result.diagnostics.skipped_database is True
    assert result.diagnostics.load is None
    assert result.diagnostics.preclassification_ms >= 0
    assert result.diagnostics.total_ms >= result.diagnostics.preclassification_ms


def test_preclassification_keeps_specific_intents_and_lowercase_person_names():
    assert classify_exact_fact_intents("Can you provide your address?") == (ExactFactType.ADDRESS,)
    assert classify_exact_fact_intents("who is devu vimal?") == (ExactFactType.LEADERSHIP,)
    assert classify_exact_fact_intents("Who is Saeed Yousif Ibrahim Al Zaabi?") == (
        ExactFactType.LEADERSHIP,
    )


@pytest.mark.parametrize("limit", (599, 901))
@pytest.mark.asyncio
async def test_early_fallback_still_validates_evidence_bound(limit):
    with pytest.raises(ValueError, match="between 600 and 900"):
        await retrieve_exact_fact(
            _DatabaseMustNotBeUsed(),  # type: ignore[arg-type]
            tenant_id="11111111-1111-4111-8111-111111111111",  # type: ignore[arg-type]
            agent_id="22222222-2222-4222-8222-222222222222",  # type: ignore[arg-type]
            query="Please summarize the latest revenue report.",
            max_evidence_chars=limit,
        )


@pytest.mark.asyncio
async def test_approved_database_index_is_cached_and_exposes_stage_timings(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Exact fact agent",
        system_prompt="Answer only from approved evidence.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved facts",
        approval_status="approved",
        is_active=True,
    )
    db.add_all((agent, knowledge_base))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Contact",
            location="https://example.test/contact",
            status="indexed",
            content="Al Zaabi Group telephone: +971 2 665 9998.",
            content_sha256="c" * 64,
            compiled_at=datetime.now(UTC),
            structured_content={
                "schema_version": "compiler-test-v1",
                "facts": [
                    {
                        "subject": "Al Zaabi Group",
                        "predicate": "primary telephone",
                        "value": "+971 2 665 9998",
                        "evidence": "Al Zaabi Group telephone: +971 2 665 9998.",
                        "search_phrases": ["What is Al Zaabi Group's phone number?"],
                    }
                ],
            },
        )
    )
    await db.commit()
    cache = ExactFactIndexCache()

    cold = await load_agent_exact_fact_index_with_diagnostics(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        cache=cache,
    )
    warm = await load_agent_exact_fact_index_with_diagnostics(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        cache=cache,
    )

    assert cold.index is not None
    assert warm.index is cold.index
    assert cold.diagnostics.cache_hit is False
    assert cold.diagnostics.source_count == 1
    assert cold.diagnostics.fact_count == 1
    assert cold.diagnostics.source_load_ms >= 0
    assert cold.diagnostics.index_build_ms >= 0
    assert warm.diagnostics.cache_hit is True
    assert warm.diagnostics.source_load_ms == 0
    assert warm.diagnostics.index_build_ms == 0

    resolved = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is Al Zaabi Group's phone number?",
        cache=cache,
    )
    assert resolved.response_action == ExactFactResponseAction.ANSWER
    assert resolved.diagnostics is not None
    assert resolved.diagnostics.load is not None
    assert resolved.diagnostics.load.cache_hit is True
    assert resolved.diagnostics.resolution_ms >= 0


@pytest.mark.asyncio
async def test_database_loader_covers_all_500_sources_without_silent_cutoff(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Full crawl exact fact agent",
        system_prompt="Answer only from approved evidence.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Full 500 page crawl",
        approval_status="approved",
        is_active=True,
    )
    db.add_all((agent, knowledge_base))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    compiled_at = datetime.now(UTC)
    sources = []
    for number in range(1, MAX_INDEX_SOURCES + 1):
        value = f"Service {number}-1"
        sources.append(
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                source_type="website",
                name=f"Page {number}",
                location=f"https://example.test/page-{number}",
                status="indexed",
                content=f"Company {number} offers {value}.",
                content_sha256=f"{number:064x}",
                compiled_at=compiled_at,
                structured_content={
                    "schema_version": "compiler-test-v1",
                    "facts": [
                        {
                            "fact_type": "services",
                            "subject": f"Company {number}",
                            "predicate": "service offering",
                            "value": value,
                            "evidence": f"Company {number} offers {value}.",
                            "search_phrases": [f"What services does Company {number} offer?"],
                        }
                    ],
                },
            )
        )
    db.add_all(sources)
    await db.commit()

    loaded = await load_agent_exact_fact_index_with_diagnostics(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        cache=None,
    )

    assert loaded.index is not None
    assert loaded.index.eligible_source_count == 500
    assert loaded.index.indexed_source_count == 500
    assert loaded.index.fact_count == 500
    assert loaded.index.index_truncated is False
    assert loaded.diagnostics.eligible_source_count == 500
    assert loaded.diagnostics.indexed_source_count == 500
    assert loaded.diagnostics.fact_count == 500
    assert loaded.diagnostics.index_truncated is False
    subjects = {fact.subject for fact in loaded.index.facts}
    assert "Company 257" in subjects
    assert "Company 500" in subjects

    overflow_value = "Service 501-1"
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Page 501",
            location="https://example.test/page-501",
            status="indexed",
            content=f"Company 501 offers {overflow_value}.",
            content_sha256=f"{501:064x}",
            compiled_at=compiled_at,
            structured_content={
                "schema_version": "compiler-test-v1",
                "facts": [
                    {
                        "fact_type": "services",
                        "subject": "Company 501",
                        "predicate": "service offering",
                        "value": overflow_value,
                        "evidence": f"Company 501 offers {overflow_value}.",
                        "search_phrases": ["What services does Company 501 offer?"],
                    }
                ],
            },
        )
    )
    await db.commit()

    overflow = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What services does Company 501 offer?",
        cache=None,
    )

    assert overflow.response_action == ExactFactResponseAction.FALLBACK
    assert overflow.reason == "exact_fact_index_truncated"
    assert overflow.diagnostics is not None
    assert overflow.diagnostics.load is not None
    assert overflow.diagnostics.load.eligible_source_count == 501
    assert overflow.diagnostics.load.indexed_source_count == 500
    assert overflow.diagnostics.load.fact_count == 500
    assert overflow.diagnostics.load.index_truncated is True
    assert overflow.diagnostics.load.truncation_reasons == ("source_limit",)


@pytest.mark.asyncio
async def test_database_loader_falls_back_when_approved_pdf_lacks_structured_coverage(
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Mixed knowledge agent",
        system_prompt="Answer only from approved evidence.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Mixed PDF and structured knowledge",
        approval_status="approved",
        is_active=True,
    )
    db.add_all((agent, knowledge_base))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add_all(
        (
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                source_type="website",
                name="Structured contact",
                status="indexed",
                content="Al Zaabi Group telephone: +971 2 665 9998.",
                structured_content={
                    "schema_version": "compiler-test-v1",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "primary telephone",
                            "value": "+971 2 665 9998",
                            "evidence": "Al Zaabi Group telephone: +971 2 665 9998.",
                            "search_phrases": ["Al Zaabi phone number"],
                        }
                    ],
                },
            ),
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                source_type="file",
                name="Approved directory.pdf",
                status="indexed",
                content="A second approved telephone may be listed in this PDF.",
                structured_content=None,
            ),
        )
    )
    await db.commit()

    loaded = await load_agent_exact_fact_index_with_diagnostics(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        cache=None,
    )
    result = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        cache=None,
    )

    assert loaded.index is not None
    assert loaded.index.eligible_source_count == 2
    assert loaded.index.indexed_source_count == 1
    assert loaded.index.index_truncated is True
    assert loaded.index.truncation_reasons == ("incomplete_structured_coverage",)
    assert result.response_action == ExactFactResponseAction.FALLBACK
    assert result.reason == "exact_fact_index_truncated"
