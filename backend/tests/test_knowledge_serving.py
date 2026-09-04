from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource
from app.models.call import Call
from app.models.tenant import Tenant
from app.services.exact_fact_retrieval import ExactFactResponseAction, retrieve_exact_fact
from app.services.knowledge_retrieval import (
    load_agent_knowledge_terminology,
    retrieve_knowledge_context,
)
from app.services.knowledge_serving import (
    INBOUND_KNOWLEDGE_ADMISSION_STATE,
    KnowledgeServingError,
    backfill_approved_serving_revisions,
    backfill_approved_serving_revisions_batch,
    knowledge_admission_is_durable,
    knowledge_call_reservation_metadata,
    load_agent_serving_revision,
    parse_serving_revision_id,
    pre_admit_outbound_knowledge_call,
    publish_serving_revision,
    serving_knowledge_base_id_from_call_metadata,
    serving_revision_id_from_call_metadata,
    serving_revocation_generation_from_call_metadata,
    speech_lexicon_artifact_id_from_call_metadata,
    speech_lexicon_content_sha256_from_call_metadata,
)
from app.services.knowledge_sources import invalidate_knowledge_approval
from app.services.speech_lexicon import load_agent_speech_lexicon, publish_speech_lexicon
from tests.conftest import engine as test_engine
from tests.conftest import test_session_factory as session_factory


@pytest.mark.asyncio
async def test_serving_backfill_quarantines_invalid_row_and_continues(db, tenant):
    invalid = KnowledgeBase(
        tenant_id=tenant.id,
        name="Broken serving legacy knowledge",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    valid = KnowledgeBase(
        tenant_id=tenant.id,
        name="Valid serving legacy knowledge",
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
            name="Valid serving source",
            content="Example Medical Centre provides approved visitor information.",
            status="indexed",
            structured_content=_structured("+971 2 333 3333", "PRP treatment"),
        )
    )
    db.add_all((invalid, valid))
    await db.flush()

    result = await backfill_approved_serving_revisions_batch(
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
    assert valid.serving_revision_id is not None
    rerun = await backfill_approved_serving_revisions_batch(db, tenant_id=tenant.id, limit=2)
    assert rerun.selected == 0


def _structured(phone: str, service: str) -> dict:
    return {
        "schema_version": "compiler-test-v1",
        "exact_fact_coverage": {
            "complete": True,
            "absence_authoritative": True,
        },
        "facts": [
            {
                "subject": "Example Medical Centre",
                "predicate": "primary telephone",
                "value": phone,
                "evidence": f"Example Medical Centre telephone: {phone}.",
                "search_phrases": ["What is the phone number?"],
            },
            {
                "subject": "Example Medical Centre",
                "predicate": "service",
                "value": service,
                "evidence": f"Example Medical Centre offers {service}.",
                "search_phrases": [f"Do you offer {service}?"],
            },
        ],
        "speech_entities": [
            {
                "canonical": "Example Medical Centre",
                "entity_type": "organization",
                "language": "en",
                "critical": True,
                "aliases": [],
                "evidence_sha256": "a" * 64,
            },
            {
                "canonical": service,
                "entity_type": "service",
                "language": "en",
                "critical": False,
                "aliases": [],
                "evidence_sha256": "b" * 64,
            },
        ],
    }


@pytest.mark.asyncio
async def test_publishing_the_current_serving_revision_is_idempotent(db, tenant):
    """A current-revision identity-map hit must retain its immutable sources."""

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Idempotent publication knowledge",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Published directory",
            content="Example Medical Centre telephone is +971 2 111 1111.",
            status="indexed",
            structured_content=_structured("+971 2 111 1111", "customer support"),
        )
    )
    db.add(knowledge)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    first = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    await db.commit()
    knowledge_id = knowledge.id
    revision_id = first.id

    async with session_factory() as retry_db:
        loaded_knowledge = await retry_db.get(KnowledgeBase, knowledge_id)
        assert loaded_knowledge is not None
        assert loaded_knowledge.serving_revision is not None
        assert loaded_knowledge.serving_revision.sources == []
        loaded_lexicon = loaded_knowledge.speech_lexicon
        assert loaded_lexicon is not None

        second = await publish_serving_revision(
            retry_db,
            tenant_id=tenant.id,
            knowledge_base=loaded_knowledge,
            speech_lexicon=loaded_lexicon,
            allow_draft_for_approval=True,
        )

        assert second.id == revision_id
        assert len(second.sources) == 1
        assert second.sources[0].content.strip()


@pytest.mark.asyncio
async def test_delete_waits_for_admitted_call_to_become_terminal(
    client,
    auth_headers,
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Deletion-fenced receptionist",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Reserved immutable knowledge",
        provider="inworld",
        scope_label="Reserved immutable knowledge",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Approved directory",
            content="Reserved immutable knowledge telephone: +971 2 111 1111.",
            status="indexed",
            structured_content=_structured("+971 2 111 1111", "customer support"),
        )
    )
    replacement = KnowledgeBase(
        tenant_id=tenant.id,
        name="Replacement knowledge",
        provider="inworld",
        sync_status="ready",
        approval_status="approved",
        source_count=0,
        indexed_source_count=0,
        is_active=True,
    )
    db.add_all((agent, knowledge, replacement))
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent.id,
        knowledge_base_id=knowledge.id,
        provider="inworld",
        sync_status="synced",
    )
    db.add(binding)
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = revision.published_at
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number="+97141234567",
        to_number="+971501234567",
        provider="livekit_sip",
        call_metadata={
            "runtime": {
                "transport": "livekit_sip",
                "speech_provider": "inworld",
                **knowledge_call_reservation_metadata(revision, 0),
            }
        },
    )
    db.add(call)
    await db.flush()
    await pre_admit_outbound_knowledge_call(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        call_id=call.id,
    )
    await db.commit()
    knowledge_id = knowledge.id
    call_id = call.id

    # Rebinding removes the ordinary delete guard, but the admitted call still
    # owns the immutable A release until it reaches a terminal state.
    binding.knowledge_base_id = replacement.id
    await db.commit()
    blocked = await client.delete(
        f"/api/v1/knowledge/{knowledge_id}",
        headers=auth_headers,
    )
    assert blocked.status_code == 409
    assert "reserved by an active call" in blocked.json()["detail"]
    assert await db.get(KnowledgeBase, knowledge_id) is not None

    admitted_call = await db.get(Call, call_id)
    admitted_call.status = "completed"
    admitted_call.ended_at = datetime.now(UTC)
    await db.commit()
    deleted = await client.delete(
        f"/api/v1/knowledge/{knowledge_id}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204
    db.expire_all()
    assert await db.get(KnowledgeBase, knowledge_id) is None


@pytest.mark.asyncio
async def test_bound_knowledge_approval_eagerly_serializes_agent_and_release(
    client,
    auth_headers,
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Bound approval receptionist",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Bound approval knowledge",
        scope_label="Bound approval knowledge",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Approved directory",
            content="Bound approval knowledge telephone: +971 2 111 1111.",
            status="indexed",
            structured_content=_structured("+971 2 111 1111", "customer support"),
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent.id,
        knowledge_base_id=knowledge.id,
        provider="inworld",
        sync_status="synced",
    )
    db.add(binding)
    await db.commit()

    approved = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["approval_status"] == "approved"
    assert body["serving_revision"]["revision_id"]
    assert body["speech_lexicon"]["artifact_id"]
    assert len(body["agent_bindings"]) == 1
    assert body["agent_bindings"][0]["id"] == str(binding.id)
    assert body["agent_bindings"][0]["agent_id"] == str(agent.id)
    assert body["agent_bindings"][0]["agent_name"] == agent.name


@pytest.mark.asyncio
async def test_knowledge_api_reports_live_release_while_new_source_is_pending(
    client,
    auth_headers,
    db,
    tenant,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="API blue green knowledge",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Version one",
            content="Example Medical Centre offers approved Botox consultations.",
            status="indexed",
            structured_content=_structured("+971 2 111 1111", "Botox consultation"),
        )
    )
    db.add(knowledge)
    await db.commit()

    approved = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert approved.status_code == 200
    live = approved.json()["serving_revision"]
    assert live["revision_id"]
    assert live["source_count"] == 1
    assert approved.json()["has_pending_changes"] is False

    staged = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/text",
        headers=auth_headers,
        json={
            "name": "Version two pending",
            "content": "This newly added laser policy must not be live before approval.",
        },
    )
    assert staged.status_code == 200
    assert staged.json()["approval_status"] == "draft"
    assert staged.json()["has_pending_changes"] is True
    assert staged.json()["serving_revision"]["revision_id"] == live["revision_id"]
    assert staged.json()["serving_revision"]["source_count"] == 1
    assert staged.json()["source_count"] == 2


@pytest.mark.asyncio
async def test_pending_draft_keeps_last_approved_revision_live_and_swap_is_atomic(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Blue green receptionist",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Example Medical Centre",
        scope_label="Example Medical Centre",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Approved directory",
        content=("Example Medical Centre offers Botox treatment. Telephone +971 2 111 1111."),
        status="indexed",
        content_sha256="1" * 64,
        compiled_at=datetime.now(UTC),
        structured_content=_structured("+971 2 111 1111", "Botox treatment"),
    )
    knowledge.sources.append(source)
    db.add_all((agent, knowledge))
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent.id,
        knowledge_base_id=knowledge.id,
        provider="inworld",
        sync_status="synced",
    )
    db.add(binding)
    first_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    first_revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=first_lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = first_revision.published_at
    await db.commit()

    source.content = "Example Medical Centre offers laser treatment. Telephone +971 2 222 2222."
    source.content_sha256 = "2" * 64
    source.structured_content = _structured("+971 2 222 2222", "laser treatment")
    assert invalidate_knowledge_approval(knowledge) is True
    await db.commit()

    assert knowledge.approval_status == "draft"
    assert knowledge.serving_revision_id == first_revision.id
    assert knowledge.speech_lexicon_artifact_id == first_lexicon.id
    old_context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Do you offer Botox treatment?",
    )
    assert old_context is not None
    assert "Botox treatment" in old_context
    assert "+971 2 222 2222" not in old_context
    old_phone = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        cache=None,
    )
    assert old_phone.response_action == ExactFactResponseAction.ANSWER
    assert old_phone.evidence[0].value == "+971 2 111 1111"
    loaded_lexicon = await load_agent_speech_lexicon(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
    )
    assert loaded_lexicon is not None
    assert loaded_lexicon.artifact_id == first_lexicon.id

    source.status = "failed"
    with pytest.raises(KnowledgeServingError, match="searchable"):
        await publish_serving_revision(
            db,
            tenant_id=tenant.id,
            knowledge_base=knowledge,
            speech_lexicon=first_lexicon,
            allow_draft_for_approval=True,
        )
    assert knowledge.serving_revision_id == first_revision.id
    source.status = "indexed"

    with pytest.raises(KnowledgeServingError, match="does not match"):
        await publish_serving_revision(
            db,
            tenant_id=tenant.id,
            knowledge_base=knowledge,
            speech_lexicon=first_lexicon,
            allow_draft_for_approval=True,
        )

    second_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    second_revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=second_lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = second_revision.published_at
    await db.commit()

    assert second_revision.id != first_revision.id
    new_phone = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        cache=None,
    )
    assert new_phone.response_action == ExactFactResponseAction.ANSWER
    assert new_phone.evidence[0].value == "+971 2 222 2222"

    # A call reserved before approval keeps the first immutable release for
    # every knowledge path, even after the green pointer swaps to version two.
    pinned_revision = await load_agent_serving_revision(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        serving_revision_id=first_revision.id,
    )
    assert pinned_revision is not None
    assert pinned_revision.id == first_revision.id
    pinned_context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Do you offer Botox treatment?",
        serving_revision_id=first_revision.id,
    )
    assert pinned_context is not None
    assert "Botox treatment" in pinned_context
    assert "laser treatment" not in pinned_context
    pinned_phone = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        serving_revision_id=first_revision.id,
        cache=None,
    )
    assert pinned_phone.response_action == ExactFactResponseAction.ANSWER
    assert pinned_phone.evidence[0].value == "+971 2 111 1111"
    pinned_lexicon = await load_agent_speech_lexicon(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        serving_revision_id=first_revision.id,
    )
    assert pinned_lexicon is not None
    assert pinned_lexicon.artifact_id == first_lexicon.id
    pinned_terminology = await load_agent_knowledge_terminology(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        serving_revision_id=first_revision.id,
    )
    assert "Botox treatment" in pinned_terminology
    assert "laser treatment" not in pinned_terminology

    # Once call admission has authenticated both immutable IDs, every
    # per-turn path must keep serving that release even if this agent is later
    # rebound. The optional KB pin is deliberately required for this bypass;
    # a revision ID by itself continues to be checked against the live binding.
    replacement = KnowledgeBase(
        tenant_id=tenant.id,
        name="Replacement Medical Centre",
        scope_label="Replacement Medical Centre",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    replacement.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Replacement directory",
            content="Replacement Medical Centre offers physiotherapy. Telephone +971 2 999 9999.",
            status="indexed",
            content_sha256="3" * 64,
            compiled_at=datetime.now(UTC),
            structured_content=_structured("+971 2 999 9999", "physiotherapy"),
        )
    )
    db.add(replacement)
    await db.flush()
    replacement_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=replacement,
    )
    await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=replacement,
        speech_lexicon=replacement_lexicon,
    )
    binding.knowledge_base_id = replacement.id
    await db.commit()

    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            query="Do you offer Botox treatment?",
            serving_revision_id=first_revision.id,
            knowledge_base_id=knowledge.id,
        )
        is not None
    )
    rebound_phone = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        serving_revision_id=first_revision.id,
        knowledge_base_id=knowledge.id,
        cache=None,
    )
    assert rebound_phone.response_action == ExactFactResponseAction.ANSWER
    assert rebound_phone.evidence[0].value == "+971 2 111 1111"
    rebound_lexicon = await load_agent_speech_lexicon(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        serving_revision_id=first_revision.id,
        knowledge_base_id=knowledge.id,
    )
    assert rebound_lexicon is not None
    assert rebound_lexicon.artifact_id == first_lexicon.id
    rebound_terminology = await load_agent_knowledge_terminology(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        serving_revision_id=first_revision.id,
        knowledge_base_id=knowledge.id,
    )
    assert "Botox treatment" in rebound_terminology
    assert "physiotherapy" not in rebound_terminology
    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            query="Do you offer Botox treatment?",
            serving_revision_id=first_revision.id,
        )
        is None
    )
    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            query="Do you offer Botox treatment?",
            serving_revision_id=first_revision.id,
            knowledge_base_id=replacement.id,
        )
        is None
    )

    unknown_revision_id = uuid4()
    assert (
        await load_agent_serving_revision(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            serving_revision_id=unknown_revision_id,
        )
        is None
    )
    assert (
        await retrieve_knowledge_context(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            query="What is the phone number?",
            serving_revision_id=unknown_revision_id,
        )
        is None
    )
    assert (
        await load_agent_speech_lexicon(
            db,
            tenant_id=tenant.id,
            agent_id=agent.id,
            serving_revision_id=unknown_revision_id,
        )
        is None
    )


def test_call_metadata_revision_pin_is_strictly_parsed():
    revision_id = uuid4()
    knowledge_base_id = uuid4()
    speech_lexicon_artifact_id = uuid4()
    speech_lexicon_content_sha256 = "a" * 64
    assert parse_serving_revision_id(revision_id) == revision_id
    assert (
        serving_revision_id_from_call_metadata(
            {"runtime": {"knowledge_serving_revision_id": str(revision_id)}}
        )
        == revision_id
    )
    assert serving_revision_id_from_call_metadata({"runtime": {}}) is None
    assert (
        serving_knowledge_base_id_from_call_metadata(
            {"runtime": {"knowledge_serving_knowledge_base_id": str(knowledge_base_id)}}
        )
        == knowledge_base_id
    )
    assert serving_knowledge_base_id_from_call_metadata({"runtime": {}}) is None
    with pytest.raises(KnowledgeServingError, match="invalid"):
        serving_knowledge_base_id_from_call_metadata(
            {"runtime": {"knowledge_serving_knowledge_base_id": "not-a-uuid"}}
        )
    with pytest.raises(KnowledgeServingError, match="invalid"):
        serving_revision_id_from_call_metadata(
            {"runtime": {"knowledge_serving_revision_id": "not-a-uuid"}}
        )
    assert (
        serving_revocation_generation_from_call_metadata(
            {"runtime": {"knowledge_serving_revocation_generation": 7}}
        )
        == 7
    )
    assert serving_revocation_generation_from_call_metadata({"runtime": {}}) is None
    for invalid_generation in (True, -1, "7", 1.5):
        with pytest.raises(KnowledgeServingError, match="non-negative integer"):
            serving_revocation_generation_from_call_metadata(
                {"runtime": {"knowledge_serving_revocation_generation": invalid_generation}}
            )
    assert (
        speech_lexicon_artifact_id_from_call_metadata(
            {"runtime": {"speech_lexicon_artifact_id": str(speech_lexicon_artifact_id)}}
        )
        == speech_lexicon_artifact_id
    )
    assert speech_lexicon_artifact_id_from_call_metadata({"runtime": {}}) is None
    with pytest.raises(KnowledgeServingError, match="invalid"):
        speech_lexicon_artifact_id_from_call_metadata(
            {"runtime": {"speech_lexicon_artifact_id": "not-a-uuid"}}
        )
    assert (
        speech_lexicon_content_sha256_from_call_metadata(
            {"runtime": {"speech_lexicon_content_sha256": speech_lexicon_content_sha256}}
        )
        == speech_lexicon_content_sha256
    )
    assert speech_lexicon_content_sha256_from_call_metadata({"runtime": {}}) is None
    with pytest.raises(KnowledgeServingError, match="invalid"):
        speech_lexicon_content_sha256_from_call_metadata(
            {"runtime": {"speech_lexicon_content_sha256": "not-a-sha256"}}
        )

    admitted_metadata = {
        "runtime": {
            "knowledge_serving_revision_id": str(revision_id),
            "knowledge_serving_knowledge_base_id": str(knowledge_base_id),
            "knowledge_serving_revocation_generation": 7,
            "speech_lexicon_artifact_id": str(speech_lexicon_artifact_id),
            "speech_lexicon_content_sha256": speech_lexicon_content_sha256,
            "knowledge_admission_state": "admitted_before_dispatch",
            "knowledge_admitted_at": "2026-09-04T12:00:00+00:00",
        }
    }
    assert knowledge_admission_is_durable(admitted_metadata)
    legacy_inworld_metadata = {
        "runtime": {
            key: value
            for key, value in admitted_metadata["runtime"].items()
            if key
            not in {
                "speech_lexicon_artifact_id",
                "speech_lexicon_content_sha256",
            }
        }
    }
    assert knowledge_admission_is_durable(legacy_inworld_metadata)
    inbound_admitted_metadata = {
        "runtime": {
            **admitted_metadata["runtime"],
            "knowledge_admission_state": INBOUND_KNOWLEDGE_ADMISSION_STATE,
        }
    }
    assert knowledge_admission_is_durable(inbound_admitted_metadata)
    assert not knowledge_admission_is_durable({"runtime": {}})
    for invalid_marker in (
        {"knowledge_admission_state": "admitted_before_dispatch"},
        {
            "knowledge_admission_state": "admitted_before_dispatch",
            "knowledge_admitted_at": "not-a-timestamp",
        },
        {
            "knowledge_admission_state": "admitted_before_dispatch",
            "knowledge_admitted_at": "2026-09-04T12:00:00",
        },
        {
            "knowledge_admission_state": "admitted_before_dispatch",
            "knowledge_admitted_at": "2026-09-04T12:00:00+00:00",
        },
    ):
        with pytest.raises(KnowledgeServingError, match="admission"):
            knowledge_admission_is_durable({"runtime": invalid_marker})


@pytest.mark.asyncio
async def test_serving_revision_is_tenant_isolated_and_backfill_is_idempotent(db, tenant):
    agent = Agent(tenant_id=tenant.id, name="Agent", system_prompt="Use evidence")
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Legacy approved knowledge",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Legacy source",
            content="Legacy approved knowledge contains Example Medical Centre information.",
            status="indexed",
            content_sha256="3" * 64,
            structured_content=_structured("+971 2 333 3333", "PRP treatment"),
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
        )
    )
    await db.commit()

    assert await backfill_approved_serving_revisions(db, tenant_id=tenant.id) == 1
    await db.commit()
    assert await backfill_approved_serving_revisions(db, tenant_id=tenant.id) == 0
    loaded = await load_agent_serving_revision(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
    )
    assert loaded is not None
    assert loaded.source_count == 1

    other_tenant = Tenant(name="Other", slug=f"other-{uuid4().hex[:8]}")
    db.add(other_tenant)
    await db.flush()
    assert (
        await load_agent_serving_revision(
            db,
            tenant_id=other_tenant.id,
            agent_id=agent.id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_serving_revision_ranks_before_candidate_limit(db, tenant):
    agent = Agent(tenant_id=tenant.id, name="Large-site agent", system_prompt="Use evidence")
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Large site",
        approval_status="approved",
        sync_status="ready",
        source_count=61,
        indexed_source_count=61,
        is_active=True,
    )
    for index in range(60):
        knowledge.sources.append(
            KnowledgeSource(
                id=UUID(f"a0000000-0000-0000-0000-{index + 1:012x}"),
                tenant_id=tenant.id,
                source_type="website",
                name=f"Clinic page {index:02d}",
                status="indexed",
                content="The clinic provides general approved information for visitors.",
            )
        )
    knowledge.sources.append(
        KnowledgeSource(
            id=UUID(int=(1 << 128) - 1),
            tenant_id=tenant.id,
            source_type="website",
            name="Specialist clinic page",
            status="indexed",
            content="The clinic offers cardiothoracic service consultations on Tuesdays.",
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
        )
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
    )
    await db.commit()

    context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Which clinic offers cardiothoracic consultations?",
        serving_revision_id=revision.id,
    )

    assert context is not None
    assert "cardiothoracic service" in context


@pytest.mark.asyncio
async def test_serving_revision_unions_contact_fallback_with_postgres_fts(db, tenant):
    """A ranked FTS hit must not hide a differently worded contact page."""

    agent = Agent(
        tenant_id=tenant.id,
        name="Contact fallback agent",
        system_prompt="Use approved evidence only.",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Contact fallback knowledge",
        approval_status="approved",
        sync_status="ready",
        source_count=2,
        indexed_source_count=2,
        is_active=True,
    )
    knowledge.sources.extend(
        (
            KnowledgeSource(
                tenant_id=tenant.id,
                source_type="website",
                name="Contact",
                status="indexed",
                content="Call us at +971 2 665 9998.",
            ),
            KnowledgeSource(
                tenant_id=tenant.id,
                source_type="website",
                name="Telephone enquiry policy",
                status="indexed",
                content=(
                    "Phone number questions are handled according to the approved enquiry policy."
                ),
            ),
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
        )
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
    )
    await db.commit()

    context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        serving_revision_id=revision.id,
    )

    assert context is not None
    assert "+971 2 665 9998" in context


@pytest.mark.asyncio
async def test_postgres_contact_lane_keeps_reach_us_ahead_of_footer_noise(db, tenant):
    """A real contact page survives the 12-row lane limit on footer-heavy sites."""

    if test_engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL candidate ordering")

    contact_source_id = UUID(int=1)
    noise_count = 13
    agent = Agent(
        tenant_id=tenant.id,
        name="Footer-heavy contact agent",
        system_prompt="Use approved evidence only.",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Footer-heavy website",
        approval_status="approved",
        sync_status="ready",
        source_count=noise_count + 1,
        indexed_source_count=noise_count + 1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            id=contact_source_id,
            tenant_id=tenant.id,
            source_type="website",
            name="Reach Us",
            location="https://company.example/reach-us",
            status="indexed",
            content="Call us at +971 2 665 9998 for the main reception desk.",
        )
    )
    for index in range(noise_count):
        knowledge.sources.append(
            KnowledgeSource(
                id=UUID(int=index + 2),
                tenant_id=tenant.id,
                source_type="website",
                name=f"Company news {index:02d}",
                location=f"https://company.example/news/{index}",
                status="indexed",
                content=(
                    f"Approved company news item {index}. "
                    f"Footer email: newsletter-{index}@company.example."
                ),
            )
        )
    db.add_all((agent, knowledge))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
        )
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
    )

    # Make the strong source older than every weak footer match. Without the
    # evidence-priority CASE, the newest 12 rows crowd it out deterministically.
    baseline = datetime(2024, 1, 1, tzinfo=UTC)
    for source in revision.sources:
        source.created_at = (
            baseline
            if source.original_source_id == contact_source_id
            else baseline + timedelta(days=1, seconds=source.original_source_id.int)
        )
    await db.commit()

    context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        serving_revision_id=revision.id,
    )

    assert context is not None
    assert "+971 2 665 9998" in context


@pytest.mark.asyncio
async def test_partial_ai_extraction_keeps_omitted_approved_branch_retrievable(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Partial compiler receptionist",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Two branch directory",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    source_text = (
        "Branch A primary telephone is +971 2 111 1111.\n\n"
        "Branch B primary telephone is +971 2 222 2222."
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="website",
            name="Approved branch directory",
            status="indexed",
            content=(
                "VERIFIED STRUCTURED FACTS\nSUBJECT: Branch A\n"
                "- primary telephone: +971 2 111 1111\n\nSOURCE CONTENT\n" + source_text
            ),
            structured_content={
                "schema_version": "knowledge-compiler-v8",
                "facts": [
                    {
                        "subject": "Branch A",
                        "predicate": "primary telephone",
                        "value": "+971 2 111 1111",
                        "evidence": "Branch A primary telephone is +971 2 111 1111.",
                        "search_phrases": ["Branch A phone number"],
                    }
                ],
                "exact_fact_coverage": {
                    "complete": False,
                    "absence_authoritative": False,
                    "returned_facts_validated": True,
                },
            },
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
        )
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
    )
    await db.commit()

    branch_b = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number for Branch B?",
        serving_revision_id=revision.id,
    )
    generic = await retrieve_exact_fact(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="What is the phone number?",
        serving_revision_id=revision.id,
    )

    assert branch_b is not None
    assert "+971 2 222 2222" in branch_b
    assert generic.response_action == ExactFactResponseAction.FALLBACK
    assert generic.reason == "generic_query_requires_complete_exact_fact_coverage"


@pytest.mark.asyncio
async def test_legacy_ai_extraction_keeps_omitted_raw_fact_in_mutable_and_release_paths(db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Legacy compiler receptionist",
        system_prompt="Use approved evidence only.",
        voice_provider="inworld",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Legacy directory",
        approval_status="approved",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="website",
            name="Legacy approved directory",
            status="indexed",
            content=(
                "VERIFIED STRUCTURED FACTS\nSUBJECT: Branch A\n"
                "- primary telephone: +971 2 111 1111\n\nSOURCE CONTENT\n"
                "Branch A primary telephone is +971 2 111 1111.\n\n"
                "The chairman of Al Zaabi Group is Saeed Al Zaabi."
            ),
            structured_content={
                "schema_version": "knowledge-compiler-v7",
                "facts": [
                    {
                        "subject": "Branch A",
                        "predicate": "primary telephone",
                        "value": "+971 2 111 1111",
                        "evidence": "Branch A primary telephone is +971 2 111 1111.",
                        "search_phrases": ["Branch A phone number"],
                    }
                ],
                "validation": {
                    "all_evidence_source_grounded": True,
                    "facts_rejected": 0,
                },
            },
        )
    )
    db.add_all((agent, knowledge))
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
        )
    )
    await db.flush()

    mutable = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Who is the chairman?",
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
    )
    await db.commit()
    released = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Who is the chairman?",
        serving_revision_id=revision.id,
    )

    assert mutable is not None and "Saeed Al Zaabi" in mutable
    assert released is not None and "Saeed Al Zaabi" in released
