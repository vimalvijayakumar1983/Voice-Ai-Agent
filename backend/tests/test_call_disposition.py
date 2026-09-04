from types import SimpleNamespace

from app.services.call_disposition import (
    apply_grounding_quality_guard,
    disposition_catalog,
    infer_disposition_profile,
    normalize_call_analysis,
    normalize_provider_call_analysis,
    summarize_runtime_grounding,
)


def test_receptionist_information_call_is_resolved_without_follow_up():
    result = normalize_call_analysis(
        {
            "summary": "The caller requested and received the office number.",
            "key_topics": ["contact details"],
            "action_items": [],
            "sentiment": "neutral",
            "disposition": "information_provided",
            "resolution": "resolved",
            "customer_intent": "Obtain the office telephone number",
            "follow_up": {"required": False},
            "confidence": 0.96,
            "evidence": ["What is your telephone number?"],
            "needs_review": False,
        },
        profile="receptionist",
    )

    assert result["disposition"] == "information_provided"
    assert result["disposition_details"]["resolution"] == "resolved"
    assert result["disposition_details"]["follow_up"]["required"] is False
    assert result["disposition_details"]["needs_review"] is False


def test_sales_only_label_cannot_misclassify_receptionist_call():
    result = normalize_call_analysis(
        {
            "summary": "The caller asked for an address.",
            "disposition": "interested",
            "resolution": "resolved",
            "confidence": 0.91,
        },
        profile="receptionist",
    )

    assert result["disposition"] == "unknown"
    assert result["disposition_details"]["needs_review"] is True


def test_explicit_profile_metadata_wins_and_catalog_is_scoped():
    agent = SimpleNamespace(
        agent_metadata={"disposition_profile": "collections"},
        name="General agent",
        description="",
        system_prompt="",
    )

    assert infer_disposition_profile(agent) == "collections"
    assert "payment_promised" in disposition_catalog("collections")
    assert "qualified_lead" not in disposition_catalog("collections")


def test_low_confidence_outcome_is_routed_for_review():
    result = normalize_call_analysis(
        {
            "summary": "The audio was unclear.",
            "disposition": "callback",
            "resolution": "unknown",
            "confidence": 0.4,
            "follow_up": {"required": True, "action": "Call again", "owner": "sales"},
        },
        profile="sales",
    )

    assert result["disposition_details"]["needs_review"] is True
    assert result["disposition_details"]["follow_up"] == {
        "required": True,
        "action": "Call again",
        "owner": "sales",
        "due_at": None,
    }


def test_trusted_provider_analytics_are_normalized_without_inventing_evidence():
    result = normalize_provider_call_analysis(
        {
            "summary": "The caller received the requested office information.",
            "keyTopics": ["office details"],
            "actionItems": [],
            "sentiment": "positive",
            "dispositionMetrics": [{"value": "answered", "confidence": 0.92}],
        },
        profile="receptionist",
    )

    assert result is not None
    assert result["disposition"] == "information_provided"
    assert result["disposition_details"]["resolution"] == "resolved"
    assert result["disposition_details"]["analysis_source"] == "provider_analytics"
    assert result["disposition_details"]["evidence"] == []


def test_incomplete_or_out_of_profile_provider_analytics_require_ai_fallback():
    assert (
        normalize_provider_call_analysis(
            {"summary": "No disposition was supplied."},
            profile="receptionist",
        )
        is None
    )
    assert (
        normalize_provider_call_analysis(
            {
                "summary": "The caller asked for an address.",
                "dispositionMetrics": [{"value": "qualified_lead"}],
            },
            profile="receptionist",
        )
        is None
    )


def test_grounding_guard_downgrades_only_unsupported_no_match_answer():
    analysis = normalize_call_analysis(
        {
            "summary": "The caller received company information.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.9,
        },
        profile="receptionist",
    )
    grounding = summarize_runtime_grounding(
        {
            "runtime": {
                "turn_diagnostics": [
                    {"grounding_outcome": "response_after_verified_retrieval"},
                    {"grounding_outcome": "no_match_correctly_refused"},
                    {"grounding_outcome": "no_match_unverified_response"},
                ]
            }
        }
    )

    guarded = apply_grounding_quality_guard(analysis, grounding=grounding)

    assert guarded["disposition_details"]["resolution"] == "partially_resolved"
    assert guarded["disposition_details"]["needs_review"] is True
    assert guarded["disposition_details"]["confidence"] == 0.5
    assert guarded["disposition_details"]["grounding"]["response_after_verified_retrieval"] == 1
    assert guarded["disposition_details"]["grounding"]["no_match_correctly_refused"] == 1


def test_grounding_guard_keeps_correct_refusal_out_of_manual_review():
    analysis = normalize_call_analysis(
        {
            "summary": "The agent correctly declined an unsupported request.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.9,
        },
        profile="receptionist",
    )

    guarded = apply_grounding_quality_guard(
        analysis,
        grounding={"no_match_correctly_refused": 1},
    )

    assert guarded["disposition_details"]["resolution"] == "resolved"
    assert guarded["disposition_details"]["needs_review"] is False


def test_grounding_guard_downgrades_refusal_or_clarification_despite_exact_evidence():
    analysis = normalize_call_analysis(
        {
            "summary": "The agent did not use exact facts that retrieval supplied.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.94,
        },
        profile="receptionist",
    )
    grounding = summarize_runtime_grounding(
        {
            "runtime": {
                "turn_diagnostics": [
                    {
                        "grounding_outcome": "response_after_verified_retrieval",
                        "response_action": "refused_despite_verified_evidence",
                        "exact_fact_action": "answer",
                    },
                    {
                        "grounding_outcome": "response_after_verified_retrieval",
                        "response_action": "asked_clarification_despite_verified_evidence",
                        "exact_fact_action": "answer",
                    },
                ]
            }
        }
    )

    guarded = apply_grounding_quality_guard(analysis, grounding=grounding)

    details = guarded["disposition_details"]
    assert grounding["refused_despite_verified_evidence"] == 1
    assert grounding["clarified_despite_verified_exact_fact"] == 1
    assert details["resolution"] == "partially_resolved"
    assert details["needs_review"] is True
    assert details["confidence"] == 0.5
    assert "refused despite verified evidence" in details["evidence"][-1]
    assert "verified exact-fact answer" in details["evidence"][-1]


def test_grounding_guard_downgrades_knowledge_tool_errors_but_not_correct_refusals():
    analysis = normalize_call_analysis(
        {
            "summary": "The call ended after the knowledge tool failed.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.95,
        },
        profile="receptionist",
    )

    guarded = apply_grounding_quality_guard(
        analysis,
        grounding={
            "knowledge_error_response": 1,
            "no_match_correctly_refused": 2,
        },
    )

    details = guarded["disposition_details"]
    assert details["resolution"] == "partially_resolved"
    assert details["needs_review"] is True
    assert details["confidence"] == 0.5
    assert details["grounding"]["no_match_correctly_refused"] == 2
    assert "knowledge retrieval error" in details["evidence"][-1]


def test_grounding_guard_downgrades_interrupted_answer_without_forging_verification():
    analysis = normalize_call_analysis(
        {
            "summary": "The assistant began answering before the call ended.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.91,
        },
        profile="receptionist",
    )
    grounding = summarize_runtime_grounding(
        {
            "runtime": {
                "ignored_interrupted_assistant_item_count": 1,
                "turn_diagnostics": [
                    {
                        "outcome": "answered",
                        "knowledge_result": "verified",
                    }
                ],
            }
        }
    )

    guarded = apply_grounding_quality_guard(analysis, grounding=grounding)

    details = guarded["disposition_details"]
    assert grounding["answered_without_grounding"] == 1
    assert grounding["response_after_verified_retrieval"] == 0
    assert details["resolution"] == "partially_resolved"
    assert details["needs_review"] is True
    assert details["confidence"] == 0.5
    assert "no completed grounding verdict" in details["evidence"][-1]


def test_completed_grounded_answer_is_not_downgraded_after_an_earlier_barge_in():
    analysis = normalize_call_analysis(
        {
            "summary": "The caller received a verified answer after changing the question.",
            "disposition": "information_provided",
            "resolution": "resolved",
            "confidence": 0.91,
        },
        profile="receptionist",
    )
    grounding = summarize_runtime_grounding(
        {
            "runtime": {
                # This counter is useful operational telemetry, but a normal
                # barge-in is not itself evidence that the final turn failed.
                "ignored_interrupted_assistant_item_count": 1,
                "turn_diagnostics": [
                    {
                        "outcome": "answered",
                        "knowledge_result": "verified",
                        "grounding_outcome": "response_after_verified_retrieval",
                    }
                ],
            }
        }
    )

    guarded = apply_grounding_quality_guard(analysis, grounding=grounding)

    details = guarded["disposition_details"]
    assert grounding["answered_without_grounding"] == 0
    assert details["resolution"] == "resolved"
    assert details["needs_review"] is False
    assert details["confidence"] == 0.91
