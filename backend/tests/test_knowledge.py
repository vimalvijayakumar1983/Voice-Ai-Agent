import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

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
    KnowledgeProviderCleanup,
    KnowledgeSource,
)
from app.providers.smallest import SmallestAIError
from app.services.knowledge_retrieval import retrieve_knowledge_context
from app.services.knowledge_serving import publish_serving_revision
from app.services.knowledge_sources import consolidate_smallest_url_duplicates
from app.services.pdf_ingestion import PreparedPdf
from app.services.speech_lexicon import publish_speech_lexicon


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
        provider="smallest",
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
                "stage": "provider_indexing",
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
async def test_pdf_upload_replaces_existing_unbound_provider_source(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    provider_items: list[dict] = []
    upload_names: list[str] = []

    class KnowledgeClient:
        async def list_knowledge_items(self, knowledge_base_id):
            assert knowledge_base_id == "provider-kb-1"
            return list(provider_items)

        async def upload_knowledge_pdf(self, **kwargs):
            assert kwargs["knowledge_base_id"] == "provider-kb-1"
            assert kwargs["file_name"].startswith("vav-pdf-")
            assert kwargs["file_name"].endswith(".pdf")
            upload_names.append(kwargs["file_name"])
            item_id = f"provider-pdf-{len(upload_names)}"
            provider_items.append(
                {
                    "_id": item_id,
                    "fileName": kwargs["file_name"],
                    "processingStatus": "processing",
                }
            )
            return {"data": {"_id": item_id}}

        async def delete_knowledge_item(self, **kwargs):
            raise AssertionError("No stale item should exist")

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", KnowledgeClient)
    kicked_cleanup_ids: list[UUID] = []
    monkeypatch.setattr(
        knowledge_endpoint,
        "_kick_provider_cleanup",
        lambda cleanup_ids: kicked_cleanup_ids.extend(cleanup_ids),
    )
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
    await db.commit()

    response = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("botox.pdf", b"%PDF-1.4\nknowledge", "application/pdf")},
    )

    assert response.status_code == 200
    first_source_id = response.json()["sources"][0]["id"]

    replacement = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("botox.pdf", b"%PDF-1.4\nreplacement", "application/pdf")},
    )

    assert replacement.status_code == 200
    assert replacement.json()["source_count"] == 1
    assert replacement.json()["sources"][0]["id"] == first_source_id
    assert replacement.json()["sources"][0]["retrieval_ready"] is True
    assert upload_names[0] != upload_names[1]
    cleanup = await db.scalar(
        select(KnowledgeProviderCleanup).where(
            KnowledgeProviderCleanup.provider_item_id == "provider-pdf-1"
        )
    )
    assert cleanup is not None
    assert cleanup.status == "pending"
    assert cleanup.knowledge_source_id == UUID(first_source_id)
    assert kicked_cleanup_ids == [cleanup.id]


@pytest.mark.asyncio
async def test_pdf_upload_failure_is_quarantined_and_cannot_be_approved_or_bound(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    list_calls = 0
    uploaded_name: str | None = None

    class KnowledgeClient:
        async def list_knowledge_items(self, knowledge_base_id):
            nonlocal list_calls
            assert knowledge_base_id == "provider-kb-pdf-failure"
            list_calls += 1
            if list_calls == 1:
                return [
                    {
                        "_id": "provider-pdf-current",
                        "fileName": "existing.pdf",
                        "processingStatus": "completed",
                    }
                ]
            raise SmallestAIError(
                "Provider listing failed after accepting the upload",
                upstream_status_code=503,
            )

        async def upload_knowledge_pdf(self, **kwargs):
            nonlocal uploaded_name
            uploaded_name = kwargs["file_name"]
            return {"data": {"_id": "provider-pdf-orphan"}}

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", KnowledgeClient)
    monkeypatch.setattr(knowledge_endpoint, "_kick_provider_cleanup", lambda _ids: None)
    monkeypatch.setattr(
        knowledge_endpoint,
        "prepare_pdf",
        lambda *_args, **_kwargs: PreparedPdf(
            provider_content=b"%PDF-1.4\nsearchable",
            extracted_text="Replacement information that must not be published.",
            extraction_method="native",
            page_count=1,
            sha256="e" * 64,
            ocr_page_count=0,
        ),
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved PDF knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-pdf-failure",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="file",
        name="existing.pdf",
        content="Previously approved PDF content.",
        file_content=b"%PDF-1.4\nold",
        mime_type="application/pdf",
        size_bytes=16,
        status="indexed",
        provider_item_id="provider-pdf-current",
    )
    knowledge.sources.append(source)
    agent = Agent(
        tenant_id=tenant.id,
        name="Unbound Smallest agent",
        system_prompt="Use approved provider knowledge.",
        voice_provider="smallest",
        voice_id="smallest:default",
    )
    db.add_all([knowledge, agent])
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    agent_id = agent.id

    response = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("existing.pdf", b"%PDF-1.4\nnew", "application/pdf")},
    )

    assert response.status_code == 502
    assert uploaded_name is not None and uploaded_name.startswith("vav-pdf-")
    db.expire_all()
    persisted = await db.get(KnowledgeSource, source_id)
    persisted_kb = await db.get(KnowledgeBase, knowledge_id)
    assert persisted.content == "Previously approved PDF content."
    assert persisted.provider_item_id == "provider-pdf-current"
    assert persisted_kb.approval_status == "approved"
    cleanup = await db.scalar(
        select(KnowledgeProviderCleanup).where(
            KnowledgeProviderCleanup.knowledge_base_id == knowledge_id,
            KnowledgeProviderCleanup.provider_artifact_name == uploaded_name,
        )
    )
    assert cleanup is not None
    assert cleanup.status == "pending"
    assert cleanup.provider_item_id.startswith("pending-upload:")

    approval = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    refresh = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/refresh",
        headers=auth_headers,
    )
    binding = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/bindings",
        headers=auth_headers,
        json={"agent_id": str(agent_id)},
    )
    assert approval.status_code == 409
    assert "remote knowledge cleanup" in approval.json()["detail"]
    assert refresh.status_code == 409
    assert "remote knowledge cleanup" in refresh.json()["detail"]
    assert binding.status_code == 409
    assert "remote knowledge cleanup" in binding.json()["detail"]


@pytest.mark.asyncio
async def test_pdf_delete_commits_cleanup_intent_before_remote_delete(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    class KnowledgeClient:
        async def delete_knowledge_item(self, **_kwargs):
            raise AssertionError("The request must not perform an inline remote delete")

    monkeypatch.setattr(knowledge_endpoint, "get_smallest_client", KnowledgeClient)
    kicked_cleanup_ids: list[UUID] = []
    monkeypatch.setattr(
        knowledge_endpoint,
        "_kick_provider_cleanup",
        lambda cleanup_ids: kicked_cleanup_ids.extend(cleanup_ids),
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="PDF deletion safety",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-pdf-delete",
        sync_status="ready",
        approval_status="approved",
        source_count=2,
        indexed_source_count=2,
    )
    pdf_source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="file",
        name="retire.pdf",
        content="Provider-backed PDF to retire.",
        status="indexed",
        provider_item_id="provider-pdf-retire",
    )
    text_source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="text",
        name="Retained text",
        content="Approved content that keeps the mutable draft retrieval-ready.",
        status="indexed",
    )
    knowledge.sources.extend([pdf_source, text_source])
    agent = Agent(
        tenant_id=tenant.id,
        name="Cleanup-blocked Smallest agent",
        system_prompt="Use approved knowledge.",
        voice_provider="smallest",
        voice_id="smallest:default",
    )
    db.add_all([knowledge, agent])
    await db.commit()
    knowledge_id = knowledge.id
    pdf_source_id = pdf_source.id
    agent_id = agent.id

    response = await client.delete(
        f"/api/v1/knowledge/{knowledge_id}/sources/{pdf_source_id}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert [source["name"] for source in response.json()["sources"]] == ["Retained text"]
    cleanup = await db.scalar(
        select(KnowledgeProviderCleanup).where(
            KnowledgeProviderCleanup.provider_item_id == "provider-pdf-retire"
        )
    )
    assert cleanup is not None
    assert cleanup.status == "pending"
    assert cleanup.knowledge_source_id is None
    assert kicked_cleanup_ids == [cleanup.id]
    db.expire_all()
    assert await db.get(KnowledgeSource, pdf_source_id) is None

    approval = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/approval",
        headers=auth_headers,
        json={"approved": True},
    )
    binding = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/bindings",
        headers=auth_headers,
        json={"agent_id": str(agent_id)},
    )
    assert approval.status_code == 409
    assert "remote knowledge cleanup" in approval.json()["detail"]
    assert binding.status_code == 409
    assert "remote knowledge cleanup" in binding.json()["detail"]


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
        provider="smallest",
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
        provider_item_id="provider-contact",
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
        provider="smallest",
        provider_knowledge_base_id="provider-kb-approved",
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
        provider_item_id="provider-current-contact",
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
    assert refreshed_source.provider_item_id == "provider-current-contact"
    assert refreshed_source.error_message is None
    assert refreshed_source.source_metadata["recovery_error_code"] == "http_error"
    assert refreshed_source.source_metadata["recovery"]["status"] == "failed"
    assert "staged_refresh" not in refreshed_source.source_metadata


@pytest.mark.asyncio
async def test_smallest_bound_refresh_is_rejected_before_queue_or_live_provider_mutation(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    queued = []
    monkeypatch.setattr(
        knowledge_tasks.repair_website_source,
        "apply_async",
        lambda **kwargs: queued.append(kwargs),
    )
    agent = Agent(
        tenant_id=tenant.id,
        name="Live Smallest receptionist",
        system_prompt="Use only approved provider knowledge.",
        voice_provider="smallest",
        voice_id="smallest:default",
        provider_agent_id="provider-agent-live",
        sync_status="synced",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Live provider collection",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-live",
        sync_status="ready",
        approval_status="approved",
        published_at=datetime.now(UTC),
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Approved contact page",
        location="https://company.example/contact",
        content="Approved contact details.",
        raw_content="Approved contact details.",
        content_sha256="b" * 64,
        status="indexed",
        provider_item_id="provider-item-live",
    )
    knowledge.sources.append(source)
    db.add_all([agent, knowledge])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="smallest",
            sync_status="synced",
        )
    )
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id

    response = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/{source_id}/repair",
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert "blue/green provider knowledge-base swap" in response.json()["detail"]
    assert queued == []
    deleted = await client.delete(
        f"/api/v1/knowledge/{knowledge_id}/sources/{source_id}",
        headers=auth_headers,
    )
    uploaded = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("replacement.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    added_urls = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/urls",
        headers=auth_headers,
        json={"urls": ["https://company.example/new"]},
    )
    assert deleted.status_code == 409
    assert uploaded.status_code == 409
    assert added_urls.status_code == 409
    db.expire_all()
    unchanged_knowledge = await db.get(KnowledgeBase, knowledge_id)
    unchanged_source = await db.get(KnowledgeSource, source_id)
    assert unchanged_knowledge.approval_status == "approved"
    assert unchanged_source.source_metadata is None
    assert unchanged_source.provider_item_id == "provider-item-live"


@pytest.mark.asyncio
async def test_worker_rechecks_smallest_binding_before_provider_upload(tenant, db, monkeypatch):
    from app.services.website_recovery import WebsiteRecoveryError
    from app.tasks import knowledge_tasks

    repair_run_id = str(uuid4())
    agent = Agent(
        tenant_id=tenant.id,
        name="Late-bound Smallest agent",
        system_prompt="Use approved provider knowledge.",
        voice_provider="smallest",
        voice_id="smallest:default",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Provider isolation fence",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-live",
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
        content="Approved content.",
        status="indexed",
        provider_item_id="provider-item-live",
        source_metadata={"repair_run_id": repair_run_id, "staged_refresh": True},
    )
    knowledge.sources.append(source)
    db.add_all([agent, knowledge])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="smallest",
            sync_status="pending",
        )
    )
    await db.commit()
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )

    with pytest.raises(WebsiteRecoveryError) as raised:
        await knowledge_tasks._ensure_provider_refresh_isolated(
            tenant.id,
            knowledge.id,
            source.id,
            repair_run_id=repair_run_id,
        )

    assert raised.value.code == "provider_blue_green_required"


@pytest.mark.asyncio
async def test_smallest_provider_publish_rejects_active_recovery_and_crawl(tenant, db):
    from app.api.v1.endpoints.agents import _approved_bound_provider_knowledge_base_id

    agent = Agent(
        tenant_id=tenant.id,
        name="Smallest publishing fence",
        system_prompt="Use approved provider knowledge.",
        voice_provider="smallest",
        voice_id="smallest:default",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Publishing fence knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-fenced",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Recovering source",
        location="https://company.example/contact",
        content="Approved content.",
        status="indexed",
        provider_item_id="provider-current",
        source_metadata={
            "recovery": {"stage": "compiling", "status": "processing"},
        },
    )
    knowledge.sources.append(source)
    db.add_all([agent, knowledge])
    await db.flush()
    db.add(
        AgentKnowledgeBinding(
            tenant_id=tenant.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge.id,
            provider="smallest",
            sync_status="pending",
        )
    )
    await db.commit()

    with pytest.raises(HTTPException, match="crawl or recovery work in progress"):
        await _approved_bound_provider_knowledge_base_id(
            db,
            agent_id=agent.id,
            tenant_id=tenant.id,
        )

    source.source_metadata = {
        "recovery": {"stage": "verified", "status": "completed"},
    }
    crawl = KnowledgeCrawl(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge.id,
        root_url="https://company.example/",
        allowed_host="company.example",
        status="discovering",
        max_pages=10,
        max_depth=2,
    )
    db.add(crawl)
    await db.flush()
    with pytest.raises(HTTPException, match="crawl or recovery work in progress"):
        await _approved_bound_provider_knowledge_base_id(
            db,
            agent_id=agent.id,
            tenant_id=tenant.id,
        )

    crawl.status = "completed"
    assert (
        await _approved_bound_provider_knowledge_base_id(
            db,
            agent_id=agent.id,
            tenant_id=tenant.id,
        )
        == "provider-kb-fenced"
    )


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
        provider="smallest",
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
        provider_item_id="provider-old",
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
        provider_item_id="provider-new",
        artifact_name="vav-web-recovery-new.pdf",
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
        provider="smallest",
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
        provider_item_id="provider-same",
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
        provider_item_id="provider-same",
        artifact_name="vav-web-recovery-same.pdf",
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
        provider_item_id="provider-newest",
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
    assert refreshed.provider_item_id == "provider-newest"
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
        provider_item_id="provider-newest",
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
        provider_item_id="provider-stale",
        artifact_name="vav-web-recovery-stale.pdf",
        requested_mode="ai_verified",
        reused_compilation=False,
    )

    assert committed is False
    db.expire_all()
    refreshed = await db.get(KnowledgeSource, source_id)
    assert refreshed.name == "Newest result"
    assert refreshed.raw_content == "Newest raw source"
    assert refreshed.content == "Newest compiled source"
    assert refreshed.provider_item_id == "provider-newest"
    assert refreshed.source_metadata == newest_metadata


@pytest.mark.asyncio
async def test_repair_commit_transactionally_outboxes_superseded_provider_artifact(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    repair_run_id = str(uuid4())
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Transactional cleanup knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-transactional",
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Old page",
        location="https://company.example/page",
        raw_content="Old raw page",
        content="Old compiled page",
        structured_content=_compiled_structure(value="Old fact"),
        status="indexed",
        provider_item_id="provider-old",
        source_metadata={
            "repair_run_id": repair_run_id,
            "repair_generation": 2,
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
    upload_reservation_id = await knowledge_tasks._reserve_provider_upload_artifact(
        tenant.id,
        knowledge_id,
        source_id,
        repair_run_id=repair_run_id,
        remote_id="provider-kb-transactional",
        artifact_name="vav-web-recovery-new.pdf",
    )

    committed = await knowledge_tasks._commit_repair_success(
        tenant.id,
        knowledge_id,
        source_id,
        repair_run_id=repair_run_id,
        page=_recovered_page("New raw page"),
        compiled=_compiled_result(
            "New compiled page",
            _compiled_structure(value="New fact"),
        ),
        raw_content_sha256=hashlib.sha256(b"New raw page").hexdigest(),
        compiled_content_sha256=hashlib.sha256(b"New compiled page").hexdigest(),
        provider_item_id="provider-new",
        artifact_name="vav-web-recovery-new.pdf",
        requested_mode="ai_verified",
        reused_compilation=False,
        provider_cleanup_ids=("provider-old",),
        provider_upload_reservation_id=upload_reservation_id,
    )

    assert committed is True
    db.expire_all()
    cleanup = await db.scalar(
        select(KnowledgeProviderCleanup).where(
            KnowledgeProviderCleanup.provider_item_id == "provider-old"
        )
    )
    refreshed_source = await db.get(KnowledgeSource, source_id)
    assert cleanup is not None
    assert cleanup.knowledge_base_id == knowledge_id
    assert cleanup.knowledge_source_id == source_id
    assert cleanup.repair_run_id == repair_run_id
    assert cleanup.status == "pending"
    assert await db.get(KnowledgeProviderCleanup, upload_reservation_id) is None
    assert refreshed_source.provider_item_id == "provider-new"
    assert refreshed_source.source_metadata["provider_cleanup_pending_ids"] == ["provider-old"]


@pytest.mark.asyncio
async def test_stale_run_cannot_delete_provider_artifact_reserved_for_cleanup(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    old_run_id = str(uuid4())
    new_run_id = str(uuid4())
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Provider cleanup generation guard",
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
        content="Newest compiled source",
        status="indexed",
        provider_item_id="provider-newest",
        source_metadata={
            "repair_run_id": new_run_id,
            "repair_generation": 9,
            "provider_cleanup_pending_ids": ["provider-old"],
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

    class Provider:
        def __init__(self):
            self.deleted = []

        async def delete_knowledge_item(self, **kwargs):
            self.deleted.append(kwargs["item_id"])

    provider = Provider()
    await knowledge_tasks._cleanup_provider_artifacts(
        tenant.id,
        knowledge_id,
        source_id,
        repair_run_id=old_run_id,
        provider=provider,
        remote_id="provider-kb",
        provider_item_ids=("provider-old",),
    )

    assert provider.deleted == []
    db.expire_all()
    refreshed = await db.get(KnowledgeSource, source_id)
    assert refreshed.source_metadata["provider_cleanup_pending_ids"] == ["provider-old"]


@pytest.mark.asyncio
async def test_uncommitted_run_unique_artifact_is_deleted_but_current_artifact_is_retained(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Uncommitted artifact cleanup",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Current source",
        location="https://company.example/contact",
        content="Current content",
        status="indexed",
        provider_item_id="provider-current",
    )
    knowledge.sources.append(source)
    db.add(knowledge)
    await db.commit()
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )

    class Provider:
        def __init__(self):
            self.deleted = []

        async def delete_knowledge_item(self, **kwargs):
            self.deleted.append(kwargs["item_id"])

    provider = Provider()
    await knowledge_tasks._cleanup_uncommitted_provider_artifact(
        tenant.id,
        knowledge.id,
        source.id,
        provider=provider,
        remote_id="provider-kb",
        provider_item_id="provider-stale-run-unique",
    )
    await knowledge_tasks._cleanup_uncommitted_provider_artifact(
        tenant.id,
        knowledge.id,
        source.id,
        provider=provider,
        remote_id="provider-kb",
        provider_item_id="provider-current",
    )

    assert provider.deleted == ["provider-stale-run-unique"]


@pytest.mark.asyncio
async def test_pending_upload_cleanup_waits_for_eventual_listing_then_deletes_artifact(
    tenant,
    db,
    monkeypatch,
):
    from app.tasks import knowledge_tasks

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Crash-compensated PDF",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-pending-upload",
        sync_status="error",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.flush()
    cleanup = KnowledgeProviderCleanup(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge.id,
        provider="smallest",
        provider_knowledge_base_id="provider-kb-pending-upload",
        provider_item_id=f"pending-upload:{uuid4()}",
        provider_artifact_name="vav-pdf-eventual.pdf",
        status="pending",
        attempts=0,
        available_at=datetime.now(UTC),
    )
    db.add(cleanup)
    await db.commit()
    cleanup_id = cleanup.id
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )

    class Provider:
        def __init__(self):
            self.visible = False
            self.deleted: list[str] = []

        async def list_knowledge_items(self, _knowledge_base_id):
            if not self.visible:
                return []
            return [
                {
                    "_id": "provider-eventual-item",
                    "fileName": "vav-pdf-eventual.pdf",
                }
            ]

        async def delete_knowledge_item(self, **kwargs):
            self.deleted.append(kwargs["item_id"])

    provider = Provider()
    first = await knowledge_tasks._process_provider_cleanup(cleanup_id, provider=provider)
    assert first == "pending"
    db.expire_all()
    retained = await db.get(KnowledgeProviderCleanup, cleanup_id)
    assert retained is not None
    assert retained.attempts == 1
    assert "not visible" in retained.last_error

    retained.available_at = datetime.now(UTC)
    provider.visible = True
    await db.commit()
    second = await knowledge_tasks._process_provider_cleanup(cleanup_id, provider=provider)
    assert second == "completed"
    assert provider.deleted == ["provider-eventual-item"]
    db.expire_all()
    assert await db.get(KnowledgeProviderCleanup, cleanup_id) is None


@pytest.mark.asyncio
async def test_failed_orphan_cleanup_is_durable_and_blocks_smallest_bind_and_publish(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import agents as agents_endpoint
    from app.tasks import knowledge_tasks

    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Cleanup-gated knowledge",
        provider="smallest",
        provider_knowledge_base_id="provider-kb-cleanup",
        sync_status="ready",
        approval_status="approved",
        source_count=1,
        indexed_source_count=1,
    )
    source = KnowledgeSource(
        tenant_id=tenant.id,
        source_type="website",
        name="Current source",
        location="https://company.example/contact",
        content="Current approved content",
        status="indexed",
        provider_item_id="provider-current",
    )
    knowledge.sources.append(source)
    agent = Agent(
        tenant_id=tenant.id,
        name="Cleanup-gated Smallest agent",
        system_prompt="Use only approved knowledge.",
        voice_provider="smallest",
        voice_id="smallest:default",
    )
    db.add_all([knowledge, agent])
    await db.commit()
    knowledge_id = knowledge.id
    source_id = source.id
    agent_id = agent.id
    monkeypatch.setattr(
        knowledge_tasks,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )

    class FailingProvider:
        async def delete_knowledge_item(self, **_kwargs):
            raise SmallestAIError(
                "Provider cleanup temporarily unavailable",
                upstream_status_code=503,
            )

    await knowledge_tasks._cleanup_uncommitted_provider_artifact(
        tenant.id,
        knowledge_id,
        source_id,
        provider=FailingProvider(),
        remote_id="provider-kb-cleanup",
        provider_item_id="provider-orphan",
        repair_run_id=str(uuid4()),
    )

    cleanup = await db.scalar(
        select(KnowledgeProviderCleanup).where(
            KnowledgeProviderCleanup.provider_item_id == "provider-orphan"
        )
    )
    assert cleanup is not None
    assert cleanup.status == "pending"
    assert cleanup.attempts == 1
    assert cleanup.last_error.startswith("SmallestAIError:")

    blocked_bind = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/bindings",
        headers=auth_headers,
        json={"agent_id": str(agent_id)},
    )
    assert blocked_bind.status_code == 409
    assert "remote knowledge cleanup" in blocked_bind.json()["detail"]

    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent_id,
        knowledge_base_id=knowledge_id,
        provider="smallest",
        sync_status="pending",
    )
    db.add(binding)
    await db.commit()
    binding_id = binding.id
    cleanup_id = cleanup.id
    with pytest.raises(HTTPException, match="remote artifact cleanup"):
        await agents_endpoint._approved_bound_provider_knowledge_base_id(
            db,
            agent_id=agent_id,
            tenant_id=tenant.id,
        )
    await db.rollback()
    binding = await db.get(AgentKnowledgeBinding, binding_id)
    await db.delete(binding)
    cleanup = await db.get(KnowledgeProviderCleanup, cleanup_id)
    cleanup.available_at = datetime.now(UTC)
    await db.commit()

    class SuccessfulProvider:
        def __init__(self):
            self.deleted = []

        async def delete_knowledge_item(self, **kwargs):
            self.deleted.append(kwargs["item_id"])

    provider = SuccessfulProvider()
    outcome = await knowledge_tasks._process_provider_cleanup(cleanup_id, provider=provider)
    assert outcome == "completed"
    assert provider.deleted == ["provider-orphan"]
    db.expire_all()
    assert await db.get(KnowledgeProviderCleanup, cleanup_id) is None

    allowed_bind = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/bindings",
        headers=auth_headers,
        json={"agent_id": str(agent_id)},
    )
    assert allowed_bind.status_code == 200


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
        provider="smallest",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add_all([agent, knowledge])
    await db.commit()

    first = await client.post(
        f"/api/v1/knowledge/{knowledge.id}/sources/text",
        headers=auth_headers,
        json={"name": "Published FAQ", "content": "The published answer is blue."},
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
        provider="smallest",
        sync_status="local_only",
        approval_status="draft",
    )
    db.add(knowledge)
    await db.commit()
    knowledge_id = knowledge.id

    added = await client.post(
        f"/api/v1/knowledge/{knowledge_id}/sources/text",
        headers=auth_headers,
        json={"name": "Published FAQ", "content": "The approved answer is blue."},
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
