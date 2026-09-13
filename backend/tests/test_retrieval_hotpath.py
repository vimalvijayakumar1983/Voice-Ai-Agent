"""Behavior-preserving word expansion, with no provider calls or caches."""

import random

import pytest

from app.services import knowledge_retrieval as retrieval


def reference(tokens):
    expanded = set()
    for token in tokens:
        expanded.add(token)
        expanded.add(retrieval._singular(token))
        expanded.update(retrieval._specialty_forms(token))
        if token == "dr":
            expanded.add("doctor")
    return expanded


@pytest.mark.parametrize("seed", range(10))
def test_distinct_expansion_matches_previous_algorithm(seed):
    rng = random.Random(seed)
    words = [
        "dr",
        "dentist",
        "urology",
        "surgeons",
        "pediatrician",
        "clinic",
        "nondental",
        "electric",
        "doctor",
        "surgery",
        "no",
        "not",
        "2003",
        "12.50",
        "AED",
        "invoice",
        "hours",
        "companies",
        "طبيب",
        "أسنان",
        "डॉक्टर",
        "",
        "a" * 500,
    ]
    for count in (0, 1, 10, 100, 1000):
        tokens = rng.choices(words, k=count)
        before = list(tokens)
        assert retrieval._token_forms(tokens) == reference(tokens)
        assert tokens == before


def test_repeated_words_expand_only_once(monkeypatch):
    original = retrieval._specialty_forms
    calls = []

    def tracked(token):
        calls.append(token)
        return original(token)

    monkeypatch.setattr(retrieval, "_specialty_forms", tracked)
    tokens = ["doctor", "urology", "dr"] * 10_000
    result = retrieval._token_forms(tokens)
    assert len(calls) == 3
    assert {"doctor", "urology", "urologist", "dr"} <= result


def test_each_call_uses_its_own_words_and_fresh_result():
    first = retrieval._token_forms(["tenant_a", "urology"])
    first.add("injected")
    assert "injected" not in retrieval._token_forms(["tenant_a", "urology"])
    assert "tenant_a" not in retrieval._token_forms(["tenant_b", "dental"])


@pytest.mark.parametrize(
    "query",
    [
        "Which doctor works in urology?",
        "Do you offer beard laser?",
        "What is the invoice amount?",
        "Where is the dental department?",
    ],
)
def test_ranking_outputs_exactly_match_reference(monkeypatch, query):
    documents = [
        ("Directory", ("Dr Example is a urologist. Dr Sample is a dentist. " * 300)),
        ("Policies", ("Beard laser is not offered. Invoice amount AED 12.50. " * 300)),
        ("Location", ("The dental department is on floor 2. " * 300)),
    ]
    plan = retrieval.build_contextual_query_plan(query)
    actual = retrieval._rank_contextual_knowledge(plan.variants, documents, 4)
    monkeypatch.setattr(retrieval, "_token_forms", reference)
    assert actual == retrieval._rank_contextual_knowledge(plan.variants, documents, 4)
