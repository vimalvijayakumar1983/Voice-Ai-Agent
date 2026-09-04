"""Canonical resolution of speech-recognition settings for every call path."""

from __future__ import annotations

from typing import Any

INWORLD_STT_FIRST_PARTY = "inworld/inworld-stt-1"
INWORLD_STT_FAST_ACCURATE = "assemblyai/u3-rt-pro"
INWORLD_STT_WIDE_MULTILINGUAL = "soniox/stt-rt-v4"
INWORLD_STT_MODELS = frozenset(
    {
        INWORLD_STT_FIRST_PARTY,
        INWORLD_STT_FAST_ACCURATE,
        INWORLD_STT_WIDE_MULTILINGUAL,
    }
)
U3_SUPPORTED_LANGUAGES = frozenset({"en", "es", "fr", "de", "it", "pt"})


def configured_stt_languages(*, model: Any, profile: Any) -> tuple[str, ...]:
    """Return the declared language set in stable, de-duplicated order."""

    values = [
        *(getattr(model, "supported_languages", None) or []),
        getattr(model, "language", ""),
        getattr(profile, "stt_language", ""),
    ]
    return tuple(
        dict.fromkeys(
            text
            for value in values
            if (text := str(value or "").strip()) and text.casefold() != "auto"
        )
    )


def resolve_inworld_stt_language(*, model: Any, profile: Any) -> str:
    """Resolve the exact language sent over the Inworld session boundary."""

    configured = str(getattr(profile, "stt_language", "") or "").strip()
    if configured and configured.casefold() != "auto":
        return configured
    base_languages = {
        language.casefold().split("-", 1)[0]
        for language in configured_stt_languages(model=model, profile=profile)
    }
    if getattr(model, "language_switching_enabled", False) and len(base_languages) > 1:
        return "auto"
    return str(getattr(model, "language", "") or "en-US").strip() or "en-US"


def inworld_stt_wire_language(*, model: Any, profile: Any) -> str | None:
    """Return the exact language value serialized to Inworld.

    The LiveKit adapter represents provider auto-detection as an explicit JSON
    null.  Keeping that boundary conversion next to the policy resolver makes
    the production session and the live readiness probe exercise the same
    route.
    """

    effective = resolve_inworld_stt_language(model=model, profile=profile)
    return None if effective.casefold() == "auto" else effective


def resolved_stt_script_languages(*, model: Any, profile: Any) -> tuple[str, ...]:
    """Return only scripts the on-wire recognition mode may legitimately emit."""

    effective = resolve_inworld_stt_language(model=model, profile=profile)
    if effective.casefold() != "auto":
        return (effective,)
    return configured_stt_languages(model=model, profile=profile)


def configured_inworld_stt_model(*, profile: Any) -> str:
    runtime_config = getattr(profile, "runtime_config", None)
    config = runtime_config if isinstance(runtime_config, dict) else {}
    return str(config.get("stt_model") or "auto").strip().casefold()


def resolve_inworld_stt_model(*, model: Any, profile: Any) -> str:
    """Select the recognizer once so reservation, diagnostics and worker agree."""

    configured = configured_inworld_stt_model(profile=profile)
    if configured in INWORLD_STT_MODELS:
        return configured
    languages = {
        language.casefold().split("-", 1)[0]
        for language in configured_stt_languages(model=model, profile=profile)
    }
    return (
        INWORLD_STT_FAST_ACCURATE
        if languages and languages.issubset(U3_SUPPORTED_LANGUAGES)
        else INWORLD_STT_WIDE_MULTILINGUAL
    )
