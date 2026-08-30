from app.services.agent_catalog import (
    AGENT_TEMPLATES,
    LanguageCompatibilityStatus,
    VoiceModelPool,
    language_catalog,
    language_code,
    normalize_voices,
    unsupported_voice_languages,
    voice_language_compatibility,
    voice_model_pool,
    voice_synthesizer_model,
)


def test_language_code_preserves_safe_provider_codes_and_labels():
    assert language_code("English") == "en"
    assert language_code("AR") == "ar"
    assert language_code("Arabic") == "ar"
    assert language_code("Chinese (Mandarin)") == "zh"
    assert language_code("Modern Standard Arabic") == "ar"
    assert language_code("pt_BR") == "pt-br"
    assert language_code("zh-Hant-TW") == "zh-hant-tw"
    assert language_code("English (United States)") == "en-us"
    assert language_code("Spanish (Latin America)") == "es-419"
    assert language_code("en_US.UTF-8") == "en-us"
    assert language_code("sr_RS@latin") == "sr-rs-latin"
    assert language_code("iw_IL") == "he-il"
    assert language_code("qaa_Latn_X_DEMO") == "qaa-latn-x-demo"
    assert language_code("Klingon") == "klingon"

    assert language_code("") is None
    assert language_code("<script>alert(1)</script>") is None
    assert language_code("x" * 64) is None
    assert language_code("en/../../secrets") is None


def test_catalog_preserves_every_safe_provider_language_without_local_fallback():
    voices = normalize_voices(
        [
            {
                "voiceId": "global-voice",
                "displayName": "Global Voice",
                "tags": {
                    "language": [
                        "English",
                        "Arabic",
                        "ja_JP",
                        "zh-Hant-TW",
                        "Modern Standard Arabic",
                        "<script>alert(1)</script>",
                    ]
                },
            }
        ],
        [],
    )

    assert voices[0]["languages"] == [
        "en",
        "ar",
        "ja-jp",
        "zh-hant-tw",
    ]
    assert {item["code"]: item["name"] for item in language_catalog(voices)} == {
        "en": "English",
        "ar": "Arabic",
        "ja-jp": "Japanese (JP)",
        "zh-hant-tw": "Chinese (Mandarin) (Hant-TW)",
    }

    assert language_catalog(normalize_voices([{"voiceId": "untagged"}], [])) == []
    assert language_catalog([]) == []


def test_voice_language_capability_validation_is_provider_grounded():
    provider_voices = [
        {
            "voiceId": "tagged",
            "tags": {"language": ["English", "Hindi", "pt_BR", "Arabic"]},
        },
        {"voiceId": "untagged", "tags": {"gender": "female"}},
    ]

    assert (
        unsupported_voice_languages(
            provider_voices,
            "tagged",
            ["en", "hi", "pt-br", "ar"],
        )
        == []
    )
    assert unsupported_voice_languages(
        provider_voices,
        "tagged",
        ["en", "fr", "ja_JP", "fr"],
    ) == ["fr", "ja-jp"]

    # The provider default, untagged voices, and unknown IDs have no advertised
    # capability boundary to enforce here. Public voice existence is a separate check.
    assert unsupported_voice_languages(provider_voices, "", ["fr"]) == []
    assert unsupported_voice_languages(provider_voices, "untagged", ["fr"]) == []
    assert unsupported_voice_languages(provider_voices, "missing", ["fr"]) == []

    assert voice_language_compatibility(provider_voices, "untagged", ["fr"]) == (
        LanguageCompatibilityStatus.UNKNOWN,
        [],
    )
    assert voice_language_compatibility(provider_voices, "missing", ["fr"]) == (
        LanguageCompatibilityStatus.VOICE_NOT_FOUND,
        [],
    )


def test_voice_language_compatibility_normalizes_base_locale_and_script_tags():
    provider_voices = [
        {
            "voiceId": "locale-aware",
            "tags": {"language": ["en_US", "zh-Hant-TW"]},
        }
    ]

    assert (
        unsupported_voice_languages(
            provider_voices,
            "locale-aware",
            ["en", "en-US", "zh-Hant", "zh_Hant_TW"],
        )
        == []
    )
    assert unsupported_voice_languages(
        provider_voices,
        "locale-aware",
        ["en-GB", "zh-Hans"],
    ) == ["en-gb", "zh-hans"]


def test_voice_languages_are_read_from_supported_provider_shapes():
    voices = normalize_voices(
        [
            {"voiceId": "top-level-list", "languages": ["de_DE", "es-419"]},
            {"voiceId": "tag-plural", "tags": {"languages": ["French", "sv_SE"]}},
        ],
        [],
    )
    by_id = {voice["id"]: voice["languages"] for voice in voices}

    assert by_id == {
        "tag-plural": ["fr", "sv-se"],
        "top-level-list": ["de-de", "es-419"],
    }


def test_voice_model_pairing_distinguishes_standard_pro_and_unknown():
    assert voice_synthesizer_model({"voiceId": "jordan"}) == "waves_lightning_v3_1"
    # Atoms transparently routes both pools from the voice ID; it does not take
    # the direct Waves `lightning_v3.1_pro` token in its draft model field.
    assert voice_synthesizer_model({"voiceId": "rhea"}) == "waves_lightning_v3_1"
    assert (
        voice_synthesizer_model({"voiceId": "new-standard", "modelIds": ["lightning-v3.1"]})
        == "waves_lightning_v3_1"
    )
    assert (
        voice_synthesizer_model({"voiceId": "new-pro", "modelIds": ["lightning-v3.1-pro"]})
        == "waves_lightning_v3_1"
    )
    assert (
        voice_model_pool({"voiceId": "new-standard", "modelIds": ["lightning_v3.1"]})
        is VoiceModelPool.STANDARD
    )
    assert (
        voice_model_pool({"voiceId": "new-pro", "modelIds": ["lightning_v3.1_pro"]})
        is VoiceModelPool.PRO
    )

    # A current catalog entry can safely use Atoms' provider routing even when
    # its new ID has not yet reached the local model-card snapshot.
    assert (
        voice_model_pool(
            {
                "voiceId": "new-catalog-voice",
                "displayName": "New Catalog Voice",
                "tags": {"language": ["English"]},
            }
        )
        is VoiceModelPool.PROVIDER_ROUTED
    )
    assert (
        voice_synthesizer_model(
            {
                "voiceId": "new-catalog-voice",
                "displayName": "New Catalog Voice",
                "tags": {"language": ["English"]},
            }
        )
        == "waves_lightning_v3_1"
    )

    assert voice_synthesizer_model({"voiceId": "new-unclassified-voice"}) is None
    assert (
        voice_synthesizer_model(
            {
                "voiceId": "jordan",
                "displayName": "Jordan",
                "modelIds": ["future-model-v4"],
            }
        )
        is None
    )
    assert voice_synthesizer_model({}) is None


def test_normalized_voices_expose_provider_derived_pool_without_model_guessing():
    voices = normalize_voices(
        [
            {"voiceId": "jordan", "displayName": "Jordan"},
            {"voiceId": "rhea", "displayName": "Rhea"},
            {
                "voiceId": "new-provider-routed",
                "displayName": "New Provider Routed",
                "tags": {"language": ["English"]},
            },
            {
                "voiceId": "future-model",
                "displayName": "Future Model",
                "modelIds": ["future-model-v4"],
            },
        ],
        [
            {
                "voiceId": "brand-standard",
                "displayName": "Brand Standard",
                "status": "completed",
                "modelIds": ["lightning-v3.1"],
            },
            {
                "voiceId": "brand-pro",
                "displayName": "Brand Pro",
                "status": "completed",
                "modelIds": ["lightning-v3.1-pro"],
            },
            {
                "voiceId": "brand-unknown",
                "displayName": "Brand Unknown",
                "status": "completed",
                "modelIds": ["future-model-v4"],
            },
        ],
    )
    by_id = {voice["id"]: voice for voice in voices}

    assert {voice_id: voice["voice_pool"] for voice_id, voice in by_id.items()} == {
        "jordan": "standard",
        "rhea": "pro",
        "new-provider-routed": "unknown",
        "future-model": "unknown",
        "brand-standard": "cloned",
        "brand-pro": "cloned",
        "brand-unknown": "cloned",
    }
    assert by_id["jordan"]["_model_pool"] is VoiceModelPool.STANDARD
    assert by_id["rhea"]["_model_pool"] is VoiceModelPool.PRO
    assert by_id["new-provider-routed"]["_model_pool"] is VoiceModelPool.PROVIDER_ROUTED
    assert by_id["future-model"]["_model_pool"] is None
    assert by_id["brand-standard"]["_model_pool"] is VoiceModelPool.STANDARD
    assert by_id["brand-pro"]["_model_pool"] is VoiceModelPool.PRO
    assert by_id["brand-unknown"]["_model_pool"] is None


def test_language_catalog_excludes_languages_available_only_on_unusable_voices():
    voices = normalize_voices(
        [
            {
                "voiceId": "rhea",
                "displayName": "Rhea",
                "tags": {"language": ["Arabic"]},
            },
            {
                "voiceId": "future-voice",
                "displayName": "Future Voice",
                "tags": {"language": ["Klingon"]},
                "modelIds": ["future-model-v4"],
            },
        ],
        [],
    )
    by_id = {voice["id"]: voice for voice in voices}

    assert by_id["rhea"]["synthesizer_model"] == "waves_lightning_v3_1"
    assert by_id["future-voice"]["synthesizer_model"] is None
    assert language_catalog(voices) == [{"code": "ar", "name": "Arabic"}]


def test_templates_mark_unavailable_action_dependencies_truthfully():
    templates = {template["id"]: template for template in AGENT_TEMPLATES}
    all_prompts = "\n".join(template["system_prompt"] for template in AGENT_TEMPLATES)

    assert "Transfer or escalate urgent" not in all_prompts
    assert "record do-not-call immediately" not in all_prompts
    assert "Use the scheduling tool to check real availability" not in all_prompts
    assert "State the verified balance" not in all_prompts

    assert (
        "connected handoff capability returns confirmation"
        in templates["receptionist"]["system_prompt"]
    )
    support_prompt = templates["customer_support"]["system_prompt"]
    assert "APPROVED KNOWLEDGE BASE CONTEXT" in support_prompt
    assert "authoritative reference data" in support_prompt
    assert "Never invent policies, prices, availability" in support_prompt
    assert "no connected do-not-call writer" in templates["lead_qualification"]["system_prompt"]
    assert (
        "Without a confirmed scheduling result" in templates["appointment_booking"]["system_prompt"]
    )
    assert "cannot take or process a payment" in templates["payment_reminder"]["system_prompt"]
    assert "connected compliance writer" in templates["payment_reminder"]["system_prompt"]
