"""Regression contract for real failures, not company-specific answer overrides."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.agent import KnowledgeBase, KnowledgeSource
from app.services.knowledge_quality import source_quality
from app.services.knowledge_retrieval import (
    _source_retrieval_documents,
    rank_knowledge,
    retrieve_knowledge_context,
)
from app.services.knowledge_serving import (
    KnowledgeServingError,
    publish_serving_revision,
    validate_serving_revision_integrity,
)
from app.services.speech_lexicon import publish_speech_lexicon


def structured(person="Dr Kevin", role="dental doctor"):
    return {
        "facts": [
            {
                "subject": person,
                "predicate": "role",
                "value": role,
                "evidence": f"{person} is the {role}",
                "search_phrases": [f"Who is the {role}?", f"What's the name of your {role}?"],
            }
        ]
    }


@pytest.mark.parametrize(
    "query",
    [
        "Who is the dental doctor?",
        "What's the name of your dental doctor?",
        "Tell me the name of the dental doctor",
        "I am looking for a dental doctor",
    ],
)
def test_equivalent_person_queries_find_the_same_fact(query):
    docs = _source_retrieval_documents(
        name="Dental Doctor",
        content="Dr Kevin is the dental doctor",
        structured_content=structured(),
    )
    result = rank_knowledge(query, docs)
    assert result and "Dr Kevin" in result[0].text


def test_person_subject_does_not_need_to_be_company_subject():
    args = dict(
        name="Dental Doctor",
        content="Dr Kevin is the dental doctor",
        structured_content=structured(),
        company_subject="Royal Medical Center",
    )
    assert not _source_retrieval_documents(**args)  # mixed-company legacy stays fenced
    assert not _source_retrieval_documents(**args, owner_company="Other Clinic")
    assert _source_retrieval_documents(**args, owner_company="Royal Medical Center")


def test_no_medical_topic_is_invented_by_request_normalization():
    docs = _source_retrieval_documents(
        name="Dental Doctor",
        content="Dr Kevin is the dental doctor",
        structured_content=structured(),
    )
    assert not rank_knowledge("What is the name of your cancer specialist?", docs)
    assert not rank_knowledge("What is the dental doctor's consultation fee?", docs)


@pytest.mark.parametrize(
    "company,service",
    [("Royal Medical Center", "dermatology"), ("Future Supply Company", "delivery")],
)
def test_service_question_framing_preserves_topic_and_constraints(company, service):
    facts = {
        "facts": [
            {
                "subject": company,
                "predicate": "service offering",
                "value": service,
                "evidence": f"{company} offers {service}.",
            }
        ]
    }
    docs = _source_retrieval_documents(
        name="Business services", content=f"{company} offers {service}.", structured_content=facts
    )
    for query in [
        f"Does {company} offer {service}?",
        f"Does {company} provide {service}?",
        f"Is {service} available at {company}?",
    ]:
        assert rank_knowledge(query, docs), query
    for query in [
        f"Does {company} offer cancer treatment?",
        f"Does {company} offer free {service}?",
        f"Is {service} available tomorrow at 10?",
        f"What is the {service} fee at {company}?",
    ]:
        assert not rank_knowledge(query, docs), query


def test_departments_and_doctor_directory_are_retrievable_without_live_slot_claims():
    doctors = [("Our team", "Dr Asha Rao — Dental doctor. Dr Sam Ali — ENT specialist.")]
    assert rank_knowledge("Which doctors are available?", doctors)
    assert not rank_knowledge("Which doctors are available tomorrow at 10?", doctors)
    departments = [
        (
            "Services",
            "ENT Department provides ear, nose and throat care. "
            "Dental Department provides dental care.",
        )
    ]
    assert rank_knowledge("I am looking for an ENT department.", departments)
    assert rank_knowledge("What departments are available?", departments)


def test_empty_doctor_directory_is_not_usable_but_short_plain_text_is():
    s = SimpleNamespace(
        raw_content=None,
        content="Home Doctors Specialties Login Register",
        source_metadata=None,
        status="indexed",
        source_type="website",
        name="Our Doctors",
        location="https://example.com/doctors",
        structured_content=None,
    )
    assert source_quality(s)[0] == "needs_repair"
    s.source_type = "text"
    s.content = "Dr Kevin is the dental doctor"
    s.structured_content = structured()
    assert source_quality(s)[0] == "not_tested"


@pytest.mark.asyncio
async def test_long_empty_directory_shell_automatically_renders(monkeypatch):
    from app.services import website_recovery as recovery

    shell = (
        "<title>Our doctors</title><main><p>"
        + ("Find trusted medical professionals for quality healthcare and consultations. " * 30)
        + "</p></main>"
    )
    calls = []

    async def download(url):
        return url, shell, len(shell)

    async def render(url):
        calls.append(url)
        html = (
            "<title>Our doctors</title><main><p>Dr Asha Rao is our dental doctor. "
            + ("She provides dental consultations at the clinic. " * 5)
            + "</p></main>"
        )
        return html, len(html)

    monkeypatch.setattr(recovery, "download_html", download)
    monkeypatch.setattr(recovery, "render_html", render)
    result = await recovery.recover_page("https://clinic.example/doctors")
    assert calls and result.method == "javascript_render" and "Dr Asha Rao" in result.text

    async def still_empty(url):
        return shell, len(shell)

    monkeypatch.setattr(recovery, "render_html", still_empty)
    with pytest.raises(recovery.WebsiteRecoveryError, match="directory entries"):
        await recovery.recover_page("https://clinic.example/doctors")


@pytest.mark.asyncio
async def test_owned_publication_checks_natural_service_questions(db, tenant):
    company = "Example Medical Center"
    phrases = [
        f"Does {company} offer dermatology?",
        f"Is dermatology available at {company}?",
        f"What dermatology services does {company} offer?",
    ]
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name=company + " One Day Surgery",
        owner_company=company + " One Day Surgery",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Our clinicians",
        status="indexed",
        content=f"{company} offers dermatology.",
        structured_content={
            "facts": [
                {
                    "subject": company,
                    "predicate": "service offering",
                    "value": "dermatology",
                    "evidence": f"{company} offers dermatology.",
                    "search_phrases": phrases,
                }
            ]
        },
    )
    kb.sources.append(source)
    db.add(kb)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db, tenant_id=tenant.id, knowledge_base=kb, allow_draft_for_approval=True
    )
    await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    assert kb.readiness_report[str(source.id)]["checks_count"] == 3
    assert kb.readiness_report[str(source.id)]["status"] == "passed"


@pytest.mark.asyncio
async def test_owned_person_retrieval_accepts_unambiguous_company_name_prefix(db, tenant):
    company = "Example Medical Center One Day Surgery"
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name=company,
        owner_company=company,
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    kb.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Dental Doctor",
            status="indexed",
            content="Dr Kevin is the dental doctor",
            structured_content=structured(),
        )
    )
    db.add(kb)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db, tenant_id=tenant.id, knowledge_base=kb, allow_draft_for_approval=True
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    for scope in (None, company):
        for subject in (company, "Example Medical Center"):
            result = await retrieve_knowledge_context(
                db,
                tenant_id=tenant.id,
                agent_id=uuid4(),
                knowledge_base_id=kb.id,
                serving_revision_id=revision.id,
                company_subject=scope,
                query=f"Who is the dental doctor at {subject}?",
            )
            assert result and "Dr Kevin" in result
        result = await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=uuid4(),
            knowledge_base_id=kb.id,
            serving_revision_id=revision.id,
            company_subject=scope,
            query="Who is the dental doctor at Different Medical Center?",
        )
        assert not result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "company,person,role",
    [
        ("Royal Medical Center", "Dr Kevin", "dental doctor"),
        ("Future Trading Company", "Ms Asha", "accounts manager"),
    ],
)
async def test_published_owned_source_works_and_remains_revision_pinned(
    db, tenant, company, person, role
):
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name=company,
        owner_company=company,
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Staff contact",
        content=f"{person} is the {role}",
        structured_content=structured(person, role),
        status="indexed",
    )
    kb.sources.append(source)
    db.add(kb)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db, tenant_id=tenant.id, knowledge_base=kb, allow_draft_for_approval=True
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    assert kb.readiness_report[str(source.id)]["status"] == "passed"
    assert revision.manifest["owner_company"] == company
    validate_serving_revision_integrity(revision, lexicon)
    from app.api.v1.endpoints.knowledge import _source_response

    report = kb.readiness_report[str(source.id)]
    assert (
        _source_response(
            source, published_revision_id=revision.id, owner_company=company, check=report
        ).quality_status
        == "sample_checks_passed"
    )
    assert (
        _source_response(
            source,
            published_revision_id=revision.id,
            owner_company="Different Company",
            check=report,
        ).quality_status
        != "sample_checks_passed"
    )
    kb.owner_company = "Different Company"
    source.content = "Changed draft with no previous name"
    assert (
        _source_response(
            source, published_revision_id=revision.id, owner_company=company, check=report
        ).quality_status
        != "sample_checks_passed"
    )
    await db.flush()
    for query in [f"What's the name of your {role}?", f"{company}. Who is the {role}?"]:
        context = await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=uuid4(),
            query=query,
            knowledge_base_id=kb.id,
            serving_revision_id=revision.id,
            company_subject=company,
        )
        assert context and person in context
    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=uuid4(),
            query=f"Who is the {role}?",
            knowledge_base_id=kb.id,
            serving_revision_id=revision.id,
            company_subject="Different Company",
        )
        is None
    )
    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=uuid4(),
            agent_id=uuid4(),
            query=f"Who is the {role}?",
            knowledge_base_id=kb.id,
            serving_revision_id=revision.id,
            company_subject=company,
        )
        is None
    )


@pytest.mark.asyncio
async def test_failed_retrieval_check_does_not_move_live_pointer(db, tenant, monkeypatch):
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name="Test Clinic",
        owner_company="Test Clinic",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    kb.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Staff",
            content="Dr Kevin is the dental doctor",
            structured_content=structured(),
            status="indexed",
        )
    )
    db.add(kb)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db, tenant_id=tenant.id, knowledge_base=kb, allow_draft_for_approval=True
    )
    first = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    kb.sources[0].content = "Dr James is the dental doctor"
    kb.sources[0].structured_content = structured("Dr James")
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db, tenant_id=tenant.id, knowledge_base=kb, allow_draft_for_approval=True
    )

    async def no_match(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.knowledge_retrieval.retrieve_knowledge_context", no_match)
    with pytest.raises(KnowledgeServingError, match="Retrieval check failed"):
        await publish_serving_revision(
            db,
            tenant_id=tenant.id,
            knowledge_base=kb,
            speech_lexicon=lexicon,
            allow_draft_for_approval=True,
        )
    assert kb.serving_revision_id == first.id


def test_publication_probe_compares_words_and_names_retrieved_sources():
    from app.services.knowledge_serving import _probe_missing_term, _probe_sources

    context = (
        "Source: About Us | Royal Medical Center\n"
        "VERIFIED STRUCTURED FACTS\nSUBJECT: Royal Medical Center\n"
        "- location: Al Najda Street, Abu Dhabi – UAE\n\n"
        "Source: Doctors\nSUBJECT: Dr. Hayam Aly\n- specialty: General practitioner"
    )

    assert _probe_missing_term(context, "Royal Medical Center", "Abu Dhabi, UAE") is None
    assert _probe_missing_term(context, "Royal Medical Center", "Dubai") == "Dubai"
    assert _probe_missing_term(None, "Royal Medical Center", "Abu Dhabi") == (
        "Royal Medical Center"
    )
    assert _probe_sources(context) == "'About Us | Royal Medical Center', 'Doctors'"
    assert _probe_sources(None) == "no evidence"


def _company_fact(subject, predicate, value, phrases):
    return {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "evidence": f"{subject} {predicate}: {value}",
        "search_phrases": phrases,
    }


def _item_source(tenant_id, name, prefix):
    """A directory-like source whose short facts all carry location phrasing."""
    facts = [
        _company_fact(
            f"{prefix} {index}",
            "location",
            f"Room {index}",
            [f"{prefix} {index} location", f"Where is {prefix} {index}?"],
        )
        for index in range(12)
    ]
    return KnowledgeSource(
        tenant_id=tenant_id,
        source_type="web",
        name=name,
        content="\n".join(fact["evidence"] for fact in facts),
        structured_content={"facts": facts},
        status="indexed",
    )


@pytest.mark.asyncio
async def test_company_location_question_prefers_the_company_fact_over_item_phrases(db, tenant):
    """A caller naming the clinic gets its own location fact, not a denser item fact.

    Directory pages carry many short facts whose search phrases mention
    "location" or "where". With two chunks per source and six slots, those
    denser chunks used to crowd the company's own (longer) location fact out
    of the retrieved context, so the publication probe for the About page
    failed even though the fact was compiled correctly.
    """
    company = "Royal Medical Center"
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name="Royal Medical",
        owner_company=company,
        sync_status="ready",
        approval_status="draft",
        source_count=4,
        indexed_source_count=4,
    )
    address = "Al Najda Street, opposite the central bus station, Abu Dhabi, United Arab Emirates"
    # The About page compiles the owner under its page-qualified name; the
    # company-first ranking must still recognise it as the owner.
    page_qualified = "Royal Medical Center Abu Dhabi"
    kb.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="web",
            name="About Us | Royal Medical Center Abu Dhabi",
            content=f"{page_qualified} location: {address}",
            structured_content={
                "facts": [
                    _company_fact(
                        page_qualified,
                        "location",
                        address,
                        [
                            "What is the location of Royal Medical Center?",
                            "Where is Royal Medical Center located?",
                            "Royal Medical Center address",
                            "How do I get to Royal Medical Center?",
                            "Which street is Royal Medical Center on?",
                            "Royal Medical Center directions",
                            "Is Royal Medical Center in Abu Dhabi?",
                            "Royal Medical Center location details",
                        ],
                    )
                ]
            },
            status="indexed",
        )
    )
    for name, prefix in (
        ("Doctors | Royal Medical Center Abu Dhabi", "Dr. Person"),
        ("Departments | Royal Medical Center Abu Dhabi", "Department"),
        ("Offers | Royal Medical Center Abu Dhabi", "Offer"),
    ):
        kb.sources.append(_item_source(tenant.id, name, prefix))
    # A directory page also names the owner (page-qualified) in short facts, so
    # the probe's subject appears in the context even when the About fact is
    # crowded out; only the value then reveals the gap.
    kb.sources[-1].structured_content["facts"].append(
        _company_fact(
            page_qualified,
            "department",
            "Dentistry",
            ["Which departments does Royal Medical Center have?"],
        )
    )
    db.add(kb)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db, tenant_id=tenant.id, knowledge_base=kb, allow_draft_for_approval=True
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    assert all(report["status"] == "passed" for report in kb.readiness_report.values())

    context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=uuid4(),
        query="What is the location of Royal Medical Center?",
        knowledge_base_id=kb.id,
        serving_revision_id=revision.id,
        company_subject=company,
    )
    assert context is not None
    first_block = context.split("\n\n")[0]
    assert "SUBJECT: Royal Medical Center" in first_block
    assert address in first_block


def _directory_document(specialties):
    blocks = []
    for index, specialty in enumerate(specialties):
        name = f"Dr. Person {index}"
        role = specialty.split()[-1].lower()
        blocks.append(
            "VERIFIED STRUCTURED FACTS\n"
            f"SUBJECT: {name}\n"
            f"- specialty: {specialty}\n"
            f"  Search phrases: What is {name}'s specialty? | Is {name} a {role}? | "
            f"Who is the {specialty.lower()} at Royal Medical Center?\n"
            f"  Evidence: {name} | {specialty} | Arabic, English | 10+ Years"
        )
    return "\n\n".join(blocks)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Which doctor is in urology?", "Dr. Person 0"),
        ("Do you have a urology doctor?", "Dr. Person 0"),
        ("Who is the cardiology doctor?", "Dr. Person 3"),
        ("Is there a dermatology specialist?", "Dr. Person 4"),
        ("Which doctor treats children?", "Dr. Person 6"),
        ("Do you have a psychiatry doctor?", "Dr. Person 1"),
        ("Which doctor does plastic surgery?", "Dr. Person 7"),
        ("Is there a dental doctor?", "Dr. Person 8"),
    ],
)
def test_specialty_questions_match_the_directory_card_word_forms(query, expected):
    """A caller says "urology"; the card says "Consultant Urologist"."""
    from app.services.knowledge_retrieval import (
        _rank_contextual_knowledge,
        build_contextual_query_plan,
    )

    document = _directory_document(
        [
            "Consultant Urologist",
            "Specialist Psychiatrist",
            "General Practitioner",
            "Consultant Cardiologist",
            "Specialist Dermatologist",
            "Consultant Otolaryngology",
            "Specialist Pediatrician",
            "Consultant Plastic Surgeon",
            "General Dentist",
        ]
    )
    plan = build_contextual_query_plan(query)
    matches = _rank_contextual_knowledge(
        plan.variants, [("Best Doctors Near Me in Abu Dhabi", document)], 6, "Royal Medical Center"
    )
    assert matches, query
    assert f"SUBJECT: {expected}" in matches[0].text, query


def test_specialty_forms_never_widen_ordinary_words_or_framing_nouns():
    """Codex review on #49: no "-ic" family, and framing verbs only for directory questions."""
    from app.services.knowledge_retrieval import _specialty_forms, rank_knowledge

    assert _specialty_forms("clinic") == set()
    assert _specialty_forms("electric") == set()
    assert _specialty_forms("nondental") == set()
    assert _specialty_forms("surgeon") == {"surgery", "surgical", "surgeons"}
    assert _specialty_forms("urology") == {"urologist", "urological", "urologists"}

    fees = (
        "VERIFIED STRUCTURED FACTS\nSUBJECT: Royal Medical Center\n"
        "- cancellation fee: AED 100\n  Search phrases: cancellation fee\n"
        "  Evidence: Cancellation fee: AED 100"
    )
    company = (
        "VERIFIED STRUCTURED FACTS\nSUBJECT: Royal Medical Center\n"
        "- description: a clinic in Abu Dhabi\n  Search phrases: about the clinic\n"
        "  Evidence: Royal Medical Center is a clinic in Abu Dhabi"
    )
    assert not rank_knowledge("What is the handling fee?", [("Policies", fees)])
    assert not rank_knowledge("Who is the clinician?", [("About", company)])


def _specialties_document():
    return (
        "VERIFIED STRUCTURED FACTS\n"
        "SUBJECT: Adam & Eve Specialized Medical Centre\n"
        "- specialties: Pediatrics, Pediatric Dentistry, Internal Medicine, Dermatology\n"
        "  Search phrases: What specialties does Adam & Eve Specialized Medical Centre "
        "have? | Which services are provided? | Is pediatrics available?\n"
        "  Evidence: Our Specialties: Pediatrics, Pediatric Dentistry, Internal Medicine, "
        "Dermatology\n"
        "- address: Al Nahyan, Abu Dhabi\n"
        "  Search phrases: Where is Adam & Eve Specialized Medical Centre?\n"
        "  Evidence: Located in Al Nahyan, Abu Dhabi"
    )


@pytest.mark.parametrize(
    "query",
    [
        "What kind of departments you are having there?",
        "Which departments do you have?",
        "What divisions are there?",
        "What kind of services you are providing there?",
        "What are you offering there?",
    ],
)
def test_department_questions_match_a_specialties_listing(query):
    """Adam & Eve call on 12 September: "departments" found nothing, "services" did.

    The site lists its "specialties"; a caller says "departments" and adds a
    framing verb such as "having" or "providing" that the card never states.
    """
    from app.services.knowledge_retrieval import (
        _rank_contextual_knowledge,
        build_contextual_query_plan,
    )

    plan = build_contextual_query_plan(query)
    matches = _rank_contextual_knowledge(
        plan.variants,
        [("Adam & Eve Specialized Medical Centre", _specialties_document())],
        6,
        "Adam & Eve Specialized Medical Centre",
    )
    assert matches, query
    assert "Pediatrics" in matches[0].text, query
