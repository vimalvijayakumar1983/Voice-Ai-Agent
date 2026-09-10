from app.services.knowledge_records import (
    coverage_blocks_approval,
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
