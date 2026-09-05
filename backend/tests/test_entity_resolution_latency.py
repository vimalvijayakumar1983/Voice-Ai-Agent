"""Guard performance work with complete legacy-result equivalence."""

import random
from dataclasses import replace

import pytest

from app.services import speech_lexicon as lexicon
from tests.quality.legacy_entity_resolver import resolve_canonical_entity as legacy_resolve


def entry(name, index=0, **overrides):
    values = dict(
        entry_id=str(index),
        canonical=name,
        normalized=lexicon._normalized(name),
        entity_type="person",
        tier=1,
        priority=100,
        critical=False,
        languages=("en",),
        aliases=(),
        phonetic_keys=(lexicon._phonetic_key(name),),
        source_ids=(str(index),),
        evidence_sha256=(),
    )
    return lexicon.SpeechLexiconEntry(**(values | overrides))


@pytest.mark.parametrize(
    "query",
    [
        "",
        "Who is Saeed Al Zaabi?",
        "Who is Saed Al Zabi?",
        "Can you give me the list of leadership team in Al Zaabi Group?",
        "I need a list of leadership team.",
        "Who is Dev Vimal?",
        "Saeed Al Zaabi and Devu Vimal",
        "unknown question",
        "a",
        "AB",
        "José Álvarez",
        "Jose Alvarez",
        "محمد علي",
        "सईद",
        "Al-Zaabi",
        "xSaeed Al Zaabiy",
        "Saeed_Al_Zaabi",
        "Saeeed Al Zabi!",
    ],
)
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"expected_entity_types": ("person",)},
        {"minimum_confidence": 0, "safe_apply_margin": 0},
        {"minimum_confidence": 0.93, "safe_apply_confidence": 0.97},
    ],
)
def test_full_legacy_result_equivalence(query, options):
    entries = (
        entry("Saeed Al Zaabi", aliases=("Saeed", "Al-Zaabi")),
        entry("Saeed Al Zabi", 1, priority=150),
        entry("Devu Vimal", 2),
        entry("Al Zaabi Group", 3, entity_type="organization"),
        entry("José Álvarez", 4),
        entry("محمد علي", 5),
        entry("AB", 6),
        entry("", 7),
        entry("सईद", 8),
    )
    expected = legacy_resolve(query, entries, **options)
    assert lexicon.resolve_canonical_entity(query, entries, **options) == expected
    assert (
        lexicon.resolve_canonical_entity(query, lexicon.prepare_speech_lexicon(entries), **options)
        == expected
    )


def test_seeded_mutations_preserve_scores_ties_margins_and_actions():
    rng = random.Random(20260906)
    names = [
        "Saeed Al Zaabi",
        "Devu Vimal",
        "Harbour Medical Centre",
        "José Álvarez",
        "محمد علي",
        "Royal Hospital",
        "A A",
    ]
    entries = [entry(name, i, tier=i % 3 + 1, priority=i * 7) for i, name in enumerate(names)]
    entries += [replace(entries[0], entry_id="duplicate", priority=999)]
    prepared = lexicon.prepare_speech_lexicon(entries)
    for _ in range(150):
        chars = list(rng.choice(names))
        for _ in range(rng.randrange(4)):
            at = rng.randrange(len(chars))
            chars[at] = rng.choice(" aeiousz")
        query = rng.choice(["Who is ", "Tell me about ", ""]) + "".join(chars)
        options = dict(
            expected_entity_types=rng.choice([(), ("person",), ("organization",)]),
            minimum_confidence=rng.choice([0, 0.5, 0.84, 0.95]),
            safe_apply_margin=rng.choice([0, 0.06, 0.2]),
        )
        assert lexicon.resolve_canonical_entity(query, prepared, **options) == legacy_resolve(
            query, entries, **options
        )


def test_prepared_revisions_are_immutable_and_do_not_share_results():
    old = [entry("Old Company", entity_type="organization")]
    prepared = lexicon.prepare_speech_lexicon(old)
    old[0] = entry("New Company", entity_type="organization")
    assert lexicon.resolve_canonical_entity("Old Company", prepared).canonical == "Old Company"
    assert lexicon.resolve_canonical_entity("New Company", old).canonical == "New Company"


def test_windows_prepared_once_per_alias_width(monkeypatch):
    entries = tuple(entry(f"Harbour Division {i}", i) for i in range(100))
    prepared = lexicon.prepare_speech_lexicon(entries)
    original = lexicon._candidate_windows
    calls = []

    def track(text, width):
        calls.append(width)
        return original(text, width)

    monkeypatch.setattr(lexicon, "_candidate_windows", track)
    lexicon.resolve_canonical_entity("Which healthcare companies are in the group?", prepared)
    assert calls == [3]
