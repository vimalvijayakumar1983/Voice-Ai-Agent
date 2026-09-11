from app.services.knowledge_records import (
    coverage_blocks_approval,
    coverage_issue,
    coverage_report,
    make_record,
    records_from_text,
    render_records,
)


def test_text_records_keep_bullets_fields_and_prose_separate():
    records = records_from_text(
        """# Clinic FAQ
Opening hours: 9 AM to 9 PM daily
- Consultation | AED 150
- Follow-up visit | AED 100
PRP consultations are available after a doctor completes an assessment.
It takes about twenty minutes.
"""
    )

    assert [(record.kind, record.text) for record in records] == [
        ("heading", "Clinic FAQ"),
        ("field", "Opening hours: 9 AM to 9 PM daily"),
        ("list_item", "Consultation | AED 150"),
        ("list_item", "Follow-up visit | AED 100"),
        (
            "paragraph",
            "PRP consultations are available after a doctor completes an assessment. "
            "It takes about twenty minutes.",
        ),
    ]
    assert records[2].heading_path == ("Clinic FAQ",)
    assert render_records(records).count("\n\n") == 4


def test_make_record_drops_calls_to_action_and_repeated_fragments():
    record = make_record("card", ["Dr Rana", "GP", "View Profile", "GP", "Book now"])

    assert record is not None
    assert record.text == "Dr Rana | GP"
    assert make_record("card", ["Read more", "View profile"]) is None


def _doctor_records():
    return [
        make_record("heading", ["Our Doctors"]),
        make_record("card", ["Dr Randa Ahmed", "General Practitioner", "15+ Years Experience"]),
        make_record("card", ["Dr Dalia Hassan", "General Practitioner", "23+ Years Experience"]),
        make_record("paragraph", ["Royal Medical Center provides family care."]),
    ]


def test_coverage_is_partial_until_every_record_is_captured_as_facts():
    facts = [
        {
            "subject": "Dr Randa Ahmed",
            "predicate": "specialty",
            "value": "General Practitioner",
            "evidence": "Dr Randa Ahmed | General Practitioner | 15+ Years Experience",
        }
    ]
    structured = {"facts": facts, "entities": [], "validation": {"facts_accepted": 1}}

    partial = coverage_report(
        _doctor_records(), structured, requested_mode="automatic", effective_mode="ai_verified"
    )
    assert partial["status"] == "partial"
    assert partial["record_total"] == 2
    assert partial["records_covered"] == 1
    assert partial["uncovered"] == ["Dr Dalia Hassan | General Practitioner | 23+ Years Experience"]
    assert partial["entities_found"] == 2
    assert partial["entities_with_facts"] == 1
    assert coverage_blocks_approval(partial) is None

    facts.append(
        {
            "subject": "Dr Dalia Hassan",
            "predicate": "experience",
            "value": "23+ Years",
            "evidence": "Dr Dalia Hassan | General Practitioner | 23+ Years Experience",
        }
    )
    complete = coverage_report(
        _doctor_records(), structured, requested_mode="automatic", effective_mode="ai_verified"
    )
    assert complete["status"] == "complete"
    assert complete["records_covered"] == 2


def test_coverage_distinguishes_skipped_fast_mode_from_missing_compilation():
    records = _doctor_records()

    skipped = coverage_report(records, {}, requested_mode="fast", effective_mode="fast")
    missing = coverage_report(records, {}, requested_mode="automatic", effective_mode="fast")

    assert skipped["status"] == "skipped"
    assert coverage_blocks_approval(skipped) is None
    assert missing["status"] == "not_compiled"
    assert coverage_blocks_approval(missing) is not None
    assert coverage_blocks_approval(None) is None


def test_coverage_requires_one_fact_to_carry_every_field_of_a_record():
    records = [
        make_record("table_row", ["Service: Consultation", "Price: AED 100"]),
    ]
    scattered = {
        "facts": [
            {
                "subject": "Consultation",
                "predicate": "price",
                "value": "AED 150",
                "evidence": "Service: Consultation | Price: AED 150",
            },
            {
                "subject": "X-ray",
                "predicate": "price",
                "value": "AED 100",
                "evidence": "Service: X-ray | Price: AED 100",
            },
        ],
        "entities": [],
        "validation": {"facts_accepted": 2},
    }

    report = coverage_report(
        records, scattered, requested_mode="automatic", effective_mode="ai_verified"
    )

    assert report["status"] == "partial"
    assert report["records_covered"] == 0
    assert report["uncovered"] == ["Service: Consultation | Price: AED 100"]

    scattered["facts"].append(
        {
            "subject": "Consultation",
            "predicate": "price",
            "value": "AED 100",
            "evidence": "Service: Consultation | Price: AED 100",
        }
    )
    complete = coverage_report(
        records, scattered, requested_mode="automatic", effective_mode="ai_verified"
    )
    assert complete["status"] == "complete"


def test_dedupe_keeps_identical_rows_that_sit_under_different_headings():
    records = records_from_text(
        """# Branch A
Opening hours: 9 AM to 5 PM
# Branch B
Opening hours: 9 AM to 5 PM
Opening hours: 9 AM to 5 PM
"""
    )

    assert [(record.heading_path, record.text) for record in records] == [
        ((), "Branch A"),
        (("Branch A",), "Opening hours: 9 AM to 5 PM"),
        ((), "Branch B"),
        (("Branch B",), "Opening hours: 9 AM to 5 PM"),
    ]


def test_prose_only_sources_are_reported_as_unstructured_not_complete():
    records = [
        make_record("heading", ["About us"]),
        make_record("paragraph", ["Royal Medical Center has served Abu Dhabi since 2005."]),
    ]
    structured = {
        "facts": [
            {
                "subject": "Royal Medical Center",
                "predicate": "serving since",
                "value": "2005",
                "evidence": "Royal Medical Center has served Abu Dhabi since 2005.",
            }
        ],
        "entities": [],
        "validation": {"facts_accepted": 1},
    }

    report = coverage_report(
        records, structured, requested_mode="automatic", effective_mode="ai_verified"
    )

    assert report["status"] == "unstructured"
    assert report["record_total"] == 0
    assert coverage_blocks_approval(report) is None
    assert "completeness is not verified" in coverage_issue(report)


def test_a_fact_captures_a_record_when_it_carries_its_identifying_words():
    records = [
        make_record("list_item", ["Physiotherapy Department"]),
        make_record("list_item", ["Laser Department"]),
        make_record("list_item", ["Pediatric Dentistry Department"]),
    ]
    structured = {
        "facts": [
            {
                "subject": "Royal Medical Center",
                "predicate": "departments",
                "value": "Physiotherapy, Laser",
                "evidence": "Departments: Physiotherapy, Laser",
            },
            {
                "subject": "Royal Medical Center",
                "predicate": "department",
                "value": "Dentistry",
                "evidence": "Dentistry Department",
            },
        ],
        "entities": [],
        "validation": {"facts_accepted": 2},
    }

    report = coverage_report(
        records, structured, requested_mode="automatic", effective_mode="ai_verified"
    )

    assert report["records_covered"] == 2
    assert report["uncovered"] == ["Pediatric Dentistry Department"]


def test_every_uncovered_record_is_kept_for_review():
    records = [make_record("list_item", [f"Department {index}"]) for index in range(40)]
    structured = {"facts": [], "entities": [], "validation": {"facts_accepted": 0}}

    report = coverage_report(
        records, structured, requested_mode="automatic", effective_mode="ai_verified"
    )

    assert report["uncovered_total"] == 40
    assert len(report["uncovered"]) == 40
    assert "(+37 more)" in coverage_issue(report)


def test_uncovered_records_and_focused_rendering_carry_page_context():
    from app.services.knowledge_records import (
        make_record,
        render_focused_records,
        uncovered_records,
    )

    heading = make_record("heading", ["Departments"])
    dentistry = make_record("list_item", ["Dentistry"], heading_path=("Departments",))
    radiology = make_record("list_item", ["Radiology"], heading_path=("Departments",))
    card = make_record(
        "card",
        ["Dr. Hayam Aly", "General practitioner", "21+ Years"],
        heading_path=("Our Doctors",),
    )
    facts = [
        {
            "subject": "Royal Medical Center",
            "predicate": "department",
            "value": "Radiology",
            "evidence": "Departments › Radiology",
        }
    ]

    missing = uncovered_records([heading, dentistry, radiology, card], facts)

    assert [record.text for record in missing] == [dentistry.text, card.text]
    rendered = render_focused_records("Departments | Royal Medical Center Abu Dhabi", missing)
    assert rendered.split("\n\n") == [
        "Departments - Royal Medical Center Abu Dhabi › Departments › Dentistry",
        "Departments - Royal Medical Center Abu Dhabi › Our Doctors › "
        "Dr. Hayam Aly | General practitioner | 21+ Years",
    ]
    assert render_focused_records("", [dentistry]) == "Departments › Dentistry"
