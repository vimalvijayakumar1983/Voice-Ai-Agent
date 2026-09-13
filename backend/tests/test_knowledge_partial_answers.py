"""Cross-business regressions for partial retrieval, not clinic-specific answers."""

import pytest

from app.services import knowledge_retrieval as retrieval


@pytest.mark.parametrize(
    "question,available,missing",
    [
        ("Do you guys provide Botox and filler?", "filler", "Botox"),
        ("Can you offer printing and binding?", "binding", "printing"),
        (
            "Does Example Clinic provide physiotherapy and cardiology?",
            "physiotherapy",
            "cardiology",
        ),
    ],
)
def test_independent_services_retain_available_evidence(question, available, missing):
    documents = [("Example Clinic services", f"Example Clinic provides {available} consultations.")]
    matches = retrieval._rank_contextual_knowledge((question,), documents, 6)
    assert matches
    assert available in matches[0].text
    assert missing not in matches[0].text


@pytest.mark.parametrize(
    "question",
    [
        "Do you provide Botox and filler together?",
        "Do you provide Botox and filler for children?",
        "Do you provide Botox and filler in Dubai?",
        "Do you provide Botox and filler today?",
        "Do you provide Botox and filler at the same price?",
        "Do you provide not Botox and filler?",
        "Do you provide research and development services?",
    ],
)
def test_qualifiers_are_not_silently_discarded(question):
    assert retrieval._capability_parts(question) == ()
    assert "their combination" in retrieval._RETRIEVAL_SCOPE_NOTE


def test_filler_specific_offer_is_not_expanded_to_generic_treatment():
    evidence = "Filler for the intimate area: one ml at AED 750 per session."
    matches = retrieval._rank_contextual_knowledge(
        ("Do you guys provide Botox and filler?",),
        [("Offers", evidence)],
        6,
    )
    assert matches and matches[0].text == evidence


@pytest.mark.parametrize("wording", ["kind", "type", "types"])
def test_descriptive_department_query_retrieves_procedures_not_just_label(wording):
    matches = retrieval._rank_contextual_knowledge(
        (f"What {wording} of laser department are you having?",),
        [("Home", "Laser services include skin renewal, hair removal and scar correction.")],
        6,
    )
    assert matches and "hair removal" in matches[0].text


def test_description_survives_runtime_four_match_budget():
    sources = [(f"Departments {index}", "Laser Department") for index in range(5)]
    sources.append(
        ("Homepage", "Laser services include skin renewal, hair removal and scar correction.")
    )
    matches = retrieval._rank_contextual_knowledge(
        ("What kind of laser department are you having?",),
        sources,
        4,
    )
    assert "hair removal" in matches[0].text


def test_department_relationship_is_not_inferred_from_separate_lists():
    matches = retrieval._rank_contextual_knowledge(
        ("Is endoscopic surgery under the gastroenterology department?",),
        [("Departments", "Gastroenterology department. Endoscopic surgery is available.")],
        6,
    )
    assert not matches


def test_examples_do_not_establish_directory_completeness():
    assert "not a complete directory" in retrieval._RETRIEVAL_SCOPE_NOTE
    assert "Do not count" in retrieval._RETRIEVAL_SCOPE_NOTE
