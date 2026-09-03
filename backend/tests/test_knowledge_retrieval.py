"""Focused tests for provider-neutral knowledge ranking."""

from collections import Counter

import pytest

from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource
from app.services import knowledge_retrieval
from app.services.knowledge_retrieval import build_contextual_query_plan, rank_knowledge


def test_contextual_plan_recovers_domain_phrase_without_changing_raw_query():
    plan = build_contextual_query_plan(
        "Can you explain chemical feeling?",
        terminology=("Chemical Peeling", "Laser Hair Removal"),
    )

    assert plan.primary_query == "Can you explain chemical feeling?"
    assert plan.variants[0] == "Can you explain chemical feeling?"
    assert "Can you explain Chemical Peeling?" in plan.variants
    assert plan.recovered_terms == ("Chemical Peeling",)


def test_contextual_plan_does_not_turn_medical_term_into_operational_term():
    plan = build_contextual_query_plan(
        "Do you provide cancer treatment?",
        terminology=("Appointment Cancellation Policy",),
    )

    assert plan.variants == ("Do you provide cancer treatment?",)
    assert plan.recovered_terms == ()


def test_contextual_plan_recovers_one_uncertain_word_inside_known_entity():
    plan = build_contextual_query_plan(
        "Who is the chairman of Al Sabah Group?",
        terminology=("Al Zaabi Group",),
    )

    assert "Who is the chairman of Al Zaabi Group?" in plan.variants
    assert plan.recovered_terms == ("Al Zaabi Group",)


def test_contextual_plan_recovers_joined_spoken_brand_from_proper_name():
    plan = build_contextual_query_plan(
        "Do you have information about Sengobin?",
        terminology=("Saint Gobain Weber", "Appointment Cancellation Policy"),
    )

    assert "Do you have information about Saint Gobain?" in plan.variants
    assert plan.recovered_terms == ("Saint Gobain",)


def test_ranking_matches_compound_brand_name_from_short_stt_variant():
    matches = rank_knowledge(
        "Alzab",
        [
            (
                "The Group - Al Zaabi Group.pdf",
                "The organisation has operated diversified businesses in the UAE for many years.",
            )
        ],
    )

    assert matches
    assert matches[0].source == "The Group - Al Zaabi Group.pdf"


def test_ranking_never_fuzzy_matches_medical_cancer_to_cancellation_content():
    matches = rank_knowledge(
        "cancer",
        [
            (
                "Appointment Cancellation Policy.pdf",
                "Patients may cancel or reschedule an appointment with advance notice.",
            )
        ],
    )

    assert matches == []


def test_ranking_requires_two_distinct_terms_for_longer_queries():
    weak_matches = rank_knowledge(
        "cancer treatment specialist",
        [
            (
                "Treatment Policy.pdf",
                "A treatment appointment can be changed by contacting reception.",
            )
        ],
    )
    supported_matches = rank_knowledge(
        "cancer treatment specialist",
        [
            (
                "Oncology Treatment Guide.pdf",
                "Cancer treatment is planned by the clinical care team after consultation.",
            )
        ],
    )

    assert weak_matches == []
    assert supported_matches


def test_ranking_requires_content_evidence_for_topic_not_explained_by_title():
    matches = rank_knowledge(
        "cancer Alzab group",
        [
            (
                "The Group - Al Zaabi Group.pdf",
                "The organisation operates real-estate and hospitality businesses in the UAE.",
            )
        ],
    )

    assert matches == []


def test_ranking_requires_each_substantive_topic_not_explained_by_title():
    matches = rank_knowledge(
        "cancer treatment Alzab group",
        [
            (
                "The Group - Al Zaabi Group.pdf",
                "General treatment appointments support the real-estate and hospitality teams.",
            )
        ],
    )

    assert matches == []


def test_phone_number_query_matches_tel_labeled_contact_evidence():
    matches = rank_knowledge(
        "What is the contact address and phone number for Al Zaabi Group?",
        [
            (
                "Contact – Al Zaabi Group",
                "Al Zaabi Group. Office No 403 and 404, Al Reem Plaza, Electra Street, "
                "Abu Dhabi UAE. Tel: +971 2 665 9998. Fax: +971 2 665 9994. "
                "Email: info@alzaabigroup.com.",
            )
        ],
    )

    assert matches
    assert "+971 2 665 9998" in matches[0].text


def test_phone_query_excludes_other_organizations_on_same_contact_page():
    matches = rank_knowledge(
        "What is the contact address and phone number for Al Zaabi Group?",
        [
            (
                "Contact – Al Zaabi Group",
                "Al Zaabi Group. Office No 403 and 404, Al Reem Plaza, Electra Street, "
                "Abu Dhabi UAE. Tel: +971 2 665 9998.\n\n"
                "Adam and Eve Medical Center. Pink Building, Abu Dhabi. "
                "Tel: +971 2 6767 366. Mobile: +971 52 1555 366.",
            )
        ],
    )

    assert matches
    assert all("+971 2 6767 366" not in match.text for match in matches)
    assert all("+971 52 1555 366" not in match.text for match in matches)


def test_bounded_corpus_keeps_requested_contact_subject_before_excerpting():
    contact_content = (
        "SOURCE TITLE: Contact – Al Zaabi Group\n"
        "SOURCE URL: https://www.alzaabigroup.com/contact/\n\n"
        "VERIFIED STRUCTURED FACTS\n\n"
        "SUBJECT: Al Zaabi Group\n"
        "- physical-address: Office No 403 & 404 Al Reem Plaza, Electra Street, "
        "Abu Dhabi UAE\n"
        "- primary-telephone: +971 2 665 9998\n\n"
        + "\n\n".join(
            f"SUBJECT: Other Company {index}\n"
            f"- physical-address: Other address {index}, Abu Dhabi UAE\n"
            f"- primary-telephone: +971 2 700 {index:04d}\n"
            + ("Other approved details. " * 35)
            for index in range(20)
        )
        + "\n\nSOURCE CONTENT\nLong raw contact page."
    )
    documents = [("Contact – Al Zaabi Group", contact_content)] + [
        (f"Approved source {index}", "General approved business information. " * 120)
        for index in range(51)
    ]

    matches = knowledge_retrieval._rank_bounded_knowledge(
        "What is the contact address and phone number for Al Zaabi Group?",
        documents,
    )

    assert matches
    assert "Office No 403 & 404 Al Reem Plaza" in matches[0].text
    assert "+971 2 665 9998" in matches[0].text
    assert "Other Company" not in matches[0].text


def test_phone_query_requires_phone_evidence_not_only_contact_title():
    matches = rank_knowledge(
        "What is the phone number for Al Zaabi Group?",
        [
            (
                "Contact – Al Zaabi Group",
                "Use the website form to send Al Zaabi Group a general enquiry.",
            )
        ],
    )

    assert matches == []


def test_non_phone_number_remains_a_required_substantive_term():
    matches = rank_knowledge(
        "What is the customer account number?",
        [("Customer accounts", "Contact the accounts team for general assistance.")],
    )

    assert matches == []


def test_ranking_handles_compound_stt_brand_variant_and_follow_up_fillers():
    matches = rank_knowledge(
        "Okay, can you tell me more about Alzab group please?",
        [
            (
                "Al Zaabi Group - Social Media - voice-searchable.pdf",
                "Al Zaabi Group | Home | Facebook | Instagram | LinkedIn | YouTube | Share",
            ),
            (
                "Al Zaabi Group - Company Overview - voice-searchable.pdf",
                "The Al Zaabi Group is a diversified UAE business group. Its operating companies "
                "serve healthcare, trading, real estate, and other sectors through dedicated "
                "divisions with a shared commitment to customers and long-term growth.",
            ),
            (
                "Al Zaabi Group - Cosmetics Offers - voice-searchable.pdf",
                "Al Zaabi Group publishes selected cosmetics promotions and seasonal treatment "
                "offers for customers through its approved clinic channels.",
            ),
        ],
    )

    assert matches
    assert matches[0].source == "Al Zaabi Group - Company Overview - voice-searchable.pdf"


def test_ranking_recognizes_the_group_title_without_boosting_group_suffixes():
    matches = rank_knowledge(
        "Please tell me about Alzab group",
        [
            (
                "Social Media - Al Zaabi Group - voice-searchable.pdf",
                "Al Zaabi Group | Home | Facebook | Instagram | LinkedIn | YouTube | Share",
            ),
            (
                "The Group - Al Zaabi Group - voice-searchable.pdf",
                "This corporate profile explains the organisation's history, purpose, operating "
                "model, leadership principles, and long-term contribution across the UAE.",
            ),
            (
                "Divisions - Al Zaabi Group - voice-searchable.pdf",
                "The healthcare, trading, real estate, and hospitality divisions operate through "
                "dedicated companies with specialist teams and customer services.",
            ),
            (
                "Cosmetics Offers - Al Zaabi Group - voice-searchable.pdf",
                "Selected cosmetics promotions are published through approved clinic channels.",
            ),
        ],
    )

    assert {match.source for match in matches[:2]} == {
        "The Group - Al Zaabi Group - voice-searchable.pdf",
        "Divisions - Al Zaabi Group - voice-searchable.pdf",
    }
    social_score = next(match.score for match in matches if match.source.startswith("Social Media"))
    assert all(match.score > social_score for match in matches[:2])


def test_ranking_ignores_generic_source_title_suffixes():
    matches = rank_knowledge(
        "Is it voice searchable?",
        [
            ("Clinic Hours - voice-searchable.pdf", "The clinic opens daily at nine."),
            ("Doctor Directory - voice-searchable.pdf", "Dr Rao works in dermatology."),
        ],
    )

    assert matches == []


def test_ranking_penalizes_thin_navigation_and_boosts_divisions_source():
    matches = rank_knowledge(
        "What are the Alzab group divisions?",
        [
            (
                "Al Zaabi Group - Social Links - voice-searchable.pdf",
                "Al Zaabi Group | Divisions | Home | Facebook | Instagram | LinkedIn | YouTube | "
                "Share",
            ),
            (
                "Al Zaabi Group - Divisions - voice-searchable.pdf",
                "Al Zaabi Group operates several divisions, including healthcare, medical centres, "
                "trading, real estate, and hospitality. Each division has dedicated operating "
                "companies, management teams, services, and customer contact channels.",
            ),
        ],
    )

    assert matches[0].source == "Al Zaabi Group - Divisions - voice-searchable.pdf"
    assert matches[0].score > matches[1].score


def test_ranking_limits_each_source_to_two_chunks():
    detailed_paragraph = (
        "Botox treatment guidance covers consultation, suitability, preparation, aftercare, and "
        "clinic follow-up. "
    ) * 6
    matches = rank_knowledge(
        "Botox treatment guidance",
        [
            ("Detailed Botox Guide.pdf", "\n\n".join([detailed_paragraph] * 4)),
            (
                "Botox Safety FAQ.pdf",
                "Botox treatment requires a clinician consultation and approved aftercare "
                "guidance.",
            ),
            (
                "Clinic Treatment Overview.pdf",
                "The treatment overview lists Botox consultations among the clinic services.",
            ),
        ],
        limit=6,
    )

    counts = Counter(match.source for match in matches)
    assert counts["Detailed Botox Guide.pdf"] == 2
    assert max(counts.values()) <= 2
    assert len(counts) >= 2


def test_ranking_preserves_doctor_directory_routing():
    matches = rank_knowledge(
        "Which doctors are available?",
        [
            ("PRP_Treatment.pdf", "Appointments are available at the clinic."),
            (
                "Adam_and_Eve_Doctors_Directory.pdf",
                "Dr Rao — Dermatology. Dr Khan — Plastic Surgery.",
            ),
        ],
    )

    assert matches[0].source == "Adam_and_Eve_Doctors_Directory.pdf"
    assert "Dr Rao" in matches[0].text


def test_ranking_drops_filler_only_follow_up():
    assert (
        rank_knowledge(
            "Okay, yes, please tell me more about it.",
            [("General Information.pdf", "Please tell us what information you need.")],
        )
        == []
    )


def test_ranking_does_not_reject_pronoun_business_follow_up():
    matches = rank_knowledge(
        "Do they have a building materials division?",
        [
            (
                "Al Zaabi Trading - Al Zaabi Group",
                "The trading division distributes construction and building materials across "
                "Abu Dhabi and Sharjah.",
            )
        ],
    )

    assert matches
    assert "building materials" in matches[0].text


def test_ranking_treats_leadership_words_as_one_information_intent():
    matches = rank_knowledge(
        "Al Zaabi Group hierarchy management chairman leadership",
        [
            (
                "Management - Al Zaabi Group",
                "Saeed Yousif Ibrahim Al Zaabi is identified in the approved Chairman's Message.",
            )
        ],
    )

    assert matches
    assert "Saeed Yousif" in matches[0].text


@pytest.mark.asyncio
async def test_retrieval_bounds_candidates_and_offloads_ranking(db, tenant, monkeypatch):
    agent = Agent(
        tenant_id=tenant.id,
        name="Bounded retrieval agent",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Bounded retrieval knowledge",
        approval_status="approved",
        is_active=True,
        content=(
            "Clinic hours are part of the approved workspace summary. "
            + "Approved reception schedule. " * 500
        ),
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add_all(
        [
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                source_type="text",
                name=f"Clinic Hours {index}",
                status="indexed",
                content=(
                    f"Clinic hours source {index} confirms reception opens at nine each day. "
                    + "Approved reception schedule and operating-hours detail. " * 300
                ),
            )
            for index in range(knowledge_retrieval.MAX_SOURCE_CANDIDATES + 20)
        ]
    )
    await db.commit()

    ranked_document_sets: list[list[tuple[str, str]]] = []
    original_bounded_ranking_documents = knowledge_retrieval._bounded_ranking_documents

    def capture_bounded_ranking_documents(query, documents):
        ranked_documents = original_bounded_ranking_documents(query, documents)
        ranked_document_sets.append(ranked_documents)
        return ranked_documents

    async def capture_to_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(
        knowledge_retrieval,
        "_bounded_ranking_documents",
        capture_bounded_ranking_documents,
    )
    monkeypatch.setattr(knowledge_retrieval.asyncio, "to_thread", capture_to_thread)

    context = await knowledge_retrieval.retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="clinic hours",
    )

    assert context is not None
    assert ranked_document_sets
    ranked_documents = ranked_document_sets[0]
    assert len(ranked_documents) <= knowledge_retrieval.MAX_SOURCE_CANDIDATES + 1
    assert sum(len(content) for _, content in ranked_documents) <= (
        knowledge_retrieval.MAX_RANKING_CORPUS_CHARS
    )
    assert all(
        len(content) <= knowledge_retrieval.MAX_RANKING_SOURCE_CHARS
        for _, content in ranked_documents
    )
    allocated_lengths = [len(content) for _, content in ranked_documents]
    assert max(allocated_lengths) - min(allocated_lengths) <= 1
    assert any(source == knowledge_base.name for source, _ in ranked_documents)


@pytest.mark.asyncio
async def test_retrieval_keeps_query_evidence_near_end_of_large_source(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Long source retrieval agent",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Long treatment knowledge",
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Treatment Guide",
            status="indexed",
            content=(
                "Botox | Treatments | Botox | Home | Contact. "
                + "General clinic navigation and introductory information. " * 220
                + "Botox consultation requires an assessment by an approved clinician."
            ),
        )
    )
    await db.commit()

    context = await knowledge_retrieval.retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Botox consultation",
    )

    assert context is not None
    assert "Botox consultation requires an assessment" in context


@pytest.mark.asyncio
async def test_retrieval_uses_recovered_source_terminology_as_an_alternative(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Cosmetic centre agent",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Cosmetic centre knowledge",
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Chemical Peeling Treatment",
            status="indexed",
            content=(
                "Chemical peeling is a clinician-led cosmetic treatment. A consultation is "
                "required to assess suitability and explain aftercare."
            ),
        )
    )
    await db.commit()

    context = await knowledge_retrieval.retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Tell me about chemical feeling",
        terminology=("Chemical Peeling Treatment",),
    )

    assert context is not None
    assert "Contextual terminology considered: Chemical Peeling" in context
    assert "clinician-led cosmetic treatment" in context


@pytest.mark.asyncio
async def test_agent_terminology_is_derived_from_bound_approved_source_metadata(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Clinic receptionist",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Aesthetic Clinic Knowledge",
        scope_label="Cosmetic Centre",
        tags=["PRP", "dermatology"],
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Chemical Peeling - voice-searchable.pdf",
            location="https://clinic.example/treatments/platelet-rich-plasma",
            status="indexed",
            content="Arbitrary prose must not be treated as a correction vocabulary.",
            source_metadata={"page_title": "Dr Asha Dermatology Directory"},
        )
    )
    await db.commit()

    terminology = await knowledge_retrieval.load_agent_knowledge_terminology(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        hints=(agent.name,),
    )

    assert "Aesthetic Clinic Knowledge" in terminology
    assert "Chemical Peeling voice-searchable" in terminology
    assert "clinic.example treatments platelet-rich-plasma" in terminology
    assert "Dr Asha Dermatology Directory" in terminology
    assert not any("Arbitrary prose" in value for value in terminology)


@pytest.mark.asyncio
async def test_agent_terminology_includes_bounded_proper_names_from_approved_content(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Trading concierge",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Trading knowledge",
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Trading partners",
            status="indexed",
            content=(
                "The approved distribution portfolio includes Saint Gobain Weber, HENKEL, "
                "and Makita products. Ordinary prose must never become a speech alias."
            ),
        )
    )
    await db.commit()

    terminology = await knowledge_retrieval.load_agent_knowledge_terminology(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
    )

    assert "Saint Gobain Weber" in terminology
    assert "HENKEL" in terminology
    assert not any("Ordinary prose" in value for value in terminology)


@pytest.mark.asyncio
async def test_contact_query_routes_to_verified_contact_source_despite_misheard_name(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Corporate concierge",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Al Zaabi Group knowledge",
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add_all(
        [
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                source_type="website",
                name="Contact - Al Zaabi Group",
                location="https://www.alzaabigroup.com/contact/",
                status="indexed",
                content=(
                    "Contact Al Zaabi Group. Telephone: +971 2 665 9998. "
                    "Email: info@alzaabigroup.com. Abu Dhabi, United Arab Emirates."
                ),
            ),
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                source_type="website",
                name="Company overview",
                status="indexed",
                content="Al Zaabi Group operates several divisions across the UAE.",
            ),
        ]
    )
    await db.commit()

    context = await knowledge_retrieval.retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Can you share the contact details for Alzavi Group?",
        terminology=("Al Zaabi Group",),
    )

    assert context is not None
    assert "+971 2 665 9998" in context
    assert "info@alzaabigroup.com" in context


@pytest.mark.asyncio
async def test_spoken_brand_variant_retrieves_only_evidenced_approved_source(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Trading concierge",
        system_prompt="Use only approved knowledge.",
    )
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Trading knowledge",
        approval_status="approved",
        is_active=True,
    )
    db.add_all([agent, knowledge_base])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            source_type="website",
            name="Building materials partners",
            status="indexed",
            content=(
                "Saint Gobain Weber supplies approved construction and building materials "
                "through the trading division."
            ),
        )
    )
    await db.commit()

    terminology = await knowledge_retrieval.load_agent_knowledge_terminology(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
    )
    context = await knowledge_retrieval.retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Do you have information about Sengobin?",
        terminology=terminology,
    )

    assert context is not None
    assert "Contextual terminology considered: Saint Gobain" in context
    assert "construction and building materials" in context


def test_query_excerpt_uses_token_boundaries_and_prefers_substantive_tail():
    content = (
        "Hair | Home | Hair | Chair catalogue. "
        + "General navigation and introductory information. " * 220
        + "The approved hair restoration consultation includes a clinician assessment."
    )

    excerpt = knowledge_retrieval._query_aware_excerpt(
        content,
        {"hair", "restoration"},
        limit=800,
    )

    assert "approved hair restoration consultation" in excerpt
    assert knowledge_retrieval._bounded_token_position("chair only", "hair") is None


def test_query_excerpt_keeps_substantive_middle_between_navigation_and_footer():
    content = (
        "Botox treatment | Home | Contact. "
        + "Introductory filler without treatment details. " * 110
        + "Botox treatment is clinician-led and starts with a suitability assessment. "
        + "More generic site filler. " * 160
        + "Footer | Botox treatment | Privacy | Contact"
    )

    excerpt = knowledge_retrieval._query_aware_excerpt(
        content,
        {"botox", "treatment"},
        limit=800,
    )

    assert "clinician-led" in excerpt
