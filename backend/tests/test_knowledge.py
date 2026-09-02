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
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeCrawlPage,
    KnowledgeSource,
)
from app.providers.smallest import SmallestAIError
from app.services.knowledge_retrieval import retrieve_knowledge_context
from app.services.knowledge_sources import consolidate_smallest_url_duplicates
from app.services.pdf_ingestion import PreparedPdf


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
                "content": "Approved AESMC services and appointment information.",
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
                "content": "Approved AESMC doctor and treatment information.",
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


def test_reconcile_backfills_provider_extracted_content_for_runtime_retrieval():
    source = SimpleNamespace(
        source_type="file",
        name="botox.pdf",
        location=None,
        content=None,
        source_metadata=None,
        status="processing",
        provider_item_id=None,
        last_synced_at=None,
        error_message=None,
    )
    knowledge = _knowledge(source)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[
            {
                "_id": "provider-pdf-1",
                "fileName": "botox.pdf",
                "processingStatus": "completed",
                "content": "Botox treatment can be used for hyperhidrosis.",
            }
        ],
        provider_knowledge_base={"processingStatus": "completed"},
        now=datetime.now(UTC),
    )

    assert source.content == "Botox treatment can be used for hyperhidrosis."
    assert source.source_metadata == {"retrieval_content_source": "smallest_index"}


def test_reconcile_preserves_existing_local_pdf_text():
    source = SimpleNamespace(
        source_type="file",
        name="prp.pdf",
        location=None,
        content="Locally extracted PRP guidance.",
        source_metadata={"retrieval_content_source": "local_pdf"},
        status="processing",
        provider_item_id=None,
        last_synced_at=None,
        error_message=None,
    )
    knowledge = _knowledge(source)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[
            {
                "_id": "provider-pdf-2",
                "fileName": "prp.pdf",
                "processingStatus": "completed",
                "content": "Provider PRP text.",
            }
        ],
        provider_knowledge_base={"processingStatus": "completed"},
        now=datetime.now(UTC),
    )

    assert source.content == "Locally extracted PRP guidance."
    assert source.source_metadata == {"retrieval_content_source": "local_pdf"}


def test_reconcile_fails_closed_when_provider_indexed_pdf_has_no_retrievable_text():
    source = SimpleNamespace(
        source_type="file",
        name="unreadable.pdf",
        location=None,
        content=None,
        source_metadata=None,
        status="processing",
        provider_item_id=None,
        last_synced_at=None,
        error_message=None,
    )
    knowledge = _knowledge(source)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[
            {
                "_id": "provider-pdf-unreadable",
                "fileName": "unreadable.pdf",
                "processingStatus": "completed",
            }
        ],
        provider_knowledge_base={"processingStatus": "completed"},
        now=datetime.now(UTC),
    )

    assert source.status == "failed"
    assert "no retrievable text" in source.error_message
    assert knowledge.sync_status == "error"
    assert knowledge.indexed_source_count == 0


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
                "scrapedUrls": [
                    {
                        "url": "https://www.aesmc.com/",
                        "content": "Approved AESMC clinic information.",
                    }
                ],
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


def test_reconcile_completed_knowledge_base_without_text_fails_closed():
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

    assert source.status == "failed"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "error"
    assert knowledge.indexed_source_count == 0
    assert "no retrievable text" in source.error_message


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

    affected = _invalidate_bound_agent_deployments(SimpleNamespace(agent_bindings=[binding]))

    assert affected == []
    assert agent.sync_status == "local_only"
    assert binding.sync_status == "pending"
    assert binding.last_synced_at is None


@pytest.mark.parametrize("provider", ["sarvam", "elevenlabs", "inworld"])
def test_new_provider_source_is_immediately_live_for_vav_native_runtime(provider):
    agent = SimpleNamespace(
        id=uuid4(),
        name=f"{provider} concierge",
        provider_agent_id=None,
        voice_provider=provider,
        sync_status="local_only",
    )
    binding = SimpleNamespace(
        agent=agent,
        provider="smallest",
        sync_status="pending",
        last_synced_at=None,
    )

    affected = _invalidate_bound_agent_deployments(SimpleNamespace(agent_bindings=[binding]))

    assert affected == []
    assert binding.provider == provider
    assert binding.sync_status == "synced"
    assert binding.last_synced_at is not None
    assert agent.sync_status == "local_only"


def test_crawl_invalidation_keeps_inworld_knowledge_binding_live():
    from app.tasks.knowledge_tasks import _invalidate_crawl_bindings

    agent = SimpleNamespace(
        id=uuid4(),
        provider_agent_id=None,
        voice_provider="inworld",
        sync_status="local_only",
    )
    binding = SimpleNamespace(
        agent=agent,
        provider="smallest",
        sync_status="pending",
        last_synced_at=None,
    )

    _invalidate_crawl_bindings(SimpleNamespace(agent_bindings=[binding]))

    assert binding.provider == "inworld"
    assert binding.sync_status == "synced"
    assert binding.last_synced_at is not None


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
        async def list_knowledge_items(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-1"
            return []

        async def upload_knowledge_pdf(self, **kwargs):
            assert kwargs["knowledge_base_id"] == "provider-kb-1"
            assert kwargs["file_name"] == "botox.pdf"
            return {"data": {"_id": "provider-pdf-1"}}

        async def delete_knowledge_item(self, **kwargs):
            raise AssertionError("No stale item should exist")

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", KnowledgeClient)
    monkeypatch.setattr(
        knowledge_endpoint,
        "prepare_pdf",
        lambda *_args, **_kwargs: PreparedPdf(
            provider_content=b"%PDF-1.4\nsearchable",
            extracted_text="Botox treatment knowledge for reliable customer support.",
            extraction_method="native",
            page_count=1,
            sha256="f" * 64,
            ocr_page_count=0,
        ),
    )
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
    first_source_id = response.json()["sources"][0]["id"]
    assert response.json()["agent_bindings"][0]["sync_status"] == "pending"

    replacement = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("botox.pdf", b"%PDF-1.4\nreplacement", "application/pdf")},
    )

    assert replacement.status_code == 200
    assert replacement.json()["source_count"] == 1
    assert replacement.json()["sources"][0]["id"] == first_source_id
    assert replacement.json()["sources"][0]["retrieval_ready"] is True
    persisted_agent = await db.scalar(
        select(Agent).where(Agent.id == agent.id).execution_options(populate_existing=True)
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
                        {
                            "_id": "provider-scraped-page-1",
                            "url": "https://aecmc.com/doctors",
                        },
                        {
                            "_id": "provider-scraped-page-2",
                            "url": "https://aecmc.com/treatments/botox/en",
                        },
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
            provider_item_id=provider_item_id,
        )
        for url, provider_item_id in (
            ("https://aecmc.com/doctors", "provider-scraped-page-1"),
            ("https://aecmc.com/treatments/botox/en", "provider-scraped-page-2"),
        )
    ]
    pdf_source = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge.id,
        source_type="file",
        name="doctors.pdf",
        content="Searchable doctor directory content.",
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


@pytest.mark.asyncio
async def test_failed_website_source_can_be_queued_for_vav_repair(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    queued: list[tuple[list[str], str]] = []

    from app.tasks import knowledge_tasks

    monkeypatch.setattr(
        knowledge_tasks.repair_website_source,
        "apply_async",
        lambda *, args, queue: queued.append((args, queue)),
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Website recovery",
        provider="smallest",
        sync_status="error",
        approval_status="draft",
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="url",
        name="Doctors",
        location="https://www.aesmc.com/doctors",
        status="failed",
        error_message="Provider could not extract this page",
    )
    knowledge.sources.append(source)
    knowledge.source_count = 1
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/{source.id}/repair",
        headers=auth_headers,
    )

    assert response.status_code == 202
    repaired = response.json()["sources"][0]
    assert repaired["status"] == "processing"
    assert repaired["error_message"] is None
    assert repaired["source_metadata"]["recovery_attempts"] == 1
    assert repaired["source_metadata"]["recovery"]["stage"] == "queued"
    assert queued == [([str(tenant.id), str(knowledge.id), str(source.id)], "knowledge")]


@pytest.mark.asyncio
async def test_homepage_crawl_is_persisted_and_queued(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    queued: list[tuple[list[str], str]] = []
    from app.tasks import knowledge_tasks

    monkeypatch.setattr(
        knowledge_tasks.crawl_website,
        "apply_async",
        lambda *, args, queue: queued.append((args, queue)),
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Automatic website knowledge",
        provider="smallest",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/crawls",
        headers=auth_headers,
        json={
            "homepage_url": "https://clinic.example/",
            "max_pages": 120,
            "max_depth": 4,
            "include_subdomains": False,
        },
    )

    assert response.status_code == 202
    crawl_data = response.json()["crawls"][0]
    assert crawl_data["status"] == "queued"
    assert crawl_data["max_pages"] == 120
    assert crawl_data["max_depth"] == 4
    crawl = await db.get(KnowledgeCrawl, UUID(crawl_data["id"]))
    assert crawl is not None
    assert queued == [([str(tenant.id), str(knowledge.id), str(crawl.id)], "knowledge")]


@pytest.mark.asyncio
async def test_permanent_non_content_page_is_excluded_without_failing_the_crawl(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    monkeypatch.setattr(knowledge_tasks, "async_session_factory", lambda: db)
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Corporate website",
        provider="smallest",
        sync_status="error",
        approval_status="draft",
    )
    ready = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Services",
        location="https://company.example/services",
        content="Approved services and contact information.",
        status="indexed",
        provider_item_id="provider-ready",
    )
    empty = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Social media",
        location="https://company.example/social-media",
        status="failed",
        error_message="The downloaded page contained too little readable text.",
    )
    knowledge.sources.extend([ready, empty])
    knowledge.source_count = 2
    knowledge.indexed_source_count = 1
    crawl = KnowledgeCrawl(
        tenant_id=tenant.id,
        root_url="https://company.example/",
        allowed_host="company.example",
        status="completed_with_errors",
        discovered_count=2,
        indexed_count=1,
        failed_count=1,
        skipped_count=3,
        options={"respects_robots": True},
        pages=[],
    )
    knowledge.crawls.append(crawl)
    db.add(knowledge)
    await db.flush()
    crawl.pages.extend(
        [
            KnowledgeCrawlPage(
                tenant_id=tenant.id,
                knowledge_source_id=ready.id,
                url=ready.location,
                canonical_url=ready.location,
                status="indexed",
            ),
            KnowledgeCrawlPage(
                tenant_id=tenant.id,
                knowledge_source_id=empty.id,
                url=empty.location,
                canonical_url=empty.location,
                status="failed",
                error_code="no_readable_text",
            ),
        ]
    )
    await db.commit()

    await knowledge_tasks._mark_failed(
        tenant.id,
        knowledge.id,
        empty.id,
        message="The downloaded page contained too little readable text.",
        code="no_readable_text",
    )

    remaining = list(
        (
            await db.scalars(
                select(KnowledgeSource).where(KnowledgeSource.knowledge_base_id == knowledge.id)
            )
        ).all()
    )
    skipped_page = await db.scalar(
        select(KnowledgeCrawlPage).where(
            KnowledgeCrawlPage.canonical_url == "https://company.example/social-media"
        )
    )
    refreshed_crawl = await db.get(KnowledgeCrawl, crawl.id)
    refreshed_knowledge = await db.get(KnowledgeBase, knowledge.id)

    assert [source.name for source in remaining] == ["Services"]
    assert skipped_page.status == "skipped"
    assert skipped_page.knowledge_source_id is None
    assert refreshed_crawl.status == "completed"
    assert refreshed_crawl.failed_count == 0
    assert refreshed_crawl.skipped_count == 4
    assert refreshed_knowledge.sync_status == "ready"
    assert refreshed_knowledge.source_count == 1


@pytest.mark.asyncio
async def test_pasted_text_can_be_approved_bound_and_retrieved_by_inworld(
    client,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Inworld knowledge concierge",
        system_prompt="Answer only from approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:default",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Native text knowledge",
        provider="smallest",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add_all([agent, knowledge])
    await db.commit()

    added = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/text",
        headers=auth_headers,
        json={
            "name": "Approved clinic FAQ",
            "content": "PRP consultations are available after a doctor completes an assessment.",
        },
    )
    assert added.status_code == 200
    assert added.json()["sync_status"] == "ready"
    assert added.json()["sources"][0]["status"] == "indexed"
    assert added.json()["sources"][0]["retrieval_ready"] is True

    approved = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert approved.status_code == 200

    bound = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/bindings",
        headers=auth_headers,
        json={"agent_id": str(agent.id)},
    )
    assert bound.status_code == 200
    assert bound.json()["agent_bindings"][0]["sync_status"] == "synced"

    context = await retrieve_knowledge_context(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        query="Are PRP consultations available?",
    )
    assert context is not None
    assert "doctor completes an assessment" in context


@pytest.mark.asyncio
async def test_smallest_binding_rejects_vav_native_text_sources(
    client,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Smallest provider agent",
        system_prompt="Answer approved questions.",
        voice_provider="smallest",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Mixed provider knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-text-constraint",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Local FAQ",
            content="Approved locally searchable FAQ content for a VAV runtime.",
            status="indexed",
        )
    )
    db.add_all([agent, knowledge])
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/bindings",
        headers=auth_headers,
        json={"agent_id": str(agent.id)},
    )

    assert response.status_code == 409
    assert "cannot use pasted VAV text" in response.json()["detail"]


@pytest.mark.asyncio
async def test_approval_recounts_and_rejects_provider_indexed_url_without_vav_text(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="False ready website knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-false-ready",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="url",
            name="Provider-only page",
            location="https://example.com/provider-only",
            status="indexed",
        )
    )
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )

    assert response.status_code == 409
    assert "VAV-searchable" in response.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_consolidates_canonical_url_duplicates_and_keeps_searchable_copy(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Canonical website knowledge",
        provider="smallest",
        sync_status="ready",
        approval_status="draft",
    )
    usable = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Services",
        location="https://example.com/services",
        content="Approved treatment and appointment information.",
        status="indexed",
    )
    duplicate = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="url",
        name="Duplicate services",
        location="https://EXAMPLE.com/services/?utm_source=campaign#details",
        status="indexed",
        provider_item_id="provider-services-item",
    )
    knowledge.sources.extend([duplicate, usable])
    knowledge.source_count = 2
    knowledge.indexed_source_count = 2
    crawl = KnowledgeCrawl(
        tenant_id=tenant.id,
        root_url="https://example.com/",
        allowed_host="example.com",
        status="completed",
        discovered_count=1,
        indexed_count=1,
    )
    knowledge.crawls.append(crawl)
    db.add(knowledge)
    await db.flush()
    page = KnowledgeCrawlPage(
        tenant_id=tenant.id,
        crawl_id=crawl.id,
        knowledge_source_id=duplicate.id,
        url=duplicate.location,
        canonical_url="https://example.com/services",
        status="indexed",
    )
    db.add(page)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/refresh",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_count"] == 1
    assert body["indexed_source_count"] == 1
    assert body["sources"][0]["location"] == "https://example.com/services"
    assert body["sources"][0]["retrieval_ready"] is True
    assert body["sources"][0]["provider_item_id"] == "provider-services-item"
    await db.refresh(page)
    assert page.knowledge_source_id == UUID(body["sources"][0]["id"])


@pytest.mark.asyncio
async def test_preferred_repair_content_wins_and_shared_remote_batch_is_retired_once(
    tenant,
    db,
):
    deleted_batches: list[str] = []

    class Provider:
        async def delete_scraped_knowledge_url(self, **kwargs):
            batch_id = kwargs["scraped_url_id"]
            if batch_id in deleted_batches:
                raise AssertionError("A shared stale batch must be retired only once")
            deleted_batches.append(batch_id)

        async def delete_knowledge_item(self, **_kwargs):
            raise AssertionError("The fresh replacement item must be retained")

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Repair freshness knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-repair",
        sync_status="ready",
        approval_status="draft",
    )
    preferred = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Current services",
        location="https://example.com/services",
        content="Current shorter service information.",
        status="indexed",
        provider_item_id="current-item",
    )
    old_content = "Obsolete service information. " * 20
    duplicates = [
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="url",
            name=f"Old services {index}",
            location=f"https://EXAMPLE.com/services/?utm_source=old-{index}",
            content=old_content,
            status="indexed",
            provider_item_id="old-scrape-batch",
        )
        for index in range(2)
    ]
    knowledge.sources.extend([*duplicates, preferred])
    db.add(knowledge)
    await db.flush()

    removed = await consolidate_smallest_url_duplicates(
        db,
        knowledge,
        Provider(),
        scraped=[
            {
                "_id": "old-scrape-batch",
                "hostUrl": "https://example.com/services",
                "scrapedUrls": [{"url": "https://example.com/services"}],
            }
        ],
        items=[
            {
                "_id": "current-item",
                "metadata": {"sourceUrl": "https://example.com/services"},
            }
        ],
        preferred_source=preferred,
    )
    await db.flush()

    assert removed == 2
    assert deleted_batches == ["old-scrape-batch"]
    assert list(knowledge.sources) == [preferred]
    assert preferred.content == "Current shorter service information."
    assert preferred.provider_item_id == "current-item"


@pytest.mark.asyncio
async def test_adding_content_revokes_existing_knowledge_approval(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved clinic knowledge",
        provider="smallest",
        sync_status="ready",
        approval_status="approved",
        published_at=datetime.now(UTC),
    )
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/text",
        headers=auth_headers,
        json={
            "name": "New clinic policy",
            "content": "Appointments require confirmation before the scheduled visit.",
        },
    )

    assert response.status_code == 200
    assert response.json()["approval_status"] == "draft"
    assert response.json()["published_at"] is None


@pytest.mark.asyncio
async def test_refresh_retrieval_evidence_change_revokes_approval(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    content = "Current approved clinic hours are nine in the morning until six in the evening."

    class Provider:
        async def get_knowledge_base(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-status-change"
            return {"processingStatus": "completed"}

        async def list_scraped_knowledge_urls(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-status-change"
            return [
                {
                    "_id": "provider-hours",
                    "url": "https://example.com/hours",
                    "processingStatus": "completed",
                    "content": content,
                }
            ]

        async def list_knowledge_items(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-status-change"
            return []

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", Provider)
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved status-change knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-status-change",
        sync_status="processing",
        approval_status="approved",
        published_at=datetime.now(UTC),
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="url",
            name="Clinic hours",
            location="https://example.com/hours",
            content=content,
            status="processing",
            provider_item_id="provider-hours",
        )
    )
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/refresh",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["sources"][0]["status"] == "indexed"
    assert response.json()["approval_status"] == "draft"
    assert response.json()["published_at"] is None


@pytest.mark.asyncio
async def test_remote_knowledge_base_provisioning_is_idempotent_under_lock(tenant, db):
    created: list[str] = []

    class Provider:
        async def create_knowledge_base(self, **_kwargs):
            created.append("provider-kb-serialized")
            return "provider-kb-serialized"

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Serialized provisioning knowledge",
        provider="smallest",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.flush()

    first = await knowledge_endpoint._ensure_remote(db, knowledge, Provider())
    second = await knowledge_endpoint._ensure_remote(db, knowledge, Provider())

    assert first == second == "provider-kb-serialized"
    assert created == ["provider-kb-serialized"]


@pytest.mark.asyncio
async def test_ambiguous_remote_provisioning_blocks_duplicate_creation(tenant, db):
    attempts = 0

    class Provider:
        async def create_knowledge_base(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise SmallestAIError(
                "Provider response was lost after submission.",
                ambiguous=True,
            )

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Ambiguous provisioning knowledge",
        provider="smallest",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.flush()

    with pytest.raises(HTTPException):
        await knowledge_endpoint._ensure_remote(db, knowledge, Provider())
    with pytest.raises(HTTPException) as blocked:
        await knowledge_endpoint._ensure_remote(db, knowledge, Provider())

    assert blocked.value.status_code == 409
    assert attempts == 1
