"""Source-grounded Tier-1 facts and caller utterance families."""

from __future__ import annotations

from app.services.exact_fact_retrieval import (
    ExactFactIndex,
    ExactFactResponseAction,
    ExactFactSource,
    ExactFactType,
    build_exact_fact_index,
)

AL_ZAABI_SOURCE_ID = "11111111-1111-4111-8111-111111111111"
CLINIC_SOURCE_ID = "22222222-2222-4222-8222-222222222222"


def tier1_exact_fact_index() -> ExactFactIndex:
    return build_exact_fact_index(
        (
            ExactFactSource(
                source_id=AL_ZAABI_SOURCE_ID,
                source_name="Verified Al Zaabi contact and management",
                source_url="https://example.test/al-zaabi/contact",
                content_sha256="a" * 64,
                compiled_at="2026-09-04T00:00:00+00:00",
                structured_content={
                    "schema_version": "tier1-fixture-v1",
                    "facts": [
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "primary telephone",
                            "value": "+971 2 665 9998",
                            "evidence": (
                                "Al Zaabi Group, Office No 403 & 404 Al Reem Plaza, "
                                "Electra Street, Abu Dhabi UAE. Tel: +971 2 665 9998."
                            ),
                            "search_phrases": [
                                "How can I call Al Zaabi Group?",
                                "Al Zaabi Group contact number",
                            ],
                        },
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "physical address",
                            "value": (
                                "Office No 403 & 404 Al Reem Plaza, Electra Street, Abu Dhabi UAE"
                            ),
                            "evidence": (
                                "Al Zaabi Group is located at Office No 403 & 404 Al Reem Plaza, "
                                "Electra Street, Abu Dhabi UAE."
                            ),
                            "search_phrases": [
                                "Where is Al Zaabi Group located?",
                                "Give me directions to Al Zaabi Group",
                            ],
                        },
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "business hours",
                            "value": "Monday to Friday, 8:30 AM to 5:30 PM",
                            "evidence": (
                                "Al Zaabi Group business hours are Monday to Friday, "
                                "8:30 AM to 5:30 PM."
                            ),
                            "search_phrases": [
                                "When is Al Zaabi Group open?",
                                "Al Zaabi Group working timings",
                            ],
                        },
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "chairman",
                            "value": "Saeed Yousif Ibrahim Al Zaabi",
                            "evidence": ("Al Zaabi Group Chairman: Saeed Yousif Ibrahim Al Zaabi."),
                            "search_phrases": [
                                "Who is the chairman of Al Zaabi Group?",
                                "Who runs Al Zaabi Group?",
                            ],
                        },
                        {
                            "subject": "Devu Vimal",
                            "predicate": "role",
                            "value": "Board Member and Chief Financial Officer",
                            "evidence": (
                                "Devu Vimal is a Board Member and Chief Financial Officer "
                                "of Al Zaabi Group."
                            ),
                            "aliases": ["देवू विमल", "ديفو فيمال"],
                            "search_phrases": [
                                "Who is Devu Vimal?",
                                "What is Devu Vimal's role?",
                            ],
                        },
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "core services",
                            "value": "Healthcare, Trading, Contracting, Automotive and Transport",
                            "evidence": (
                                "Al Zaabi Group provides Healthcare, Trading, Contracting, "
                                "Automotive and Transport services."
                            ),
                            "search_phrases": [
                                "What services does Al Zaabi Group provide?",
                                "What does Al Zaabi Group do?",
                            ],
                        },
                        {
                            "subject": "Al Zaabi Group",
                            "predicate": "inception year",
                            "value": "2003",
                            "evidence": "Al Zaabi Group has operated since its inception in 2003.",
                            "search_phrases": [
                                "When was Al Zaabi Group established?",
                                "When was Al Zaabi Group founded?",
                                "What is Al Zaabi Group's inception year?",
                            ],
                        },
                    ],
                },
            ),
            ExactFactSource(
                source_id=CLINIC_SOURCE_ID,
                source_name="Verified Royal Clinic contact",
                source_url="https://example.test/royal-clinic/contact",
                content_sha256="b" * 64,
                compiled_at="2026-09-04T00:00:00+00:00",
                structured_content={
                    "schema_version": "tier1-fixture-v1",
                    "facts": [
                        {
                            "subject": "Royal Clinic",
                            "predicate": "primary telephone",
                            "value": "+971 2 555 0101",
                            "evidence": "Royal Clinic telephone: +971 2 555 0101.",
                            "search_phrases": ["What is the Royal Clinic phone number?"],
                        }
                    ],
                },
            ),
        ),
        knowledge_base_id="33333333-3333-4333-8333-333333333333",
        revision="tier1-quality-fixture-v1",
    )


def _evidence_id(index: ExactFactIndex, value: str) -> str:
    return next(fact.evidence_id for fact in index.facts if fact.value == value)


def tier1_quality_cases(index: ExactFactIndex):
    from tests.quality.tier1_harness import Tier1QualityCase

    phone_id = _evidence_id(index, "+971 2 665 9998")
    address_id = _evidence_id(
        index,
        "Office No 403 & 404 Al Reem Plaza, Electra Street, Abu Dhabi UAE",
    )
    hours_id = _evidence_id(index, "Monday to Friday, 8:30 AM to 5:30 PM")
    chairman_id = _evidence_id(index, "Saeed Yousif Ibrahim Al Zaabi")
    devu_id = _evidence_id(index, "Board Member and Chief Financial Officer")
    services_id = _evidence_id(
        index,
        "Healthcare, Trading, Contracting, Automotive and Transport",
    )
    founding_id = _evidence_id(index, "2003")
    return (
        Tier1QualityCase(
            case_id="phone_paraphrases",
            query="What is the phone number for Al Zaabi Group?",
            paraphrases=(
                "How can I call Al Zaabi Group?",
                "Give me Al Zaabi Group's contact number.",
            ),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.PHONE,),
            expected_evidence_ids=(phone_id,),
            forbidden_values=("+971 2 555 0101",),
        ),
        Tier1QualityCase(
            case_id="address_paraphrases",
            query="Where is Al Zaabi Group located?",
            paraphrases=("Give me directions to Al Zaabi Group.",),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.ADDRESS,),
            expected_evidence_ids=(address_id,),
        ),
        Tier1QualityCase(
            case_id="hours_paraphrases",
            query="When is Al Zaabi Group open?",
            paraphrases=("What are Al Zaabi Group's working timings?",),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.HOURS,),
            expected_evidence_ids=(hours_id,),
        ),
        Tier1QualityCase(
            case_id="leadership_paraphrases",
            query="Who is the chairman of Al Zaabi Group?",
            paraphrases=("Who runs Al Zaabi Group?",),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.LEADERSHIP,),
            expected_evidence_ids=(chairman_id,),
        ),
        Tier1QualityCase(
            case_id="services_paraphrases",
            query="What services does Al Zaabi Group provide?",
            paraphrases=(
                "What does Al Zaabi Group do?",
                "What businesses or divisions does Al Zaabi Group operate?",
            ),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.SERVICES,),
            expected_evidence_ids=(services_id,),
        ),
        Tier1QualityCase(
            case_id="founding_paraphrases",
            query="When was Al Zaabi Group established?",
            paraphrases=(
                "When was Al Zaabi Group founded?",
                "What is Al Zaabi Group's inception year?",
                "How long has Al Zaabi Group been operating?",
            ),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.FOUNDING,),
            expected_evidence_ids=(founding_id,),
        ),
        Tier1QualityCase(
            case_id="proper_name",
            query="Who is Devu Vimal?",
            paraphrases=("who is devu vimal?",),
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.LEADERSHIP,),
            expected_evidence_ids=(devu_id,),
            forbidden_values=("Saeed Yousif Ibrahim Al Zaabi",),
        ),
        Tier1QualityCase(
            case_id="cross_script_hindi",
            query="देवू विमल कौन हैं?",
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.LEADERSHIP,),
            expected_evidence_ids=(devu_id,),
        ),
        Tier1QualityCase(
            case_id="cross_script_arabic",
            query="من هو ديفو فيمال؟",
            expected_action=ExactFactResponseAction.ANSWER,
            expected_intents=(ExactFactType.LEADERSHIP,),
            expected_evidence_ids=(devu_id,),
        ),
        Tier1QualityCase(
            case_id="unverified_proper_name",
            query="Is Mohammad the chairman?",
            paraphrases=("I heard Mohammad is the chairman, is that correct?",),
            # Tier-1 extraction is an answer accelerator, not an authoritative
            # proof of absence. The general approved retriever must confirm the
            # miss before the live agent refuses it.
            expected_action=ExactFactResponseAction.FALLBACK,
            expected_intents=(ExactFactType.LEADERSHIP,),
            forbidden_values=("Saeed Yousif Ibrahim Al Zaabi",),
        ),
        Tier1QualityCase(
            case_id="unsupported_service",
            query="Do you provide legal services?",
            expected_action=ExactFactResponseAction.FALLBACK,
            expected_intents=(ExactFactType.SERVICES,),
            forbidden_values=("Healthcare",),
        ),
        Tier1QualityCase(
            case_id="out_of_tier_revenue",
            query="What was the revenue for the last six months?",
            expected_action=ExactFactResponseAction.FALLBACK,
            expected_intents=(),
        ),
        Tier1QualityCase(
            case_id="ambiguous_contact",
            query="What is the phone number?",
            # This fixture intentionally models legacy/non-authoritative AI
            # extraction. A generic query must use approved prose retrieval;
            # exact facts may not imply that these are the only two numbers.
            expected_action=ExactFactResponseAction.FALLBACK,
            expected_intents=(ExactFactType.PHONE,),
        ),
    )
