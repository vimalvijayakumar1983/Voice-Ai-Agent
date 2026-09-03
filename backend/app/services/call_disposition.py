"""Provider-neutral, evidence-bound call disposition normalization."""

from __future__ import annotations

from typing import Any

CLASSIFIER_VERSION = "vav-call-outcome-v1"

PROFILES = frozenset(
    {"general", "receptionist", "customer_support", "appointment", "sales", "collections"}
)

DISPOSITIONS = frozenset(
    {
        "information_provided",
        "request_captured",
        "appointment_booked",
        "appointment_requested",
        "appointment_rescheduled",
        "appointment_cancelled",
        "issue_resolved",
        "issue_unresolved",
        "callback",
        "transferred",
        "qualified_lead",
        "interested",
        "not_interested",
        "payment_promised",
        "payment_dispute",
        "dnc",
        "voicemail",
        "wrong_number",
        "abandoned",
        "unknown",
    }
)

PROFILE_DISPOSITIONS = {
    "receptionist": {
        "information_provided",
        "request_captured",
        "callback",
        "transferred",
        "dnc",
        "wrong_number",
        "abandoned",
        "unknown",
    },
    "customer_support": {
        "information_provided",
        "request_captured",
        "issue_resolved",
        "issue_unresolved",
        "callback",
        "transferred",
        "dnc",
        "wrong_number",
        "abandoned",
        "unknown",
    },
    "appointment": {
        "information_provided",
        "appointment_booked",
        "appointment_requested",
        "appointment_rescheduled",
        "appointment_cancelled",
        "callback",
        "transferred",
        "abandoned",
        "unknown",
    },
    "sales": {
        "information_provided",
        "request_captured",
        "callback",
        "transferred",
        "qualified_lead",
        "interested",
        "not_interested",
        "dnc",
        "voicemail",
        "wrong_number",
        "abandoned",
        "unknown",
    },
    "collections": {
        "information_provided",
        "request_captured",
        "callback",
        "transferred",
        "payment_promised",
        "payment_dispute",
        "dnc",
        "voicemail",
        "wrong_number",
        "abandoned",
        "unknown",
    },
}

RESOLUTIONS = frozenset(
    {"resolved", "partially_resolved", "unresolved", "not_applicable", "unknown"}
)

ALIASES = {
    "answered": "information_provided",
    "callback_requested": "callback",
    "do_not_call": "dnc",
    "resolved": "issue_resolved",
    "unresolved": "issue_unresolved",
}


def infer_disposition_profile(agent: Any | None) -> str:
    """Select a stable profile from explicit metadata, then conservative agent hints."""
    if agent is None:
        return "general"
    metadata = getattr(agent, "agent_metadata", None)
    explicit = str(
        getattr(agent, "disposition_profile", None)
        or (metadata or {}).get("disposition_profile")
        or ""
    ).strip().lower()
    if explicit in PROFILES and explicit != "general":
        return explicit

    text = " ".join(
        str(getattr(agent, field, "") or "").lower()
        for field in ("name", "description", "system_prompt")
    )
    if any(term in text for term in ("payment reminder", "debt collection", "collections")):
        return "collections"
    appointment_terms = ("appointment coordinator", "appointment booking", "scheduler")
    if any(term in text for term in appointment_terms):
        return "appointment"
    if any(term in text for term in ("customer support", "troubleshoot", "case status")):
        return "customer_support"
    if any(term in text for term in ("lead qualification", "sales agent", "prospect")):
        return "sales"
    if any(term in text for term in ("receptionist", "welcome callers", "front desk")):
        return "receptionist"
    return "general"


def disposition_catalog(profile: str) -> list[str]:
    allowed = PROFILE_DISPOSITIONS.get(profile, DISPOSITIONS)
    return sorted(allowed)


def _text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _string_list(value: object, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item, limit=item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_call_analysis(payload: object, *, profile: str) -> dict[str, Any]:
    """Validate untrusted model output and produce one durable outcome contract."""
    raw = payload if isinstance(payload, dict) else {}
    profile = profile if profile in PROFILES else "general"
    primary = str(raw.get("disposition") or raw.get("primary_disposition") or "unknown")
    primary = ALIASES.get(primary.strip().lower(), primary.strip().lower())
    allowed = PROFILE_DISPOSITIONS.get(profile, DISPOSITIONS)
    invalid_for_profile = primary not in allowed
    if primary not in DISPOSITIONS or invalid_for_profile:
        primary = "unknown"

    resolution = str(raw.get("resolution") or "unknown").strip().lower()
    if resolution not in RESOLUTIONS:
        resolution = "unknown"

    try:
        confidence = float(raw.get("confidence", 0.0 if primary == "unknown" else 0.75))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = round(min(max(confidence, 0.0), 1.0), 3)

    follow_up_raw = raw.get("follow_up")
    follow_up = follow_up_raw if isinstance(follow_up_raw, dict) else {}
    follow_up_required = follow_up.get("required") is True
    follow_up_details = {
        "required": follow_up_required,
        "action": _text(follow_up.get("action"), limit=300),
        "owner": _text(follow_up.get("owner"), limit=100),
        "due_at": _text(follow_up.get("due_at"), limit=80),
    }
    if not follow_up_required:
        follow_up_details = {"required": False, "action": None, "owner": None, "due_at": None}

    evidence = _string_list(raw.get("evidence"), limit=3, item_limit=240)
    needs_review = bool(raw.get("needs_review")) or confidence < 0.65 or primary == "unknown"
    if invalid_for_profile:
        needs_review = True

    return {
        "summary": _text(raw.get("summary"), limit=2000) or "Call analysis unavailable.",
        "key_topics": _string_list(raw.get("key_topics"), limit=10, item_limit=120),
        "action_items": _string_list(raw.get("action_items"), limit=10, item_limit=300),
        "sentiment": (
            str(raw.get("sentiment") or "neutral").strip().lower()
            if str(raw.get("sentiment") or "neutral").strip().lower()
            in {"positive", "neutral", "negative"}
            else "neutral"
        ),
        "disposition": primary,
        "disposition_details": {
            "version": CLASSIFIER_VERSION,
            "profile": profile,
            "primary": primary,
            "secondary": _text(raw.get("secondary_disposition"), limit=100),
            "resolution": resolution,
            "customer_intent": _text(raw.get("customer_intent"), limit=300),
            "follow_up": follow_up_details,
            "confidence": confidence,
            "evidence": evidence,
            "needs_review": needs_review,
        },
    }
