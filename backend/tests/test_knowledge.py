import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.endpoints import knowledge as knowledge_endpoint
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeCrawlPage,
    KnowledgeSource,
)
from app.services import website_crawler
from app.services.knowledge_records import make_record
from app.services.knowledge_retrieval import retrieve_knowledge_context
from app.services.knowledge_serving import publish_serving_revision
from app.services.knowledge_sources import (
    consolidate_duplicate_url_sources,
    mark_native_bindings_live,
)
from app.services.pdf_ingestion import PreparedPdf
from app.services.speech_lexicon import publish_speech_lexicon


def _compiled_structure(
    *,
    version: str = "knowledge-compiler-v8",
    value: str = "Reception is open daily.",
    input_tokens: int = 10,
    warning: str | None = None,
) -> dict:
    return {
        "schema_version": "vav-knowledge-v1",
        "page_type": "contact",
        "entities": [],
        "speech_entities": [],
        "facts": [
            {
                "subject": "Example Medical Centre",
                "predicate": "hours",
                "value": value,
                "evidence": value,
                "search_phrases": ["When are you open?"],
            }
        ],
        "exact_fact_coverage": {"complete": False, "absence_authoritative": False},
        "validation": {"facts_accepted": 1, "elapsed_ms": input_tokens},
        "compiler": {
            "version": version,
            "requested_mode": "ai_verified",
            "effective_mode": "ai_verified",
            "model": "gpt-4.1-mini",
            "input_tokens": input_tokens,
            "output_tokens": 20,
            "estimated_cost_usd": input_tokens / 1_000_000,
            "pricing_snapshot_date": "2026-09-01",
            "warning": warning,
        },
    }


def test_compiled_serving_signature_is_canonical_and_ignores_run_diagnostics():
    from app.tasks.knowledge_tasks import _compiled_serving_signature

    first = _compiled_structure(input_tokens=10)
    second = _compiled_structure(input_tokens=999, warning="Transient compiler warning")
    # Dict insertion order is not part of the serving contract either.
    second = dict(reversed(list(second.items())))

    assert _compiled_serving_signature(content="Compiled answer", structured=first) == (
        _compiled_serving_signature(content="Compiled answer", structured=second)
    )
    assert _compiled_serving_signature(content="Compiled answer", structured=first) != (
        _compiled_serving_signature(
            content="Compiled answer",
            structured=_compiled_structure(version="knowledge-compiler-v9"),
        )
    )
    assert _compiled_serving_signature(content="Compiled answer", structured=first) != (
        _compiled_serving_signature(
            content="Compiled answer",
            structured=_compiled_structure(value="Reception is closed on Sunday."),
        )
    )


def test_repair_generation_remains_monotonic_for_legacy_metadata():
    from app.tasks.knowledge_tasks import _queue_repair_metadata

    metadata, _, attempts = _queue_repair_metadata(
        {"recovery_attempts": 2, "repair_generation": 7},
        staged_refresh=False,
        message="Queued",
    )

    assert attempts == 3
    assert metadata["recovery_attempts"] == 3
    assert metadata["repair_generation"] == 8


@pytest.mark.asyncio
async def test_stale_repair_sweeper_fences_old_run_and_queues_new_generation(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    old_run_id = str(uuid4())
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Lost repair recovery",
        sync_status="processing",
        approval_status="draft",
        source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Lost page",
        location="https://company.example/lost",
        status="processing",
        source_metadata={
            "repair_run_id": old_run_id,
            "repair_generation": 3,
            "recovery_attempts": 3,
            "recovery": {
                "stage": "indexing",
                "status": "processing",
                "updated_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        },
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()
    tenant_id = tenant.id
    knowledge_id = knowledge.id
    source_id = source.id
    queued: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    monkeypatch.setattr(
        knowledge_tasks.repair_website_source,
        "apply_async",
        lambda *, args, queue: queued.append((args, queue)),
    )

    recovered = await knowledge_tasks._sweep_stale_knowledge_repairs()

    assert recovered == 1
    db.expire_all()
    refreshed = await db.get(KnowledgeSource, source_id)
    new_run_id = refreshed.source_metadata["repair_run_id"]
    assert new_run_id != old_run_id
    assert refreshed.source_metadata["repair_generation"] == 4
    assert refreshed.source_metadata["recovery"]["status"] == "queued"
    assert queued == [
        ([str(tenant_id), str(knowledge_id), str(source_id), new_run_id], "knowledge")
    ]

    await knowledge_tasks._mark_failed(
        tenant_id,
        knowledge_id,
        source_id,
        message="Late failure from the dead worker.",
        code="stale_worker_failure",
        repair_run_id=old_run_id,
    )
    db.expire_all()
    refreshed = await db.get(KnowledgeSource, source_id)
    assert refreshed.source_metadata["repair_run_id"] == new_run_id
    assert refreshed.source_metadata["recovery"]["status"] == "queued"


def _compiled_result(content: str, structured: dict):
    from app.services.knowledge_compiler import CompiledKnowledge

    compiler = structured.get("compiler") or {}
    return CompiledKnowledge(
        content=content,
        structured=structured,
        effective_mode=str(compiler.get("effective_mode") or "fast"),
        model=str(compiler.get("model") or "") or None,
        input_tokens=int(compiler.get("input_tokens") or 0),
        output_tokens=int(compiler.get("output_tokens") or 0),
        estimated_cost_usd=float(compiler.get("estimated_cost_usd") or 0),
        warning=compiler.get("warning"),
    )


def _recovered_page(text: str):
    from app.services.website_recovery import RecoveredPage

    return RecoveredPage(
        "https://company.example/contact",
        "Contact",
        text,
        "static_html",
        len(text.encode()),
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

    mark_native_bindings_live(SimpleNamespace(agent_bindings=[binding]))

    assert binding.provider == provider
    assert binding.sync_status == "synced"
    assert binding.last_synced_at is not None
    assert agent.sync_status == "local_only"


def test_legacy_binding_to_a_non_vav_agent_is_never_reported_as_synced():
    agent = SimpleNamespace(id=uuid4(), voice_provider="smallest", sync_status="synced")
    binding = SimpleNamespace(
        agent=agent,
        provider="smallest",
        sync_status="synced",
        last_synced_at=datetime.now(UTC),
    )

    mark_native_bindings_live(SimpleNamespace(agent_bindings=[binding]))

    assert binding.provider == "smallest"
    assert binding.sync_status == "pending"
    assert binding.last_synced_at is None


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
        provider="vav",
        sync_status="pending",
        last_synced_at=None,
    )

    _invalidate_crawl_bindings(SimpleNamespace(agent_bindings=[binding]))

    assert binding.provider == "inworld"
    assert binding.sync_status == "synced"
    assert binding.last_synced_at is not None


def _prepared_pdf(text: str, records=()) -> PreparedPdf:
    return PreparedPdf(
        provider_content=b"%PDF-1.4\nsearchable",
        extracted_text=text,
        extraction_method="native",
        page_count=1,
        sha256="f" * 64,
        ocr_page_count=0,
        records=tuple(records),
    )


@pytest.mark.asyncio
async def test_pdf_upload_is_indexed_locally_and_replaces_the_same_filename(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    row = make_record("table_row", ["Service: Consultation", "Price: AED 150"], page=1)
    monkeypatch.setattr(
        knowledge_endpoint,
        "prepare_pdf",
        lambda *_args, **_kwargs: _prepared_pdf(
            "Fee schedule\n\nService: Consultation | Price: AED 150", records=[row]
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
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="inworld",
            sync_status="pending",
        )
    )
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("fees.pdf", b"%PDF-1.4\nknowledge", "application/pdf")},
        data={"processing_mode": "fast"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync_status"] == "ready"
    assert body["approval_status"] == "draft"
    first_source = body["sources"][0]
    assert first_source["status"] == "indexed"
    assert first_source["retrieval_ready"] is True
    assert first_source["provider_item_id"] is None
    assert first_source["source_metadata"]["record_count"] == 1
    assert first_source["source_metadata"]["coverage"]["record_total"] == 1
    assert body["agent_bindings"][0]["sync_status"] == "synced"

    replacement = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("fees.pdf", b"%PDF-1.4\nreplacement", "application/pdf")},
        data={"processing_mode": "fast"},
    )

    assert replacement.status_code == 200, replacement.text
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

    assert response.status_code == 202, response.text
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
    assert {queue for _args, queue in queued} == {"knowledge"}
    queued_runs = {args[2]: args[3] for args, _queue in queued}
    assert queued_runs == {
        source["id"]: source["source_metadata"]["repair_run_id"] for source in body["sources"]
    }

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

    assert response.status_code == 200, response.text
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

    assert response.status_code == 200, response.text
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
    assert await db.scalar(select(AgentKnowledgeBinding.id)) is None


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
async def test_refresh_consolidation_revokes_approval_when_sources_change(
    client,
    auth_headers,
    tenant,
    db,
):
    content = "Current approved clinic hours are nine in the morning until six in the evening."
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved duplicate-page knowledge",
        sync_status="ready",
        approval_status="approved",
        published_at=datetime.now(UTC),
        source_count=2,
        indexed_source_count=2,
    )
    knowledge.sources.extend(
        [
            KnowledgeSource(
                tenant_id=tenant.id,
                source_type="url",
                name="Clinic hours",
                location="https://example.com/hours",
                content=content,
                status="indexed",
            ),
            KnowledgeSource(
                tenant_id=tenant.id,
                source_type="url",
                name="Clinic hours (tracked link)",
                location="https://example.com/hours/?utm_campaign=spring",
                content=content,
                status="indexed",
            ),
        ]
    )
    db.add(knowledge)
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/refresh",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_count"] == 1
    assert body["sources"][0]["location"] == "https://example.com/hours"
    assert body["approval_status"] == "draft"
    assert body["published_at"] is None


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
        sync_status="error",
        approval_status="draft",
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="url",
        name="Doctors",
        location="https://www.aesmc.com/doctors",
        status="failed",
        error_message="The crawler could not extract this page",
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
    assert repaired["source_metadata"]["repair_generation"] == 1
    repair_run_id = repaired["source_metadata"]["repair_run_id"]
    assert str(UUID(repair_run_id)) == repair_run_id
    assert repaired["source_metadata"]["recovery"]["stage"] == "queued"
    assert queued == [
        ([str(tenant.id), str(knowledge.id), str(source.id), repair_run_id], "knowledge")
    ]


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
    repair_run_id = body["sources"][0]["source_metadata"]["repair_run_id"]
    assert queued == [
        ([str(tenant.id), str(knowledge.id), str(source.id), repair_run_id], "knowledge")
    ]

    await knowledge_tasks._set_stage(
        tenant.id,
        knowledge.id,
        source.id,
        "fetching",
        "Downloading the candidate page.",
        repair_run_id=repair_run_id,
    )
    await knowledge_tasks._mark_failed(
        tenant.id,
        knowledge.id,
        source.id,
        message="The candidate page timed out.",
        code="download_timeout",
        repair_run_id=repair_run_id,
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
async def test_permanent_failure_during_staged_refresh_keeps_previous_source_live(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    repair_run_id = str(uuid4())
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved provider knowledge",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Contact",
        location="https://company.example/contact",
        raw_content="Previously approved raw contact information.",
        content="Previously approved compiled contact information.",
        status="indexed",
        source_metadata={
            "repair_run_id": repair_run_id,
            "repair_generation": 4,
            "staged_refresh": True,
        },
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )

    await knowledge_tasks._mark_failed(
        tenant.id,
        knowledge_id,
        source_id,
        message="The refreshed page returned HTTP 410.",
        code="http_error",
        repair_run_id=repair_run_id,
    )

    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge_id)
    refreshed_source = await db.get(KnowledgeSource, source_id)
    assert refreshed.approval_status == "approved"
    assert refreshed.sync_status == "ready"
    assert refreshed_source is not None
    assert refreshed_source.status == "indexed"
    assert refreshed_source.raw_content == "Previously approved raw contact information."
    assert refreshed_source.content == "Previously approved compiled contact information."
    assert refreshed_source.error_message is None
    assert refreshed_source.source_metadata["recovery_error_code"] == "http_error"
    assert refreshed_source.source_metadata["recovery"]["status"] == "failed"
    assert "staged_refresh" not in refreshed_source.source_metadata


@pytest.mark.asyncio
async def test_same_raw_v7_to_v8_compiler_change_creates_pending_draft_and_retains_live_release(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    raw_text = "Example Medical Centre reception hours are 8 AM to 8 PM."
    raw_sha256 = hashlib.sha256(raw_text.encode()).hexdigest()
    old_content = "OLD COMPILED: Reception hours are 8 AM to 8 PM."
    old_structured = _compiled_structure(
        version="knowledge-compiler-v7",
        value="Reception hours are 8 AM to 8 PM.",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Versioned compiler refresh",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Contact",
        location="https://company.example/contact",
        raw_content=raw_text,
        content=old_content,
        structured_content=old_structured,
        content_sha256=raw_sha256,
        status="indexed",
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        allow_draft_for_approval=True,
    )
    live_revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )
    knowledge.approval_status = "approved"
    knowledge.published_at = live_revision.published_at
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    live_revision_id = live_revision.id

    repair_run_id = str(uuid4())
    source.source_metadata = {
        "processing_mode": "ai_verified",
        "repair_run_id": repair_run_id,
        "repair_generation": 2,
        "recovery_attempts": 2,
        "staged_refresh": True,
        "force_recompile": True,
    }
    await db.commit()
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    next_content = "NEW COMPILED: Reception is available daily from 8 AM to 8 PM."
    compiled = _compiled_result(
        next_content,
        _compiled_structure(
            version="knowledge-compiler-v8",
            value="Reception is available daily from 8 AM to 8 PM.",
        ),
    )

    committed = await knowledge_tasks._commit_repair_success(
        tenant.id,
        knowledge_id,
        source_id,
        repair_run_id=repair_run_id,
        page=_recovered_page(raw_text),
        compiled=compiled,
        raw_content_sha256=raw_sha256,
        compiled_content_sha256=hashlib.sha256(next_content.encode()).hexdigest(),
        requested_mode="ai_verified",
        reused_compilation=False,
    )

    assert committed is True
    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge_id)
    refreshed_source = await db.get(KnowledgeSource, source_id)
    assert refreshed.approval_status == "draft"
    assert refreshed.serving_revision_id == live_revision_id
    assert refreshed_source.content_sha256 == raw_sha256
    assert refreshed_source.content == next_content
    assert "staged_refresh" not in refreshed_source.source_metadata
    assert "force_recompile" not in refreshed_source.source_metadata
    assert refreshed_source.source_metadata["compiled_serving_signature_v1"] == (
        knowledge_tasks._compiled_serving_signature(
            content=next_content,
            structured=compiled.structured,
        )
    )
    response = await client.get(
        f"/api/v1/knowledge/{knowledge_id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["has_pending_changes"] is True
    assert response.json()["serving_revision"]["revision_id"] == str(live_revision_id)


@pytest.mark.asyncio
async def test_staged_refresh_with_identical_serving_signature_keeps_approval(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    raw_text = "Example Medical Centre reception is open daily."
    content = "COMPILED: Reception is open daily."
    original_structured = _compiled_structure(input_tokens=10)
    repair_run_id = str(uuid4())
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="No-op compiler refresh",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Contact",
        location="https://company.example/contact",
        raw_content=raw_text,
        content=content,
        structured_content=original_structured,
        content_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
        status="indexed",
        source_metadata={
            "processing_mode": "ai_verified",
            "repair_run_id": repair_run_id,
            "repair_generation": 3,
            "recovery_attempts": 3,
            "staged_refresh": True,
        },
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    raw_sha256 = source.content_sha256
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    recompiled = _compiled_result(
        content,
        _compiled_structure(input_tokens=999, warning="Transient compiler warning"),
    )

    committed = await knowledge_tasks._commit_repair_success(
        tenant.id,
        knowledge_id,
        source_id,
        repair_run_id=repair_run_id,
        page=_recovered_page(raw_text),
        compiled=recompiled,
        raw_content_sha256=raw_sha256,
        compiled_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        requested_mode="ai_verified",
        reused_compilation=False,
    )

    assert committed is True
    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge_id)
    refreshed_source = await db.get(KnowledgeSource, source_id)
    assert refreshed.approval_status == "approved"
    assert "staged_refresh" not in refreshed_source.source_metadata


@pytest.mark.asyncio
async def test_stale_failure_cannot_overwrite_newer_success(tenant, db, monkeypatch):
    from app.tasks import knowledge_tasks

    old_run_id = str(uuid4())
    new_run_id = str(uuid4())
    newest_metadata = {
        "repair_run_id": new_run_id,
        "repair_generation": 5,
        "recovery_attempts": 5,
        "recovery": {"stage": "verified", "status": "completed", "message": "Ready"},
    }
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Repair generation guard",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="New result",
        location="https://company.example/contact",
        raw_content="Newest raw source",
        content="Newest compiled source",
        structured_content=_compiled_structure(),
        content_sha256=hashlib.sha256(b"Newest raw source").hexdigest(),
        status="indexed",
        source_metadata=newest_metadata,
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )

    await knowledge_tasks._mark_failed(
        tenant.id,
        knowledge_id,
        source_id,
        message="The stale run found no readable text after the new run completed.",
        code="no_readable_text",
        repair_run_id=old_run_id,
    )

    db.expire_all()
    refreshed = await db.get(KnowledgeSource, source_id)
    assert refreshed.status == "indexed"
    assert refreshed.content == "Newest compiled source"
    assert refreshed.error_message is None
    assert refreshed.source_metadata == newest_metadata


@pytest.mark.asyncio
async def test_stale_old_success_cannot_overwrite_newer_success(tenant, db, monkeypatch):
    from app.tasks import knowledge_tasks

    old_run_id = str(uuid4())
    new_run_id = str(uuid4())
    newest_metadata = {
        "repair_run_id": new_run_id,
        "repair_generation": 8,
        "recovery_attempts": 8,
        "recovery": {"stage": "verified", "status": "completed", "message": "Ready"},
    }
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Stale success guard",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Newest result",
        location="https://company.example/contact",
        raw_content="Newest raw source",
        content="Newest compiled source",
        structured_content=_compiled_structure(value="Newest verified fact."),
        content_sha256=hashlib.sha256(b"Newest raw source").hexdigest(),
        status="indexed",
        source_metadata=newest_metadata,
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    committed = await knowledge_tasks._commit_repair_success(
        tenant.id,
        knowledge_id,
        source_id,
        repair_run_id=old_run_id,
        page=_recovered_page("Stale old raw source"),
        compiled=_compiled_result(
            "Stale old compiled source",
            _compiled_structure(value="Stale old fact."),
        ),
        raw_content_sha256=hashlib.sha256(b"Stale old raw source").hexdigest(),
        compiled_content_sha256=hashlib.sha256(b"Stale old compiled source").hexdigest(),
        requested_mode="ai_verified",
        reused_compilation=False,
    )

    assert committed is False
    db.expire_all()
    refreshed = await db.get(KnowledgeSource, source_id)
    assert refreshed.name == "Newest result"
    assert refreshed.raw_content == "Newest raw source"
    assert refreshed.content == "Newest compiled source"
    assert refreshed.source_metadata == newest_metadata


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
            "processing_mode": "fast",
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
    speech_lexicon = approved.json()["speech_lexicon"]
    assert speech_lexicon["artifact_id"]
    assert speech_lexicon["compiler_version"] == "vav-speech-lexicon-1"
    assert speech_lexicon["source_count"] == 1
    assert speech_lexicon["entry_count"] >= 1
    assert speech_lexicon["coverage"]["tier_one_coverage_pct"] == 100.0

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
async def test_native_agent_can_bind_last_live_release_while_new_draft_is_pending(
    client,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Release-pinned concierge",
        system_prompt="Answer only from the published release.",
        voice_provider="inworld",
        voice_id="inworld:default",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Blue-green native knowledge",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add_all([agent, knowledge])
    await db.commit()

    first = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/text",
        headers=auth_headers,
        json={
            "name": "Published FAQ",
            "content": "The published answer is blue.",
            "processing_mode": "fast",
        },
    )
    assert first.status_code == 200
    approved = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert approved.status_code == 200
    live_revision_id = approved.json()["serving_revision"]["revision_id"]

    staged = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/text",
        headers=auth_headers,
        json={"name": "Pending FAQ", "content": "The unapproved answer is green."},
    )
    assert staged.status_code == 200
    assert staged.json()["approval_status"] == "draft"
    assert staged.json()["serving_revision"]["revision_id"] == live_revision_id

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
        query="What is the published answer?",
    )
    assert context is not None
    assert "published answer is blue" in context
    assert "unapproved answer is green" not in context


@pytest.mark.asyncio
async def test_explicit_unapproval_increments_revocation_generation_once_for_live_pointer(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Revocation-fenced knowledge",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.commit()
    knowledge_id = knowledge.id

    added = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/text",
        headers=auth_headers,
        json={
            "name": "Published FAQ",
            "content": "The approved answer is blue.",
            "processing_mode": "fast",
        },
    )
    assert added.status_code == 200
    approved = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert approved.status_code == 200
    staged = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/text",
        headers=auth_headers,
        json={"name": "Draft FAQ", "content": "A draft answer is green."},
    )
    assert staged.status_code == 200
    assert staged.json()["approval_status"] == "draft"
    assert staged.json()["serving_revision"] is not None

    revoked = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/approval",
        headers=auth_headers,
        json={"approved": False},
    )
    assert revoked.status_code == 200
    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge_id)
    assert refreshed.serving_revision_id is None
    assert refreshed.serving_revocation_generation == 1

    repeated = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/approval",
        headers=auth_headers,
        json={"approved": False},
    )
    assert repeated.status_code == 200
    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge_id)
    assert refreshed.serving_revocation_generation == 1


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


def _coverage_text_source(tenant, *, name: str, coverage: dict | None) -> KnowledgeSource:
    return KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name=name,
        content=f"{name} approved content for callers.",
        raw_content=f"{name} approved content for callers.",
        status="indexed",
        source_metadata={"coverage": coverage} if coverage is not None else {},
    )


@pytest.mark.asyncio
async def test_approval_blocks_uncompiled_sources_and_acknowledges_partial_coverage(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Coverage-gated knowledge",
        sync_status="ready",
        approval_status="draft",
    )
    source = _coverage_text_source(
        tenant,
        name="Doctor directory",
        coverage={"status": "not_compiled", "record_total": 4, "records_covered": 0},
    )
    knowledge.sources.append(source)
    knowledge.source_count = 1
    knowledge.indexed_source_count = 1
    db.add(knowledge)
    await db.commit()

    blocked = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert blocked.status_code == 409
    assert "Recompile" in blocked.json()["detail"]
    assert "Doctor directory" in blocked.json()["detail"]

    source.source_metadata = {
        "coverage": {
            "status": "partial",
            "record_total": 4,
            "records_covered": 3,
            "uncovered": ["Dr Dalia Hassan | General Practitioner | 23+ Years Experience"],
            "uncovered_total": 1,
            "facts_accepted": 6,
        }
    }
    await db.commit()

    listed = await client.get(f"/api/v1/knowledge/{knowledge.id}", headers=auth_headers)
    issues = listed.json()["sources"][0]["quality_issues"]
    assert any("Partial coverage: 3 of 4 records" in issue for issue in issues)

    partial = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert partial.status_code == 409
    assert "accept_partial_coverage" in partial.json()["detail"]

    acknowledged = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True, "accept_partial_coverage": True},
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["approval_status"] == "approved"


@pytest.mark.asyncio
async def test_legacy_sources_without_coverage_still_approve_but_are_flagged(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Legacy knowledge",
        sync_status="ready",
        approval_status="draft",
    )
    knowledge.sources.append(_coverage_text_source(tenant, name="Legacy FAQ", coverage=None))
    knowledge.source_count = 1
    knowledge.indexed_source_count = 1
    db.add(knowledge)
    await db.commit()

    listed = await client.get(f"/api/v1/knowledge/{knowledge.id}", headers=auth_headers)
    issues = listed.json()["sources"][0]["quality_issues"]
    assert any("Coverage not measured" in issue for issue in issues)

    approved = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert approved.status_code == 200


@pytest.mark.asyncio
async def test_reindex_requeues_every_source_type_with_the_current_pipeline(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_compile_tasks as jobs
    from app.tasks import knowledge_tasks

    repairs: list[tuple[list[str], str]] = []
    compiles: list[list[str]] = []
    monkeypatch.setattr(
        knowledge_tasks.repair_website_source,
        "apply_async",
        lambda *, args, queue: repairs.append((args, queue)),
    )
    monkeypatch.setattr(
        jobs.compile_upload, "apply_async", lambda *, args, retry: compiles.append(args)
    )
    row = make_record("table_row", ["Service: Consultation", "Price: AED 150"], page=1)
    monkeypatch.setattr(
        jobs,
        "prepare_pdf",
        lambda *_args, **_kwargs: _prepared_pdf(
            "Fee schedule\n\nService: Consultation | Price: AED 150", records=[row]
        ),
    )
    tenant_id = tenant.id
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Legacy pipeline knowledge",
        sync_status="ready",
        approval_status="approved",
        published_at=datetime.now(UTC),
        source_count=3,
        indexed_source_count=3,
    )
    raw_page = "Doctors: Dr Randa Ahmed General Practitioner"
    web = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Doctors",
        location="https://clinic.example/doctors",
        raw_content=raw_page,
        content="COMPILED: Dr Randa Ahmed is a General Practitioner.",
        structured_content=_compiled_structure(),
        content_sha256=hashlib.sha256(raw_page.encode()).hexdigest(),
        status="indexed",
    )
    text = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Legacy FAQ",
        content="Opening hours: 9 AM to 9 PM daily",
        status="indexed",
    )
    pdf = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="file",
        name="fees.pdf",
        file_content=b"%PDF-1.4\nstored",
        raw_content="Fee schedule Service Price Consultation AED 150",
        content="Flat old extraction",
        mime_type="application/pdf",
        status="indexed",
        source_metadata={"extraction_method": "native", "page_count": 1},
    )
    knowledge.sources.extend([web, text, pdf])
    db.add(knowledge)
    await db.commit()
    knowledge_id, web_id, text_id, pdf_id = knowledge.id, web.id, text.id, pdf.id

    response = await client.post(f"/api/v1/knowledge/{knowledge_id}/reindex", headers=auth_headers)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["approval_status"] == "draft"
    assert body["sync_status"] == "processing"
    sources = {source["id"]: source for source in body["sources"]}
    web_body = sources[str(web_id)]
    assert web_body["status"] == "indexed"  # staged: the approved page stays live
    assert web_body["source_metadata"]["staged_refresh"] is True
    assert web_body["source_metadata"]["force_recompile"] is True
    assert web_body["source_metadata"]["recovery"]["stage"] == "queued"
    repair_run_id = web_body["source_metadata"]["repair_run_id"]
    assert repairs == [
        ([str(tenant_id), str(knowledge_id), str(web_id), repair_run_id], "knowledge")
    ]
    for source_id in (text_id, pdf_id):
        job = sources[str(source_id)]["source_metadata"]["upload_compile"]
        assert sources[str(source_id)]["status"] == "processing"
        assert job["status"] == "queued"
        assert job["enqueued_at"]
    assert {args[2] for args in compiles} == {str(text_id), str(pdf_id)}
    db.expire_all()
    refreshed = await db.get(KnowledgeBase, knowledge_id)
    assert refreshed.reindex_requested_at is not None

    again = await client.post(f"/api/v1/knowledge/{knowledge_id}/reindex", headers=auth_headers)
    assert again.status_code == 409

    pdf_run_id = sources[str(pdf_id)]["source_metadata"]["upload_compile"]["run_id"]

    async def compile_knowledge(*, title, text, requested_mode, **_kwargs):
        structured = _compiled_structure(value="AED 150")
        structured["facts"] = [
            {
                "subject": "Consultation",
                "predicate": "price",
                "value": "AED 150",
                "evidence": "Service: Consultation | Price: AED 150",
                "search_phrases": ["consultation fee"],
            }
        ]
        structured["compiler"]["requested_mode"] = requested_mode
        return _compiled_result(f"COMPILED {title}: {text}", structured)

    monkeypatch.setattr(jobs, "compile_source_knowledge", compile_knowledge)
    monkeypatch.setattr(
        jobs, "async_session_factory", async_sessionmaker(db.bind, expire_on_commit=False)
    )
    await db.commit()
    await jobs._compile(str(tenant_id), str(knowledge_id), str(pdf_id), pdf_run_id)

    db.expire_all()
    stored = await db.get(KnowledgeSource, pdf_id)
    assert stored.status == "indexed"
    assert stored.raw_content == "Fee schedule\n\nService: Consultation | Price: AED 150"
    assert stored.content.startswith("COMPILED fees.pdf")
    assert [record["text"] for record in stored.structured_content["records"]] == [
        "Service: Consultation | Price: AED 150"
    ]
    assert stored.source_metadata["coverage"]["status"] == "complete"
    assert stored.source_metadata["coverage"]["record_total"] == 1
    assert stored.source_metadata["upload_compile"]["status"] == "completed"


@pytest.mark.asyncio
async def test_approval_requires_measured_coverage_once_a_knowledge_base_is_reindexed(
    client,
    auth_headers,
    tenant,
    db,
):
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Re-indexed knowledge",
        sync_status="ready",
        approval_status="draft",
        reindex_requested_at=datetime.now(UTC),
    )
    knowledge.sources.append(_coverage_text_source(tenant, name="Unmeasured FAQ", coverage=None))
    knowledge.source_count = 1
    knowledge.indexed_source_count = 1
    db.add(knowledge)
    await db.commit()

    blocked = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert blocked.status_code == 409
    assert "never measured" in blocked.json()["detail"]

    knowledge.reindex_requested_at = None
    await db.commit()
    tolerated = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    assert tolerated.status_code == 200


@pytest.mark.asyncio
async def test_paginated_directory_pages_are_merged_into_one_source(monkeypatch):
    from app.services.website_recovery import RecoveredPage, extract_page_records
    from app.tasks import knowledge_tasks

    def listing(page: int, names: list[str], *, next_page: int | None) -> str:
        cards = "".join(
            f'<div class="card"><h3>{name}</h3><p>General Practitioner</p></div>' for name in names
        )
        link = f'<a href="/doctors?page={next_page}">Next</a>' if next_page else ""
        intro = (
            "<p>Our doctors provide family medicine, paediatrics and dental care across "
            "Abu Dhabi with same-day appointments and insurance support for every patient.</p>"
        )
        return (
            "<html><head><title>Our Doctors</title></head><body><main><h1>Our Doctors</h1>"
            f'{intro}{cards}<div class="pagination">{link}</div></main></body></html>'
        )

    pages = {
        "https://clinic.example/doctors": listing(1, ["Dr One", "Dr Two"], next_page=2),
        "https://clinic.example/doctors?page=2": listing(2, ["Dr Three"], next_page=3),
        "https://clinic.example/doctors?page=3": listing(3, ["Dr Four"], next_page=None),
    }
    fetched: list[str] = []

    async def download(url):
        fetched.append(url)
        return url, pages[url], len(pages[url])

    monkeypatch.setattr(knowledge_tasks, "download_html", download)
    first_url = "https://clinic.example/doctors"
    signatures: set = set()
    title, records = extract_page_records(
        pages[first_url], url=first_url, card_signatures=signatures
    )
    first = RecoveredPage(first_url, title, "", "static_html", len(pages[first_url]))

    merged = await knowledge_tasks._follow_pagination(first, pages[first_url], records, signatures)

    assert fetched == [
        "https://clinic.example/doctors?page=2",
        "https://clinic.example/doctors?page=3",
    ]
    assert merged.pages == 3
    for name in ("Dr One", "Dr Two", "Dr Three", "Dr Four"):
        assert f"{name} | General Practitioner" in merged.text
    assert merged.text.count("Our Doctors") == 1  # the listing heading once, not per page
