"""Canonical resolution of speech-recognition settings for every call path."""

from __future__ import annotations

from typing import Any

from app.providers.sarvam import sarvam_language_code

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


def inworld_transcription_language_hint(*, model: Any, profile: Any) -> str:
    """Prompt guidance for the QA U3 route, not proof of a hard language lock.

    U3 uses native code switching; its streaming documentation recommends a
    'Transcribe <language>' prompt. Preserve wire language and auto-mode policy.
    """
    if (getattr(model, "agent_metadata", None) or {}).get("conversation_foundation_v1") is not True:
        return ""
    if resolve_inworld_stt_model(model=model, profile=profile) != INWORLD_STT_FAST_ACCURATE:
        return ""
    language = resolve_inworld_stt_language(model=model, profile=profile).casefold().split("-")[0]
    name = {
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "pt": "Portuguese",
    }.get(language)
    return (
        f"Transcribe {name}. Transcribe verbatim with standard punctuation. "
        "Include filler words and incomplete utterances. "
        if name
        else ""
    )


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


def sarvam_stt_wire_language(*, model: Any, profile: Any) -> str:
    """Return the exact language sent to Sarvam by native Twilio sessions.

    Keep readiness and the paid media path on one resolver.  Sarvam represents
    automatic language identification with the literal ``auto`` value; pinned
    languages use the provider's regional codes (for example, ``en-IN``).
    """

    configured = (
        "auto"
        if getattr(model, "language_switching_enabled", False)
        else str(getattr(profile, "stt_language", "") or "auto").strip()
    )
    if configured.casefold() == "auto":
        return "auto"
    return sarvam_language_code(configured)


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
