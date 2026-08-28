from app.services.agent_catalog import (
    language_catalog,
    language_code,
    normalize_voices,
    unsupported_voice_languages,
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
    assert voice_synthesizer_model({"voiceId": "rhea"}) is None
    assert voice_synthesizer_model({"voiceId": "new-unclassified-voice"}) is None
    assert voice_synthesizer_model({}) is None
