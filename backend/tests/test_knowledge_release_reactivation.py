"""Audited compare-and-swap restoration of immutable knowledge releases."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeServingRevisionSource,
    KnowledgeSource,
)
from app.models.audit import AuditEvent
from app.services.knowledge_serving import publish_serving_revision
from app.services.speech_lexicon import publish_speech_lexicon


async def _two_release_knowledge(db, tenant):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="CAS release knowledge",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
        serving_revocation_generation=7,
        is_active=True,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Approved company directory",
        content="Example Medical Centre telephone is +971 2 111 1111.",
        content_sha256="1" * 64,
        status="indexed",
        compiled_at=datetime.now(UTC),
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.flush()

    first_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    first = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=first_lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = first.published_at

    source.content = "Example Medical Centre telephone is +971 2 222 2222."
    source.content_sha256 = "2" * 64
    source.compiled_at = datetime.now(UTC)
    knowledge.approval_status = "draft"
    second_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    second = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=second_lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = second.published_at
    await db.commit()
    return knowledge, first, second


@pytest.mark.asyncio
async def test_release_history_and_reactivation_move_both_pointers_with_audited_cas(
    client,
    auth_headers,
    db,
    tenant,
    user,
):
    knowledge, first, second = await _two_release_knowledge(db, tenant)

    history = await client.get(
        f"/api/v1/knowledge/{knowledge.id}/releases",
        headers=auth_headers,
    )
    assert history.status_code == 200
    assert {item["revision_id"] for item in history.json()} == {str(first.id), str(second.id)}

    restored = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{first.id}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": str(second.id),
            "reason": "Incident INC-204: restore the last verified directory.",
        },
    )
    assert restored.status_code == 200
    body = restored.json()
    assert body["serving_revision"]["revision_id"] == str(first.id)
    assert body["speech_lexicon"]["artifact_id"] == str(first.speech_lexicon_artifact_id)
    assert body["approval_status"] == "draft"
    assert body["has_pending_changes"] is True

    await db.refresh(knowledge)
    assert knowledge.serving_revision_id == first.id
    assert knowledge.speech_lexicon_artifact_id == first.speech_lexicon_artifact_id
    assert knowledge.serving_revocation_generation == 7

    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.tenant_id == tenant.id,
            AuditEvent.action == "knowledge_base.serving_revision_reactivated",
            AuditEvent.resource_id == str(knowledge.id),
        )
    )
    assert audit is not None
    assert audit.actor_user_id == user.id
    assert audit.details == {
        "expected_current_revision_id": str(second.id),
        "previous_serving_revision_id": str(second.id),
        "reactivated_serving_revision_id": str(first.id),
        "speech_lexicon_artifact_id": str(first.speech_lexicon_artifact_id),
        "reason": "Incident INC-204: restore the last verified directory.",
    }

    stale = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{second.id}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": str(second.id),
            "reason": "This operator view is stale.",
        },
    )
    assert stale.status_code == 409
    assert "live release changed" in stale.json()["detail"].lower()
    await db.refresh(knowledge)
    assert knowledge.serving_revision_id == first.id
    assert knowledge.serving_revocation_generation == 7

    revoked = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": False},
    )
    assert revoked.status_code == 200
    assert revoked.json()["serving_revision"] is None
    await db.refresh(knowledge)
    assert knowledge.serving_revocation_generation == 8

    restored_after_revocation = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{second.id}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": None,
            "reason": "Reactivate the verified replacement after explicit review.",
        },
    )
    assert restored_after_revocation.status_code == 200
    assert restored_after_revocation.json()["serving_revision"]["revision_id"] == str(second.id)
    await db.refresh(knowledge)
    assert knowledge.serving_revocation_generation == 8


@pytest.mark.asyncio
async def test_release_reactivation_rejects_unknown_target_and_requires_reason_and_cas(
    client,
    auth_headers,
    db,
    tenant,
):
    knowledge, _first, second = await _two_release_knowledge(db, tenant)

    unknown = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{uuid4()}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": str(second.id),
            "reason": "Restore a verified release.",
        },
    )
    assert unknown.status_code == 404

    missing_cas = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{second.id}/activate",
        headers=auth_headers,
        json={"reason": "Restore a verified release."},
    )
    blank_reason = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{second.id}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": str(second.id),
            "reason": "   ",
        },
    )
    assert missing_cas.status_code == 422
    assert blank_reason.status_code == 422


@pytest.mark.asyncio
async def test_release_reactivation_rejects_corruption_and_provider_native_bindings(
    client,
    auth_headers,
    db,
    tenant,
):
    knowledge, first, second = await _two_release_knowledge(db, tenant)
    first_source = await db.scalar(
        select(KnowledgeServingRevisionSource).where(
            KnowledgeServingRevisionSource.serving_revision_id == first.id
        )
    )
    assert first_source is not None
    first_source.content = "Tampered historical content."
    await db.commit()

    corrupted = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{first.id}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": str(second.id),
            "reason": "Attempt to restore a corrupted release.",
        },
    )
    assert corrupted.status_code == 409
    assert "integrity validation" in corrupted.json()["detail"]

    # Restore the immutable fixture so the provider-bound failure proves the
    # provider guard rather than merely encountering the integrity guard first.
    first_source.content = "Example Medical Centre telephone is +971 2 111 1111."
    agent = Agent(
        tenant_id=tenant.id,
        name="Provider-native receptionist",
        system_prompt="Use the provider-native collection.",
        voice_provider="smallest",
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="smallest",
        )
    )
    await db.commit()

    provider_bound = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/releases/{first.id}/activate",
        headers=auth_headers,
        json={
            "expected_current_revision_id": str(second.id),
            "reason": "This needs a separate provider rollback.",
        },
    )
    assert provider_bound.status_code == 409
    assert "provider-native Smallest.ai collection" in provider_bound.json()["detail"]
    await db.refresh(knowledge)
    assert knowledge.serving_revision_id == second.id
