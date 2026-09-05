from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeSource,
)
from app.models.tenant import Tenant
from app.providers.inworld import INWORLD_TTS_SUPPORTED_LANGUAGES
from app.services.speech_lexicon import (
    LANGUAGE_SCRIPT_ALLOWLIST,
    EntityResolution,
    SpeechLexiconEntry,
    SpeechLexiconError,
    backfill_approved_speech_lexicons,
    backfill_approved_speech_lexicons_batch,
    build_speech_lexicon,
    detect_unexpected_script,
    load_agent_speech_lexicon,
    publish_speech_lexicon,
    resolve_canonical_entity,
    select_provider_terms,
)


@pytest.mark.asyncio
async def test_speech_lexicon_backfill_quarantines_invalid_row_and_continues(db, tenant):
    invalid = KnowledgeBase(
        tenant_id=tenant.id,
        name="Broken legacy knowledge",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    valid = KnowledgeBase(
        tenant_id=tenant.id,
        name="Valid legacy knowledge",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    valid.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Valid source",
            content="Al Zaabi Group approved company information.",
            status="indexed",
        )
    )
    db.add_all((invalid, valid))
    await db.flush()

    result = await backfill_approved_speech_lexicons_batch(
        db,
        tenant_id=tenant.id,
        limit=2,
    )
    await db.commit()

    assert (result.selected, result.published, result.failed) == (2, 1, 1)
    await db.refresh(invalid)
    await db.refresh(valid)
    assert invalid.approval_status == "draft"
    assert invalid.sync_status == "error"
    assert "source repair" in invalid.sync_error
    assert valid.speech_lexicon_artifact_id is not None
    rerun = await backfill_approved_speech_lexicons_batch(db, tenant_id=tenant.id, limit=2)
    assert rerun.selected == 0


def _source(*, tenant_id, knowledge_base_id, name, entities, source_id=None):
    return KnowledgeSource(
        id=source_id or uuid4(),
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_type="url",
        name=name,
        location="https://example.test/source",
        content="Approved source content with customer-answerable details.",
        content_sha256="a" * 64,
        compiled_at=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
        status="indexed",
        structured_content={
            "compiler": {"version": "vav-knowledge-compiler-8"},
            "speech_entities": entities,
        },
    )


def _entry(name, *, entry_id, entity_type="service", tier=2, priority=700):
    normalized = name.casefold()
    return SpeechLexiconEntry(
        entry_id=entry_id,
        canonical=name,
        normalized=normalized,
        entity_type=entity_type,
        tier=tier,
        priority=priority,
        critical=tier == 1,
        languages=("und",),
        aliases=(),
        phonetic_keys=(),
        source_ids=("source",),
        evidence_sha256=("b" * 64,),
    )


def test_build_is_revision_deterministic_tiered_and_excludes_url_noise():
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    knowledge_base = KnowledgeBase(
        id=knowledge_base_id,
        tenant_id=tenant_id,
        name="Al Zaabi Group knowledge",
        scope_type="group",
        scope_label="Al Zaabi Group",
        languages=["en-GB"],
        tags=["corporate"],
        sync_status="ready",
        approval_status="draft",
    )
    organization_source = _source(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        name="https://example.test/about-us",
        source_id=uuid4(),
        entities=[
            {
                "canonical": "Al Zaabi Group",
                "entity_type": "organization",
                "language": "en",
                "critical": True,
                "aliases": [],
                "evidence_sha256": "1" * 64,
            },
            {
                "canonical": "Devu Vimal",
                "entity_type": "person",
                "language": "en",
                "critical": True,
                "aliases": [],
                "evidence_sha256": "2" * 64,
            },
        ],
    )
    service_source = _source(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        name="Services",
        source_id=uuid4(),
        entities=[
            {
                "canonical": "Industrial Trading",
                "entity_type": "service",
                "language": "en",
                "critical": True,
                "aliases": [],
                "evidence_sha256": "3" * 64,
            }
        ],
    )
    first = build_speech_lexicon(
        knowledge_base,
        [organization_source, service_source],
        generated_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
    )
    second = build_speech_lexicon(
        knowledge_base,
        [service_source, organization_source],
        generated_at=datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
    )

    assert first.artifact_id == second.artifact_id
    assert first.source_revision_sha256 == second.source_revision_sha256
    assert first.content_sha256 == second.content_sha256
    assert [entry.canonical for entry in first.entries] == [
        "Al Zaabi Group",
        "Devu Vimal",
        "Industrial Trading",
    ]
    assert all(entry.tier == 1 for entry in first.entries)
    assert not any("example.test" in entry.canonical for entry in first.entries)
    assert first.coverage["tier_one_coverage_pct"] == 100.0

    service_source.content = "A changed approved retrieval document."
    changed = build_speech_lexicon(knowledge_base, [organization_source, service_source])
    assert changed.artifact_id != first.artifact_id
    assert changed.source_revision_sha256 != first.source_revision_sha256
    assert changed.content_sha256 != first.content_sha256


def test_provider_selection_prioritizes_whole_tier_one_terms_and_reports_coverage():
    entries = [
        _entry("Routine service", entry_id="service", tier=2),
        _entry(
            "Al Zaabi Group",
            entry_id="company",
            entity_type="organization",
            tier=1,
            priority=1_100,
        ),
        _entry(
            "Devu Vimal",
            entry_id="person",
            entity_type="person",
            tier=1,
            priority=1_070,
        ),
        _entry("X" * 200, entry_id="too-long", tier=1, priority=1_050),
    ]

    selection = select_provider_terms(entries, max_terms=3, max_chars=40)

    assert selection.terms == ("Al Zaabi Group", "Devu Vimal")
    assert selection.entry_ids == ("company", "person")
    assert all(term == "X" * 200 or len(term) < 200 for term in selection.terms)
    assert selection.coverage["tier_one_total"] == 3
    assert selection.coverage["tier_one_selected"] == 2
    assert selection.coverage["omitted_entries"] == 2


def test_required_term_matching_an_artifact_entry_preserves_coverage_identity():
    entry = _entry(
        "Al Zaabi Group",
        entry_id="company",
        entity_type="organization",
        tier=1,
        priority=1_100,
    )

    selection = select_provider_terms(
        [entry],
        required_terms=("Al Zaabi Group",),
    )

    assert selection.terms == ("Al Zaabi Group",)
    assert selection.entry_ids == ("company",)
    assert selection.coverage["tier_one_coverage_pct"] == 100.0


def test_legacy_pdf_directory_recovers_tier_one_people_without_path_terms():
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    knowledge_base = KnowledgeBase(
        id=knowledge_base_id,
        tenant_id=tenant_id,
        name="Medical directory knowledge",
        scope_type="branch",
        scope_label="Adam and Eve Medical Center",
        languages=["en-GB"],
        sync_status="ready",
        approval_status="draft",
    )
    source = KnowledgeSource(
        id=uuid4(),
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_type="file",
        name="Adam_and_Eve_Doctors_Directory.pdf",
        location="C:/uploads/Adam_and_Eve_Doctors_Directory.pdf",
        content=(
            "Adam and Eve Medical Center doctors directory. "
            "Dr Kaveri Amal is a specialist. Dr Devu Vimal is available by appointment."
        ),
        status="indexed",
    )

    build = build_speech_lexicon(knowledge_base, [source])

    entries = {entry.canonical: entry for entry in build.entries}
    assert entries["Kaveri Amal"].entity_type == "person"
    assert entries["Kaveri Amal"].tier == 1
    assert entries["Devu Vimal"].tier == 1
    assert all("uploads" not in entry.normalized for entry in build.entries)


def test_partial_ai_entity_extraction_is_supplemented_by_safe_tier_one_fallbacks():
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    knowledge_base = KnowledgeBase(
        id=knowledge_base_id,
        tenant_id=tenant_id,
        name="Al Zaabi Group knowledge",
        scope_type="group",
        scope_label="Al Zaabi Group",
        languages=["en-GB"],
        sync_status="ready",
        approval_status="draft",
    )
    source = _source(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        name="Leadership directory",
        entities=[
            {
                "canonical": "Al Zaabi Group",
                "entity_type": "organization",
                "language": "en",
                "critical": True,
                "aliases": [],
                "evidence_sha256": "1" * 64,
            }
        ],
    )
    source.content = (
        "Al Zaabi Group leadership directory. Dr Devu Vimal is available for customer enquiries."
    )

    build = build_speech_lexicon(knowledge_base, [source])

    entries = {entry.canonical: entry for entry in build.entries}
    assert entries["Al Zaabi Group"].entity_type == "organization"
    assert entries["Devu Vimal"].entity_type == "person"
    assert entries["Devu Vimal"].tier == 1


def test_unexpected_script_detector_respects_fixed_and_multilingual_languages():
    wrong_script = detect_unexpected_script("डेव विमल कौन हैं", expected_language="en-GB")
    mixed_name = detect_unexpected_script(
        "Please give me the phone number for सईद अल ज़ाबी General Trading LLC.",
        expected_language="en-GB",
    )
    allowed_hindi = detect_unexpected_script("डेव विमल कौन हैं", expected_language="hi-IN")
    auto = detect_unexpected_script("डेव विमल कौन हैं", expected_language="auto")
    configured_multilingual = detect_unexpected_script(
        "कृपया मुझे फोन नंबर दें",
        expected_language="auto",
        allowed_languages=("en-GB", "ar-AE", "hi-IN"),
    )
    unsupported_multilingual = detect_unexpected_script(
        "Пожалуйста, дайте номер",
        expected_language="auto",
        allowed_languages=("en-GB", "ar-AE", "hi-IN"),
    )

    assert wrong_script.is_unexpected is True
    assert mixed_name.is_unexpected is True
    assert wrong_script.unexpected_scripts == ("DEVANAGARI",)
    assert allowed_hindi.is_unexpected is False
    assert auto.is_unexpected is False
    assert configured_multilingual.is_unexpected is False
    assert unsupported_multilingual.is_unexpected is True


@pytest.mark.parametrize(
    ("language", "sample"),
    (
        ("ar", "مرحبا بكم"),
        ("bn", "স্বাগতম"),
        ("el", "καλημέρα"),
        ("gu", "સ્વાગત છે"),
        ("hi", "नमस्ते"),
        ("ja", "こんにちは東京"),
        ("kn", "ಸ್ವಾಗತ"),
        ("ko", "환영합니다"),
        ("ml", "സ്വാഗതം"),
        ("mr", "स्वागत आहे"),
        ("or", "ସ୍ୱାଗତ"),
        ("pa", "ਜੀ ਆਇਆਂ ਨੂੰ"),
        ("ru", "добро пожаловать"),
        ("ta", "வரவேற்கிறோம்"),
        ("te", "స్వాగతం"),
        ("zh", "欢迎光临"),
    ),
)
def test_provider_native_scripts_are_never_rejected(language, sample):
    assessment = detect_unexpected_script(sample, expected_language=language)

    assert assessment.is_unexpected is False
    assert assessment.unexpected_scripts == ()


def test_every_exposed_inworld_language_has_a_script_policy_and_unknown_fails_open():
    assert set(INWORLD_TTS_SUPPORTED_LANGUAGES) <= set(LANGUAGE_SCRIPT_ALLOWLIST)

    unknown = detect_unexpected_script("абвгд", expected_language="new-provider-language")
    assert unknown.is_unexpected is False
    assert unknown.unexpected_scripts == ()


def test_entity_resolver_is_non_mutating_and_requires_unique_high_confidence():
    entries = [
        _entry(
            "Devu Vimal",
            entry_id="devu",
            entity_type="person",
            tier=1,
            priority=1_070,
        ),
        _entry(
            "Saeed Al Zaabi",
            entry_id="saeed",
            entity_type="person",
            tier=1,
            priority=1_060,
        ),
    ]
    # Add the deterministic phonetic signature produced by the artifact builder.
    built_like_entry = SpeechLexiconEntry(
        **{
            **entries[0].__dict__,
            "phonetic_keys": ("dvml",),
        }
    )

    result = resolve_canonical_entity(
        "Who is Dev Vimal?",
        [built_like_entry, entries[1]],
        expected_entity_types=("person",),
    )

    assert isinstance(result, EntityResolution)
    assert result.raw_text == "Who is Dev Vimal?"
    assert result.canonical == "Devu Vimal"
    assert result.entry_id == "devu"
    assert result.safe_to_apply is True
    assert "Devu Vimal" not in result.raw_text


@pytest.mark.asyncio
async def test_published_artifact_is_tenant_isolated_approval_gated_and_idempotent(db, tenant):
    knowledge_base = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved corporate knowledge",
        scope_type="group",
        scope_label="Al Zaabi Group",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Leadership directory",
        content="Al Zaabi Group employs Devu Vimal as its CFO.",
        status="indexed",
        structured_content={
            "compiler": {"version": "vav-knowledge-compiler-8"},
            "speech_entities": [
                {
                    "canonical": "Devu Vimal",
                    "entity_type": "person",
                    "language": "en",
                    "critical": True,
                    "aliases": [],
                    "evidence_sha256": "4" * 64,
                }
            ],
        },
    )
    knowledge_base.sources.append(source)
    agent = Agent(
        tenant_id=tenant.id,
        name="Receptionist",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
    )
    db.add_all([knowledge_base, agent])
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent.id,
        knowledge_base_id=knowledge_base.id,
        provider="inworld",
        sync_status="synced",
    )
    db.add(binding)
    await db.commit()

    with pytest.raises(SpeechLexiconError, match="Approve"):
        await publish_speech_lexicon(
            db,
            tenant_id=tenant.id,
            knowledge_base=knowledge_base,
        )
    artifact = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge_base,
        allow_draft_for_approval=True,
    )
    assert (
        await load_agent_speech_lexicon(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
        )
        is None
    )

    knowledge_base.approval_status = "approved"
    await db.flush()
    loaded = await load_agent_speech_lexicon(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
    )
    assert loaded is not None
    assert loaded.artifact_id == artifact.id
    assert loaded.entries[0].canonical == "Al Zaabi Group"
    artifact_again = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge_base,
    )
    assert artifact_again.id == artifact.id

    knowledge_base.speech_lexicon_artifact_id = None
    await db.flush()
    assert await backfill_approved_speech_lexicons(db, tenant_id=tenant.id) == 1
    assert knowledge_base.speech_lexicon_artifact_id == artifact.id

    other_tenant = Tenant(name="Other Tenant", slug=f"other-{uuid4().hex[:8]}")
    db.add(other_tenant)
    await db.flush()
    assert (
        await load_agent_speech_lexicon(
            db,
            tenant_id=other_tenant.id,
            agent_id=agent.id,
        )
        is None
    )
