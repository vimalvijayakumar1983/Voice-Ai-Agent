"""Safe per-call configuration snapshots and their API projection."""

from typing import Any


def agent_configuration_snapshot(agent: Any) -> dict[str, Any]:
    """Capture the agent language/revision serving a call at dispatch time."""
    return {
        "provider_revision_id": getattr(agent, "provider_revision_id", None),
        "language": getattr(agent, "language", None),
        "supported_languages": list(getattr(agent, "supported_languages", None) or []),
        "language_switching_enabled": bool(getattr(agent, "language_switching_enabled", False)),
        "language_switching_mode": getattr(agent, "language_switching_mode", "disabled"),
    }


def public_call_metadata(value: Any) -> dict[str, Any] | None:
    """Return only non-secret operational metadata safe for workspace viewers."""
    if not isinstance(value, dict):
        return None
    snapshot = value.get("agent_configuration")
    if not isinstance(snapshot, dict):
        return None

    result: dict[str, Any] = {}
    conversation_type = value.get("conversation_type")
    if conversation_type in {"webcall", "chat", "telephonyInbound", "telephonyOutbound"}:
        result["conversation_type"] = conversation_type
    channel = value.get("channel")
    if channel in {"browser", "chat", "phone"}:
        result["channel"] = channel
    revision_id = snapshot.get("provider_revision_id")
    if isinstance(revision_id, str) and revision_id:
        result["provider_revision_id"] = revision_id
    language = snapshot.get("language")
    if isinstance(language, str) and language:
        result["language"] = language
    supported_languages = snapshot.get("supported_languages")
    if isinstance(supported_languages, list):
        result["supported_languages"] = [
            language for language in supported_languages if isinstance(language, str) and language
        ][:50]
    switching_enabled = snapshot.get("language_switching_enabled")
    if isinstance(switching_enabled, bool):
        result["language_switching_enabled"] = switching_enabled
    switching_mode = snapshot.get("language_switching_mode")
    if switching_mode in {"disabled", "automatic"}:
        result["language_switching_mode"] = switching_mode
    return result or None
