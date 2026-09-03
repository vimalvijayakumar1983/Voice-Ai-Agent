from types import SimpleNamespace

from app.services.call_disposition import (
    disposition_catalog,
    infer_disposition_profile,
    normalize_call_analysis,
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
