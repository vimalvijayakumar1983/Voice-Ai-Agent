from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeCrawlPage,
    KnowledgeSource,
)
from app.services import website_crawler
from app.services.knowledge_retrieval import retrieve_knowledge_context
from app.services.knowledge_sources import (
    consolidate_duplicate_url_sources,
    mark_knowledge_bindings_live,
)
from app.services.pdf_ingestion import PreparedPdf


def _prepared_pdf(text: str) -> PreparedPdf:
    return PreparedPdf(
        provider_content=b"%PDF-1.4\nsearchable",
        extracted_text=text,
        extraction_method="native",
        page_count=1,
        sha256="f" * 64,
        ocr_page_count=0,
    )


@pytest.mark.parametrize("provider", ["sarvam", "elevenlabs", "inworld"])
def test_source_changes_are_live_for_every_bound_vav_agent(provider):
    agent = SimpleNamespace(id=uuid4(), voice_provider=provider, sync_status="local_only")
    binding = SimpleNamespace(
        agent=agent,
        provider="vav",
        sync_status="pending",
        last_synced_at=None,
    )

    mark_knowledge_bindings_live(SimpleNamespace(agent_bindings=[binding]))

    assert binding.provider == provider
    assert binding.sync_status == "synced"
    assert binding.last_synced_at is not None
    assert agent.sync_status == "local_only"


@pytest.mark.asyncio
async def test_pdf_upload_is_indexed_locally_and_replaces_the_same_filename(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import knowledge as knowledge_endpoint

    monkeypatch.setattr(
        knowledge_endpoint,
        "prepare_pdf",
        lambda *_args, **_kwargs: _prepared_pdf(
            "Botox treatment knowledge for reliable customer support."
        ),
    )
    agent = Agent(
        tenant_id=tenant.id,
        name="Inworld concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:default",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Adam and Eve Medical Knowledge",
        sync_status="local_only",
        approval_status="approved",
        published_at=datetime.now(UTC),
    )
    db.add_all([agent, knowledge])
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent.id,
        knowledge_base_id=knowledge.id,
        provider="inworld",
        sync_status="pending",
    )
    db.add(binding)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("botox.pdf", b"%PDF-1.4\nknowledge", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "vav"
    assert body["sync_status"] == "ready"
    assert body["approval_status"] == "draft"
    first_source = body["sources"][0]
    assert first_source["status"] == "indexed"
    assert first_source["retrieval_ready"] is True
    assert first_source["source_metadata"]["retrieval_content_source"] == "vav_pdf_ingestion"
    assert body["agent_bindings"][0]["sync_status"] == "synced"

    replacement = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("botox.pdf", b"%PDF-1.4\nreplacement", "application/pdf")},
    )

    assert replacement.status_code == 200
    assert replacement.json()["source_count"] == 1
    assert replacement.json()["sources"][0]["id"] == first_source["id"]
    stored = await db.get(KnowledgeSource, UUID(first_source["id"]))
    await db.refresh(stored)
    assert stored.file_content == b"%PDF-1.4\nreplacement"
    assert stored.provider_item_id is None


@pytest.mark.asyncio
async def test_url_sources_are_registered_and_queued_for_local_extraction(
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
        name="Curated pages",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/urls",
        headers=auth_headers,
        json={"urls": ["https://clinic.example/doctors/", "https://clinic.example/offers"]},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["sync_status"] == "processing"
    assert sorted(source["location"] for source in body["sources"]) == [
        "https://clinic.example/doctors",
        "https://clinic.example/offers",
    ]
    assert all(source["status"] == "processing" for source in body["sources"])
    assert all(
        source["source_metadata"]["recovery"]["stage"] == "queued" for source in body["sources"]
    )
    assert sorted(args[2] for args, _queue in queued) == sorted(
        source["id"] for source in body["sources"]
    )
    assert {queue for _args, queue in queued} == {"knowledge"}

    duplicate = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/urls",
        headers=auth_headers,
        json={"urls": ["https://clinic.example/doctors"]},
    )
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_sitemap_discovery_reads_the_sitemap_locally(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.clinic.example/doctors</loc></url>
      <url><loc>https://www.clinic.example/offers?utm_source=x</loc></url>
      <url><loc>https://other.example/page</loc></url>
      <url><loc>https://www.clinic.example/brochure.pdf</loc></url>
    </urlset>"""

    async def download(url, *, supported_types):
        assert url == "https://www.clinic.example/sitemap.xml"
        return url, sitemap, len(sitemap)

    monkeypatch.setattr(website_crawler, "download_public_text", download)
    knowledge = KnowledgeBase(tenant_id=tenant.id, name="Sitemap knowledge")
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sitemap/discover",
        headers=auth_headers,
        json={"sitemap_url": "https://www.clinic.example/sitemap.xml"},
    )

    assert response.status_code == 200
    assert response.json()["urls"] == [
        "https://www.clinic.example/doctors",
        "https://www.clinic.example/offers",
    ]


@pytest.mark.asyncio
async def test_delete_source_is_local_and_keeps_bindings_live(
    client,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Sarvam concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="sarvam",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Adam and Eve Medical Knowledge",
        sync_status="ready",
        approval_status="approved",
        published_at=datetime.now(UTC),
    )
    db.add_all([agent, knowledge])
    await db.flush()
    page = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge.id,
        source_type="url",
        name="doctors",
        location="https://aecmc.com/doctors",
        content="Doctor directory.",
        status="indexed",
    )
    pdf_source = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge.id,
        source_type="file",
        name="doctors.pdf",
        content="Searchable doctor directory content.",
        status="indexed",
    )
    db.add_all([page, pdf_source])
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="sarvam",
            sync_status="synced",
        )
    )
    await db.commit()

    response = await client.delete(
        f"/api/v1/knowledge/{knowledge.id}/sources/{page.id}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert [source["name"] for source in body["sources"]] == ["doctors.pdf"]
    assert body["source_count"] == 1
    assert body["indexed_source_count"] == 1
    assert body["approval_status"] == "draft"
    assert body["agent_bindings"][0]["sync_status"] == "synced"
    remaining = (
        await db.scalars(
            select(KnowledgeSource).where(KnowledgeSource.knowledge_base_id == knowledge.id)
        )
    ).all()
    assert [source.name for source in remaining] == ["doctors.pdf"]


@pytest.mark.asyncio
async def test_binding_rejects_agents_that_do_not_use_vav_retrieval(
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
        name="Approved knowledge",
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
    assert "VAV-native" in response.json()["detail"]


@pytest.mark.asyncio
async def test_preferred_repair_content_wins_when_consolidating_duplicates(tenant, db):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Repair freshness knowledge",
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
        )
        for index in range(2)
    ]
    knowledge.sources.extend([*duplicates, preferred])
    db.add(knowledge)
    await db.flush()

    removed = await consolidate_duplicate_url_sources(db, knowledge, preferred_source=preferred)
    await db.flush()

    assert removed == 2
    assert list(knowledge.sources) == [preferred]
    assert preferred.content == "Current shorter service information."
    assert preferred.location == "https://example.com/services"


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
        provider="vav",
        sync_status="error",
        approval_status="draft",
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="url",
        name="Doctors",
        location="https://www.aesmc.com/doctors",
        status="failed",
        error_message="VAV could not extract this page",
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
async def test_approved_ready_source_refresh_keeps_previous_version_live(
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
    monkeypatch.setattr(knowledge_tasks, "async_session_factory", lambda: db)
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved website knowledge",
        provider="vav",
        sync_status="ready",
        approval_status="approved",
        published_at=datetime.now(UTC),
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Contact",
        location="https://company.example/contact",
        content="Previously approved contact information.",
        raw_content="Previously approved contact information.",
        content_sha256="a" * 64,
        status="indexed",
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/{source.id}/repair",
        headers=auth_headers,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["approval_status"] == "approved"
    assert body["sync_status"] == "ready"
    assert body["sources"][0]["status"] == "indexed"
    assert body["sources"][0]["source_metadata"]["staged_refresh"] is True
    assert queued == [([str(tenant.id), str(knowledge.id), str(source.id)], "knowledge")]

    await knowledge_tasks._set_stage(
        tenant.id,
        knowledge.id,
        source.id,
        "fetching",
        "Downloading the candidate page.",
    )
    await knowledge_tasks._mark_failed(
        tenant.id,
        knowledge.id,
        source.id,
        message="The candidate page timed out.",
        code="download_timeout",
    )
    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge.id)
    refreshed_source = await db.get(KnowledgeSource, source.id)

    assert refreshed.approval_status == "approved"
    assert refreshed.sync_status == "ready"
    assert refreshed_source.status == "indexed"
    assert refreshed_source.content == "Previously approved contact information."
    assert refreshed_source.source_metadata["recovery"]["status"] == "failed"
    assert (
        "previous approved content remains active"
        in refreshed_source.source_metadata["recovery"]["message"]
    )


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
        provider="vav",
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
            "processing_mode": "ai_verified",
        },
    )

    assert response.status_code == 202
    crawl_data = response.json()["crawls"][0]
    assert crawl_data["status"] == "queued"
    assert crawl_data["max_pages"] == 120
    assert crawl_data["max_depth"] == 4
    assert crawl_data["options"]["processing_mode"] == "ai_verified"
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
        provider="vav",
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
        provider="vav",
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
async def test_approval_recounts_and_rejects_indexed_url_without_vav_text(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="False ready website knowledge",
        provider="vav",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    knowledge.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="url",
            name="Page without extracted text",
            location="https://example.com/no-text",
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
        provider="vav",
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
    await db.refresh(page)
    assert page.knowledge_source_id == UUID(body["sources"][0]["id"])


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
        provider="vav",
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
