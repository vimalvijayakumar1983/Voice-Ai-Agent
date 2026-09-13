"""Directory relationships must survive natural phrasing without losing constraints."""

import pytest

from app.services import knowledge_retrieval
from app.services.knowledge_retrieval import _rank_contextual_knowledge, rank_knowledge

DOCTORS = """VERIFIED STRUCTURED FACTS

SUBJECT: Dr Mira Anwar
- specialty: Orthopedic Surgery
  Search phrases: orthopedic doctor | orthopedic specialist
  Evidence: Dr Mira Anwar, Specialist Orthopedic Surgery.

SUBJECT: Dr Kareem Nasser
- specialty: Specialist Urologist
  Search phrases: urology doctor | urologist
  Evidence: Dr Kareem Nasser, Specialist Urologist.
"""
DEPARTMENTS = "Orthopedic Department. Urological Surgery Department. Dental Department."
DOCUMENTS = [("Our Doctors", DOCTORS), ("Medical Departments", DEPARTMENTS)]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("orthopedic department doctors", "Dr Mira Anwar"),
        ("Which doctors are available in the orthopedic department?", "Dr Mira Anwar"),
        ("Which doctor works in orthopedics?", "Dr Mira Anwar"),
        ("Which doctor works in the orthopedic department?", "Dr Mira Anwar"),
        ("Who are the specialists working in urology?", "Dr Kareem Nasser"),
        ("urology department doctors", "Dr Kareem Nasser"),
        ("Which doctor works in urology?", "Dr Kareem Nasser"),
    ],
)
def test_directory_relationship_reaches_person_record(query, expected):
    matches = rank_knowledge(query, DOCUMENTS)
    assert matches
    assert expected in matches[0].text


@pytest.mark.parametrize(
    "specialty,caller,expected",
    [
        ("orthopedic", "Which doctor works in that department?", "Dr Mira Anwar"),
        ("urology", "Which doctor works in urology?", "Dr Kareem Nasser"),
    ],
)
def test_actual_failed_tool_query_shape_includes_doctor_evidence(specialty, caller, expected):
    # Company scope has already been checked and stripped by the pinned-release path.
    matches = _rank_contextual_knowledge(
        (
            f"{specialty} department doctors",
            caller,
            f"Which doctors are available in the {specialty} department?",
        ),
        DOCUMENTS,
        4,
    )
    assert matches
    assert expected in matches[0].text


@pytest.mark.parametrize(
    "query",
    [
        "Which doctor works in oncology?",
        "Which orthopedic doctor works on Fridays?",
        "Which orthopedic doctor works in Dubai?",
        "Which orthopedic doctor has ten years experience?",
        "Which urology doctor performs robotic surgery?",
        "Which orthopedic doctor has a work permit?",
        "Which doctor works in that department?",
    ],
)
def test_relationship_normalization_preserves_unverified_constraints(query):
    assert rank_knowledge(query, [("Our Doctors", DOCTORS)]) == []


def test_department_is_not_globally_a_stopword():
    assert (
        rank_knowledge("What department handles refunds?", [("Policy", "Refunds cost ten.")]) == []
    )


def test_named_doctor_must_still_match_the_specialty():
    matches = rank_knowledge("Does Dr Mira work in the urology department?", DOCUMENTS)
    assert not any("Dr Kareem Nasser" in match.text for match in matches)


def test_raw_directory_text_supports_the_same_relationship():
    matches = rank_knowledge(
        "Which doctor works in the urology department?",
        [("Doctors", "Dr Kareem Nasser | Specialist Urologist | Arabic and English")],
    )
    assert matches
    assert "Dr Kareem Nasser" in matches[0].text


@pytest.mark.parametrize(
    "query",
    [
        "Which orthopedic doctor has a work permit?",
        "Which doctor works in orthopedics and has a work permit?",
    ],
)
def test_work_modifier_is_not_discarded_with_the_relationship(query):
    parking = """VERIFIED STRUCTURED FACTS
SUBJECT: Dr Mira Anwar
- role: orthopedic doctor
- permit: parking permit
"""
    assert rank_knowledge(query, [("Our Doctors", parking)]) == []
    work = parking.replace("parking permit", "work permit")
    assert rank_knowledge(query, [("Our Doctors", work)])


@pytest.mark.parametrize(
    "query",
    [
        "Which doctor does not work in orthopedics?",
        "Which doctor doesn't work in orthopedics?",
        "Which doctor never works in orthopedics?",
        "Doctors excluding the orthopedic department",
    ],
)
@pytest.mark.parametrize("structured", [True, False])
def test_negative_relationship_does_not_return_positive_evidence(query, structured):
    content = DOCTORS if structured else "Dr Mira Anwar works in orthopedics."
    assert rank_knowledge(query, [("Doctors", content)]) == []


@pytest.mark.parametrize(
    "query",
    [
        "Which department does doctor Lee work in?",
        "In what department does Dr Lee work?",
        "What is Dr Lee's department?",
        "Give me the department of Dr Lee",
    ],
)
def test_requested_department_requires_department_evidence(query):
    content = "VERIFIED STRUCTURED FACTS\n\nSUBJECT: Dr Lee\n- specialty: Cardiology\n"
    assert rank_knowledge(query, [("Doctors", content)]) == []
    with_department = content + "- department: Cardiology department\n"
    assert rank_knowledge(query, [("Doctors", with_department)])


def test_bounded_excerpt_uses_same_relationship_tokens_as_final_rank(monkeypatch):
    observed = []
    original = knowledge_retrieval._query_aware_excerpt

    def capture(content, query_tokens, *, limit):
        observed.append(query_tokens.copy())
        return original(content, query_tokens, limit=limit)

    monkeypatch.setattr(knowledge_retrieval, "_query_aware_excerpt", capture)
    monkeypatch.setattr(knowledge_retrieval, "MAX_RANKING_SOURCE_CHARS", 900)
    content = (
        "Doctors work in the department. "
        + "Unrelated navigation. " * 300
        + "Dr Mira Anwar is the orthopedic doctor. "
        + "Unrelated footer. " * 300
    )
    matches = knowledge_retrieval._rank_bounded_knowledge(
        "Which doctor works in the orthopedic department?", [("Doctors", content)]
    )
    assert observed and all(
        "work" not in tokens and "department" not in tokens for tokens in observed
    )
    assert matches and "Dr Mira Anwar" in matches[0].text
