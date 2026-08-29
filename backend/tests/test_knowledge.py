from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.endpoints import knowledge as knowledge_endpoint
from app.api.v1.endpoints.knowledge import (
    _ensure_bound_agents_accept_knowledge_change,
    _invalidate_bound_agent_deployments,
    _provider_source_status,
    _provider_url_key,
    _reconcile_provider_sources,
)
from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource


def _knowledge(source):
    return SimpleNamespace(
        sources=[source],
        source_count=1,
        indexed_source_count=0,
        sync_status="processing",
        sync_error=None,
    )


def _url_source(location="https://www.aesmc.com/"):
    return SimpleNamespace(
        source_type="url",
        name=location,
        location=location,
        status="processing",
        provider_item_id=None,
        last_synced_at=None,
        error_message=None,
    )


def test_provider_url_key_handles_provider_trailing_slash_normalization():
    assert _provider_url_key("HTTPS://WWW.AESMC.COM") == _provider_url_key("https://www.aesmc.com/")
    assert _provider_url_key("https://www.aesmc.com/services/") == _provider_url_key(
        "https://www.aesmc.com/services"
    )


def test_provider_completed_status_aliases_are_indexed():
    for value in ("complete", "completed", "indexed", "processed", "ready", "success"):
        assert _provider_source_status({"processingStatus": value}) == "indexed"


def test_provider_processing_status_takes_precedence_over_response_status():
    assert _provider_source_status({"status": True, "processingStatus": "completed"}) == "indexed"


def test_reconcile_matches_normalized_provider_url():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[
            {
                "_id": "provider-source-1",
                "url": "https://www.aesmc.com",
                "status": "indexed",
            }
        ],
        items=[],
        provider_knowledge_base={"processingStatus": "completed"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.provider_item_id == "provider-source-1"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1


def test_reconcile_matches_completed_url_returned_as_knowledge_item():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[
            {
                "_id": "provider-item-1",
                "processingStatus": "completed",
                "metadata": {"sourceUrl": "https://www.aesmc.com"},
            }
        ],
        provider_knowledge_base={"processingStatus": "processing"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.provider_item_id == "provider-item-1"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1


def test_reconcile_matches_smallest_completed_scrape_batch():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[
            {
                "_id": "provider-scrape-batch-1",
                "knowledgeBaseId": "provider-kb-1",
                "hostUrl": "https://www.aesmc.com/",
                "scrapedUrls": ["https://www.aesmc.com/"],
                "processingStatus": "completed",
                "totalUrls": 1,
            }
        ],
        items=[],
        provider_knowledge_base={"_id": "provider-kb-1"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.provider_item_id == "provider-scrape-batch-1"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1


def test_reconcile_uses_completed_knowledge_base_as_safe_fallback():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[],
        provider_knowledge_base={"processingStatus": "completed"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1


def test_new_provider_source_invalidates_bound_agent_deployment():
    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Adam and Eve Concierge",
        provider_agent_id="provider-agent-1",
        sync_status="synced",
    )
    binding = SimpleNamespace(
        agent=agent,
        sync_status="synced",
        last_synced_at=datetime.now(UTC),
    )
    knowledge = SimpleNamespace(agent_bindings=[binding])

    affected = _invalidate_bound_agent_deployments(knowledge)

    assert affected == [agent_id]
    assert agent.sync_status == "dirty"
    assert binding.sync_status == "pending"
    assert binding.last_synced_at is None


def test_new_provider_source_keeps_unprovisioned_bound_agent_local():
    agent = SimpleNamespace(
        id=uuid4(),
        name="Local concierge",
        provider_agent_id=None,
        sync_status="local_only",
    )
    binding = SimpleNamespace(
        agent=agent,
        sync_status="synced",
        last_synced_at=datetime.now(UTC),
    )

    affected = _invalidate_bound_agent_deployments(
        SimpleNamespace(agent_bindings=[binding])
    )

    assert affected == []
    assert agent.sync_status == "local_only"
    assert binding.sync_status == "pending"
    assert binding.last_synced_at is None


def test_source_change_rejects_race_with_agent_publish():
    knowledge = SimpleNamespace(
        agent_bindings=[
            SimpleNamespace(
                agent=SimpleNamespace(
                    name="Publishing concierge",
                    sync_status="provider_scanning",
                )
            )
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        _ensure_bound_agents_accept_knowledge_change(knowledge)

    assert exc_info.value.status_code == 409
    assert "Publishing concierge" in exc_info.value.detail


@pytest.mark.asyncio
async def test_pdf_upload_marks_existing_provider_binding_for_publish(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    class KnowledgeClient:
        async def upload_knowledge_pdf(self, **kwargs):
            assert kwargs["knowledge_base_id"] == "provider-kb-1"
            assert kwargs["file_name"] == "botox.pdf"

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", KnowledgeClient)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Adam and Eve Concierge", "system_prompt": "Use approved knowledge."},
    )
    agent = await db.get(Agent, UUID(created.json()["id"]))
    agent.provider_agent_id = "provider-agent-1"
    agent.provider_branch_id = "provider-branch-1"
    agent.provider_revision_id = "provider-revision-5"
    agent.sync_status = "synced"
    agent.last_synced_at = datetime.now(UTC)
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Adam and Eve Medical Knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-1",
        sync_status="ready",
        approval_status="approved",
    )
    db.add(knowledge)
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent.id,
        knowledge_base_id=knowledge.id,
        provider="smallest",
        sync_status="synced",
        last_synced_at=datetime.now(UTC),
    )
    db.add(binding)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("botox.pdf", b"%PDF-1.4\nknowledge", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["agent_bindings"][0]["sync_status"] == "pending"
    persisted_agent = await db.scalar(
        select(Agent)
        .where(Agent.id == agent.id)
        .execution_options(populate_existing=True)
    )
    persisted_binding = await db.scalar(
        select(AgentKnowledgeBinding)
        .where(AgentKnowledgeBinding.id == binding.id)
        .execution_options(populate_existing=True)
    )
    assert persisted_agent.sync_status == "dirty"
    assert persisted_binding.sync_status == "pending"
    assert persisted_binding.last_synced_at is None


@pytest.mark.asyncio
async def test_delete_grouped_url_source_removes_provider_and_local_group(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    deleted: list[tuple[str, str]] = []

    class KnowledgeClient:
        async def list_scraped_knowledge_urls(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-1"
            return [
                {
                    "_id": "provider-scrape-batch-1",
                    "hostUrl": "https://aecmc.com/",
                    "scrapedUrls": [
                        "https://aecmc.com/doctors",
                        "https://aecmc.com/treatments/botox/en",
                    ],
                    "processingStatus": "completed",
                }
            ]

        async def list_knowledge_items(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-1"
            return [
                {
                    "_id": "provider-pdf-1",
                    "fileName": "doctors.pdf",
                    "processingStatus": "completed",
                }
            ]

        async def delete_scraped_knowledge_url(self, **kwargs):
            deleted.append((kwargs["knowledge_base_id"], kwargs["scraped_url_id"]))

        async def delete_knowledge_item(self, **kwargs):
            raise AssertionError("The PDF must be retained")

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", KnowledgeClient)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Adam and Eve Concierge", "system_prompt": "Use approved knowledge."},
    )
    agent = await db.get(Agent, UUID(created.json()["id"]))
    agent.provider_agent_id = "provider-agent-1"
    agent.sync_status = "synced"
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Adam and Eve Medical Knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-1",
        sync_status="ready",
        approval_status="approved",
    )
    db.add(knowledge)
    await db.flush()
    url_sources = [
        KnowledgeSource(
            tenant_id=tenant.id,
            knowledge_base_id=knowledge.id,
            source_type="url",
            name=url,
            location=url,
            status="indexed",
            provider_item_id="provider-scrape-batch-1",
        )
        for url in (
            "https://aecmc.com/doctors",
            "https://aecmc.com/treatments/botox/en",
        )
    ]
    pdf_source = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge.id,
        source_type="file",
        name="doctors.pdf",
        status="indexed",
        provider_item_id="provider-pdf-1",
    )
    db.add_all([*url_sources, pdf_source])
    knowledge.source_count = 3
    knowledge.indexed_source_count = 3
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="smallest",
            sync_status="synced",
            last_synced_at=datetime.now(UTC),
        )
    )
    await db.commit()

    response = await client.delete(
        f"/api/v1/knowledge/{knowledge.id}/sources/{url_sources[0].id}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert deleted == [("provider-kb-1", "provider-scrape-batch-1")]
    assert [source["name"] for source in response.json()["sources"]] == ["doctors.pdf"]
    assert response.json()["source_count"] == 1
    assert response.json()["indexed_source_count"] == 1
    assert response.json()["agent_bindings"][0]["sync_status"] == "pending"
    remaining = (
        await db.scalars(
            select(KnowledgeSource).where(KnowledgeSource.knowledge_base_id == knowledge.id)
        )
    ).all()
    assert [source.name for source in remaining] == ["doctors.pdf"]
