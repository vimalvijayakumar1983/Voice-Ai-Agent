"""CPU optimizations must preserve tokens and fuzzy-match decisions exactly."""

from difflib import SequenceMatcher
from itertools import product

from app.services import knowledge_retrieval as kr


def test_repeated_tokens_expand_once_without_changing_the_result():
    words = ["doctor", "dental", "cardiologist", "not", "Dubai", "2026", "laser"] * 200
    expected = set()
    for word in words:
        expected.update((word, kr._singular(word)))
        expected.update(kr._specialty_forms(word))
    assert kr._token_forms(words) == expected
    assert isinstance(kr._specialty_forms("dental"), frozenset)
    assert kr._specialty_forms.cache_info().maxsize == 4096


def old_similarity(query, canonical):
    scores = [SequenceMatcher(None, a, b).ratio() for a, b in zip(query, canonical, strict=True)]
    exact = sum(a == b for a, b in zip(query, canonical, strict=True))
    if len(query) == 1:
        return scores[0] if query[0][:1] == canonical[0][:1] and scores[0] >= 0.86 else 0.0
    initials = all(a[:1] == b[:1] for a, b in zip(query, canonical, strict=True))
    compact = SequenceMatcher(None, "".join(query), "".join(canonical)).ratio()
    average = sum(scores) / len(scores)
    threshold = 0.7 if len(query) >= 3 and exact >= len(query) - 1 else 0.78
    if (exact == 0 and not initials) or compact < threshold or average < threshold:
        return 0.0
    return average * 0.65 + compact * 0.35


def test_early_rejections_are_identical_to_previous_fuzzy_scoring():
    terms = ["laser", "lazer", "doctor", "doctors", "Amina", "Amna", "Dubai", "Abu", "not"]
    for a, b, c, d in product(terms, repeat=4):
        assert kr._phrase_similarity((a, b), (c, d)) == old_similarity((a, b), (c, d))
    for a, b in product(terms, repeat=2):
        assert kr._phrase_similarity((a,), (b,)) == old_similarity((a,), (b,))
