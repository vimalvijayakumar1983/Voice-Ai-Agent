"""Shared builders for tests that require an immutable knowledge release."""

from uuid import uuid4

from app.models.agent import AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource
from app.services.knowledge_serving import publish_serving_revision
from app.services.speech_lexicon import publish_speech_lexicon


async def publish_test_knowledge(
    db,
    *,
    tenant_id,
    agent,
    label: str = "Clinic",
    content: str | None = None,
):
    suffix = uuid4().hex[:8]
    knowledge = KnowledgeBase(
        tenant_id=tenant_id,
        name=f"{label} knowledge {suffix}",
        scope_label=label,
        provider=agent.voice_provider,
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
        is_active=True,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant_id,
            source_type="text",
            name=f"{label} directory",
            content=(
                content
                if content is not None
                else f"{label} Medical Centre telephone is +971 2 555 0100."
            ),
            status="indexed",
        )
    )
    db.add(knowledge)
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant_id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider=agent.voice_provider,
            sync_status="synced",
        )
    )
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant_id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant_id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = revision.published_at
    await db.flush()
    return knowledge, revision
