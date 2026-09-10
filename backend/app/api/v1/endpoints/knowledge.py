from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import PurePath
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeSource,
)
from app.schemas.knowledge import (
    AgentKnowledgeBindRequest,
    KnowledgeAgentBindingResponse,
    KnowledgeAIDraftRequest,
    KnowledgeAIDraftResponse,
    KnowledgeApprovalRequest,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeCrawlCreate,
    KnowledgeCrawlResponse,
    KnowledgeSourceResponse,
    SitemapDiscoveryRequest,
    SitemapDiscoveryResponse,
    TextSourceCreate,
    UrlSourceCreate,
)
from app.services.audit import record_audit_event
from app.services.knowledge_ai_wizard import (
    KnowledgeAIWizardError,
    generate_knowledge_ai_draft,
)
from app.services.knowledge_records import (
    coverage_blocks_approval,
    records_from_text,
    records_to_payload,
    render_records,
)
from app.services.knowledge_sources import (
    KNOWLEDGE_PROVIDER,
    VAV_NATIVE_KNOWLEDGE_PROVIDERS,
    canonical_source_url,
    consolidate_duplicate_url_sources,
    has_searchable_content,
    invalidate_knowledge_approval,
    mark_knowledge_bindings_live,
)
from app.services.pdf_ingestion import PdfIngestionError, PreparedPdf, prepare_pdf
from app.services.provider_credentials import ProviderCredentialError, load_provider_config
from app.services.rate_limit import enforce_rate_limit
from app.services.website_crawler import discover_sitemap_urls
from app.services.website_recovery import WebsiteRecoveryError, recovery_metadata

router = APIRouter(prefix="/knowledge", tags=["Knowledge Studio"])
MAX_KNOWLEDGE_PDF_BYTES = 8 * 1024 * 1024
MAX_SITEMAP_URLS = 500


def _source_response(source: KnowledgeSource) -> KnowledgeSourceResponse:
    content = source.content if isinstance(source.content, str) else ""
    extracted = source.raw_content if isinstance(source.raw_content, str) else content
    response = KnowledgeSourceResponse.model_validate(source)
    return response.model_copy(
        update={
            "retrieval_ready": bool(content.strip()),
            "extracted_character_count": len(extracted.strip()),
        }
    )


def _knowledge_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    bindings = [
        KnowledgeAgentBindingResponse(
            id=binding.id,
            agent_id=binding.agent_id,
            agent_name=binding.agent.name,
            knowledge_base_id=binding.knowledge_base_id,
            sync_status=binding.sync_status,
            last_synced_at=binding.last_synced_at,
        )
        for binding in kb.agent_bindings
    ]
    return KnowledgeBaseResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        provider=kb.provider,
        sync_status=kb.sync_status,
        sync_error=kb.sync_error,
        approval_status=kb.approval_status,
        scope_type=kb.scope_type,
        scope_label=kb.scope_label,
        languages=kb.languages or ["en"],
        tags=kb.tags or [],
        source_count=kb.source_count,
        indexed_source_count=kb.indexed_source_count,
        last_synced_at=kb.last_synced_at,
        published_at=kb.published_at,
        sources=[_source_response(source) for source in kb.sources],
        agent_bindings=bindings,
        crawls=[KnowledgeCrawlResponse.model_validate(crawl) for crawl in kb.crawls],
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def _knowledge_query(tenant_id: UUID):
    return (
        select(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == tenant_id)
        .options(
            selectinload(KnowledgeBase.sources),
            selectinload(KnowledgeBase.agent_bindings).selectinload(AgentKnowledgeBinding.agent),
            selectinload(KnowledgeBase.crawls).selectinload(KnowledgeCrawl.pages),
        )
    )


async def _get_knowledge_base(db: AsyncSession, tenant_id: UUID, kb_id: UUID) -> KnowledgeBase:
    result = await db.execute(
        _knowledge_query(tenant_id)
        .where(KnowledgeBase.id == kb_id)
        .execution_options(populate_existing=True)
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


def _recount(kb: KnowledgeBase) -> None:
    # Legacy pasted-text rows were stored as local_only even though their content
    # is immediately searchable by VAV-native runtimes. Promote them lazily so an
    # existing workspace is repaired the next time it is governed or refreshed.
    for source in kb.sources:
        if (
            source.source_type == "text"
            and source.status == "local_only"
            and has_searchable_content(source)
        ):
            source.status = "indexed"
    kb.source_count = len(kb.sources)
    kb.indexed_source_count = sum(
        source.status == "indexed" and has_searchable_content(source) for source in kb.sources
    )
    if not kb.source_count:
        kb.sync_status = "local_only"
        kb.sync_error = None
    elif kb.indexed_source_count == kb.source_count:
        kb.sync_status = "ready"
        kb.sync_error = None
    elif any(source.status == "failed" for source in kb.sources):
        kb.sync_status = "error"
        kb.sync_error = next(
            (source.error_message for source in kb.sources if source.error_message),
            "One or more sources are not usable for agent retrieval.",
        )
    elif any(source.status in {"pending", "processing"} for source in kb.sources):
        kb.sync_status = "processing"
        kb.sync_error = None
    else:
        # An upstream provider status is not retrieval evidence. This also heals
        # legacy rows that were previously marked ready with an empty content body.
        kb.sync_status = "error"
        kb.sync_error = "One or more sources have no VAV-searchable content."


def _retrieval_signature(kb: KnowledgeBase) -> tuple[tuple[str, str, str, str], ...]:
    """Capture all evidence that can change which source text agents retrieve."""
    return tuple(
        sorted(
            (
                str(source.id),
                source.status,
                canonical_source_url(source.location) or str(source.location or ""),
                str(source.content or "").strip(),
            )
            for source in kb.sources
        )
    )


def _queue_compilation(tenant_id: UUID, kb_id: UUID, source_ids: list[UUID]) -> list[UUID]:
    """Queue local compilation for sources; return the IDs that could not be queued."""
    from app.tasks.knowledge_tasks import compile_knowledge_source

    failed: list[UUID] = []
    for source_id in source_ids:
        try:
            compile_knowledge_source.apply_async(
                args=[str(tenant_id), str(kb_id), str(source_id)],
                queue="knowledge",
            )
        except Exception:
            failed.append(source_id)
    return failed


def _mark_unqueued_sources_failed(kb: KnowledgeBase, source_ids: list[UUID]) -> None:
    for source in kb.sources:
        if source.id in source_ids:
            source.status = "failed"
            source.error_message = (
                "The knowledge worker is temporarily unavailable. Re-index this source."
            )
    _recount(kb)


def _coverage_blockers(kb: KnowledgeBase) -> tuple[list[str], list[str]]:
    """Return (sources blocked outright, sources with partial coverage)."""
    blocked: list[str] = []
    partial: list[str] = []
    for source in kb.sources:
        coverage = (source.source_metadata or {}).get("coverage")
        reason = coverage_blocks_approval(coverage)
        if reason is not None:
            blocked.append(f"{source.name}: {reason}")
        elif isinstance(coverage, dict) and coverage.get("status") == "partial":
            partial.append(source.name)
    return blocked, partial


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        _knowledge_query(current_user.tenant_id).order_by(KnowledgeBase.updated_at.desc())
    )
    return [_knowledge_response(kb) for kb in result.scalars().unique().all()]


@router.post("/ai-draft", response_model=KnowledgeAIDraftResponse)
async def create_knowledge_ai_draft(
    data: KnowledgeAIDraftRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Generate governed metadata for review without creating a knowledge base."""
    await enforce_rate_limit(
        request,
        scope="knowledge-ai-draft",
        limit=10,
        window_seconds=60,
        subject=str(current_user.tenant_id),
        bind_to_client=False,
    )
    try:
        openai_config = await load_provider_config(db, current_user.tenant_id, "openai")
    except ProviderCredentialError as exc:
        raise HTTPException(
            status_code=503, detail="The OpenAI credential is unavailable."
        ) from exc
    api_key = str((openai_config or {}).get("api_key") or settings.openai_api_key).strip()
    if not api_key:
        raise HTTPException(status_code=409, detail="Add an OpenAI API key in Settings first.")

    try:
        generated = await generate_knowledge_ai_draft(
            api_key=api_key,
            brief=data.brief,
            scope_preference=data.scope_preference,
            languages=data.languages,
        )
    except KnowledgeAIWizardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="OpenAI could not generate a knowledge draft right now. Please retry.",
        ) from exc

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.ai_draft_generated",
        resource_type="knowledge_base_draft",
        resource_id=None,
        details={
            "model": generated.model,
            "scope_type": generated.draft.scope_type,
            "languages": generated.draft.languages,
        },
    )
    return generated


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = KnowledgeBase(
        tenant_id=current_user.tenant_id,
        agent_id=None,
        name=data.name,
        description=data.description or None,
        provider=KNOWLEDGE_PROVIDER,
        scope_type=data.scope_type,
        scope_label=data.scope_label,
        languages=data.languages,
        tags=data.tags,
        sources=[],
        agent_bindings=[],
        crawls=[],
    )
    db.add(kb)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.created",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"scope_type": kb.scope_type, "provider": kb.provider},
    )
    # Re-load through the canonical eager query before serialization. Async
    # SQLAlchemy cannot lazy-load a newly created relationship while FastAPI
    # is building the response, even when that relationship is currently empty.
    return _knowledge_response(await _get_knowledge_base(db, current_user.tenant_id, kb.id))


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return _knowledge_response(await _get_knowledge_base(db, current_user.tenant_id, kb_id))


@router.patch("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: UUID,
    data: KnowledgeBaseUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(kb, key, value)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.updated",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"fields": sorted(updates)},
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/sitemap/discover", response_model=SitemapDiscoveryResponse)
async def discover_sitemap(
    kb_id: UUID,
    data: SitemapDiscoveryRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """List the public page URLs declared by one sitemap, without indexing them."""
    await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    try:
        urls = await discover_sitemap_urls(str(data.sitemap_url), limit=MAX_SITEMAP_URLS)
    except WebsiteRecoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not urls:
        raise HTTPException(
            status_code=422,
            detail="The sitemap did not list any public HTTPS pages VAV can index.",
        )
    return SitemapDiscoveryResponse(urls=urls)


ACTIVE_CRAWL_STATUSES = {"queued", "discovering", "indexing", "retrying"}


@router.post(
    "/{kb_id}/crawls",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_website_crawl(
    kb_id: UUID,
    data: KnowledgeCrawlCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Queue a bounded, robots-aware crawl from one public HTTPS homepage."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    if any(crawl.status in ACTIVE_CRAWL_STATUSES for crawl in kb.crawls):
        raise HTTPException(
            status_code=409,
            detail="This knowledge base already has an active website crawl.",
        )
    approval_invalidated = invalidate_knowledge_approval(kb)
    homepage_url = str(data.homepage_url)
    host = (urlsplit(homepage_url).hostname or "").rstrip(".").lower()
    crawl = KnowledgeCrawl(
        tenant_id=current_user.tenant_id,
        knowledge_base_id=kb.id,
        root_url=homepage_url,
        allowed_host=host,
        status="queued",
        max_pages=data.max_pages,
        max_depth=data.max_depth,
        include_subdomains=data.include_subdomains,
        options={
            "respects_robots": True,
            "processing_mode": data.processing_mode,
        },
    )
    kb.crawls.append(crawl)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_crawl.queued",
        resource_type="knowledge_crawl",
        resource_id=str(crawl.id),
        details={
            "homepage_url": homepage_url,
            "max_pages": data.max_pages,
            "max_depth": data.max_depth,
            "include_subdomains": data.include_subdomains,
            "processing_mode": data.processing_mode,
            "approval_invalidated": approval_invalidated,
        },
    )
    await db.commit()

    from app.tasks.knowledge_tasks import crawl_website

    try:
        crawl_website.apply_async(
            args=[str(current_user.tenant_id), str(kb.id), str(crawl.id)],
            queue="knowledge",
        )
    except Exception:
        crawl.status = "failed"
        crawl.error_message = (
            "The website-crawl worker is temporarily unavailable. Retry the crawl."
        )
        crawl.completed_at = datetime.now(UTC)
        await db.commit()
    refreshed = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    return _knowledge_response(refreshed)


@router.post(
    "/{kb_id}/crawls/{crawl_id}/retry",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_website_crawl(
    kb_id: UUID,
    crawl_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Retry discovery or only the failed pages of a completed crawl."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    crawl = next((item for item in kb.crawls if item.id == crawl_id), None)
    if crawl is None:
        raise HTTPException(status_code=404, detail="Website crawl not found")
    if crawl.status in ACTIVE_CRAWL_STATUSES:
        raise HTTPException(status_code=409, detail="The website crawl is already active")

    invalidate_knowledge_approval(kb)
    failed_pages = [page for page in crawl.pages if page.status == "failed"]
    source_by_id = {source.id: source for source in kb.sources}
    queued_sources: list[UUID] = []
    for page in failed_pages:
        source = source_by_id.get(page.knowledge_source_id)
        if source is None:
            continue
        page.status = "queued"
        page.retry_count += 1
        page.error_code = None
        page.error_message = None
        source.status = "processing"
        source.error_message = None
        metadata = dict(source.source_metadata or {})
        metadata["recovery_attempts"] = int(metadata.get("recovery_attempts") or 0) + 1
        source.source_metadata = recovery_metadata(
            metadata,
            stage="queued",
            status="queued",
            message="Failed page queued for another complete recovery attempt.",
        )
        if source.id not in queued_sources:
            queued_sources.append(source.id)

    crawl.error_message = None
    crawl.completed_at = None
    crawl.status = "retrying" if queued_sources else "queued"
    crawl.failed_count = max(0, crawl.failed_count - len(failed_pages))
    crawl.queued_count = len(queued_sources)
    _recount(kb)
    await db.commit()

    from app.tasks.knowledge_tasks import _mark_failed, crawl_website, repair_website_source

    if queued_sources:
        for source_id in queued_sources:
            try:
                repair_website_source.apply_async(
                    args=[str(current_user.tenant_id), str(kb.id), str(source_id)],
                    queue="knowledge",
                )
            except Exception:
                await _mark_failed(
                    current_user.tenant_id,
                    kb.id,
                    source_id,
                    message="The recovery worker is unavailable. Retry this failed page.",
                    code="worker_unavailable",
                )
    elif not crawl.pages:
        crawl_website.apply_async(
            args=[str(current_user.tenant_id), str(kb.id), str(crawl.id)],
            queue="knowledge",
        )
    else:
        crawl.status = "completed"
        crawl.completed_at = datetime.now(UTC)
        await db.commit()
    refreshed = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    return _knowledge_response(refreshed)


@router.post(
    "/{kb_id}/sources/urls",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def add_url_sources(
    kb_id: UUID,
    data: UrlSourceCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Register public pages and queue VAV extraction and compilation for each."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    urls = list(dict.fromkeys(canonical_source_url(str(url)) or str(url) for url in data.urls))
    existing = {
        canonical_source_url(source.location) or source.location
        for source in kb.sources
        if source.location
    }
    new_urls = [url for url in urls if url not in existing]
    if not new_urls:
        raise HTTPException(
            status_code=409, detail="Every selected URL is already in this knowledge base"
        )
    approval_invalidated = invalidate_knowledge_approval(kb)
    queued_sources: list[KnowledgeSource] = []
    for url in new_urls:
        source = KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="url",
            name=url.rsplit("/", 1)[-1] or url,
            location=url,
            status="processing",
            source_metadata=recovery_metadata(
                {"processing_mode": "automatic"},
                stage="queued",
                status="queued",
                message="Queued for VAV download, extraction and compilation.",
            ),
        )
        kb.sources.append(source)
        queued_sources.append(source)
    _recount(kb)
    kb.last_synced_at = datetime.now(UTC)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.urls_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "count": len(new_urls),
            "approval_invalidated": approval_invalidated,
        },
    )
    # The worker must never race the request transaction that records the sources.
    await db.commit()

    from app.tasks.knowledge_tasks import _mark_failed
    from app.tasks.knowledge_tasks import repair_website_source as repair_task

    for source in queued_sources:
        try:
            repair_task.apply_async(
                args=[str(current_user.tenant_id), str(kb.id), str(source.id)],
                queue="knowledge",
            )
        except Exception:
            await _mark_failed(
                current_user.tenant_id,
                kb.id,
                source.id,
                message="The page-extraction worker is temporarily unavailable. Retry this page.",
                code="worker_unavailable",
            )
    refreshed = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    return _knowledge_response(refreshed)


@router.post("/{kb_id}/sources/text", response_model=KnowledgeBaseResponse)
async def add_text_source(
    kb_id: UUID,
    data: TextSourceCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    approval_invalidated = invalidate_knowledge_approval(kb)
    records = records_from_text(data.content)
    rendered = render_records(records) or data.content
    source = KnowledgeSource(
        tenant_id=current_user.tenant_id,
        source_type="text",
        name=data.name,
        raw_content=rendered,
        content=rendered,
        structured_content={"records": records_to_payload(records)},
        size_bytes=len(data.content.encode()),
        status="processing",
        source_metadata={
            "retrieval_content_source": "vav_text",
            "processing_mode": "automatic",
            "record_count": len(records),
        },
    )
    kb.sources.append(source)
    _recount(kb)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.text_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "name": data.name,
            "bytes": len(data.content.encode()),
            "records": len(records),
            "approval_invalidated": approval_invalidated,
        },
    )
    # The worker must never race the request transaction that records the source.
    await db.commit()
    unqueued = _queue_compilation(current_user.tenant_id, kb.id, [source.id])
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    if unqueued:
        _mark_unqueued_sources_failed(kb, unqueued)
        await db.commit()
    return _knowledge_response(kb)


@router.post(
    "/{kb_id}/sources/{source_id}/repair",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def repair_website_source(
    kb_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Queue VAV extraction and re-indexing for one web page."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    if source.source_type not in {"url", "website", "sitemap"} or not source.location:
        raise HTTPException(
            status_code=422,
            detail="Only website URL sources can use automatic page recovery",
        )
    recovery = (
        source.source_metadata.get("recovery") if isinstance(source.source_metadata, dict) else None
    )
    if isinstance(recovery, dict) and recovery.get("status") in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="This website page is already being repaired")

    metadata = dict(source.source_metadata or {})
    staged_refresh = bool(
        kb.approval_status == "approved"
        and source.status == "indexed"
        and has_searchable_content(source)
        and source.content_sha256
    )
    approval_invalidated = False if staged_refresh else invalidate_knowledge_approval(kb)
    attempts = int(metadata.get("recovery_attempts") or 0) + 1
    metadata["recovery_attempts"] = attempts
    metadata["staged_refresh"] = staged_refresh
    if not staged_refresh:
        source.status = "processing"
    source.error_message = None
    source.source_metadata = recovery_metadata(
        metadata,
        stage="queued",
        status="queued",
        message="VAV queued safe download, extraction, compilation and indexing.",
    )
    if not staged_refresh:
        kb.sync_status = "processing"
    kb.sync_error = None
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.website_repair_queued",
        resource_type="knowledge_source",
        resource_id=str(source.id),
        details={
            "attempt": attempts,
            "approval_invalidated": approval_invalidated,
            "staged_refresh": staged_refresh,
        },
    )
    # The worker must never race the request transaction that records the job.
    await db.commit()

    from app.tasks.knowledge_tasks import repair_website_source as repair_task

    try:
        repair_task.apply_async(
            args=[str(current_user.tenant_id), str(kb.id), str(source.id)],
            queue="knowledge",
        )
    except Exception as exc:
        source.status = "failed"
        source.error_message = "The website-recovery worker is temporarily unavailable."
        source.source_metadata = recovery_metadata(
            source.source_metadata,
            stage="failed",
            status="failed",
            message=source.error_message,
        )
        _recount(kb)
        await db.commit()
        raise HTTPException(status_code=503, detail=source.error_message) from exc
    return _knowledge_response(kb)


@router.post("/{kb_id}/sources/pdf", response_model=KnowledgeBaseResponse)
async def upload_pdf_source(
    kb_id: UUID,
    media: UploadFile = File(...),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    approval_invalidated = invalidate_knowledge_approval(kb)
    filename = PurePath(media.filename or "knowledge.pdf").name
    if media.content_type != "application/pdf" or not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF documents are supported")
    content = await media.read(MAX_KNOWLEDGE_PDF_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF must be 8 MB or smaller")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="The uploaded file is not a valid PDF")
    try:
        prepared: PreparedPdf = await asyncio.to_thread(
            prepare_pdf,
            content,
            languages=kb.languages,
        )
    except PdfIngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing_source = next(
        (
            source
            for source in kb.sources
            if source.source_type == "file" and source.name.casefold() == filename.casefold()
        ),
        None,
    )
    source = existing_source
    if source is None:
        source = KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="file",
            name=filename,
        )
        kb.sources.append(source)
    now = datetime.now(UTC)
    source.name = filename
    source.content = prepared.extracted_text
    source.raw_content = prepared.extracted_text
    source.structured_content = {"records": records_to_payload(list(prepared.records))}
    source.file_content = content
    source.mime_type = "application/pdf"
    source.size_bytes = len(content)
    source.status = "processing"
    source.provider_item_id = None
    source.error_message = None
    source.source_metadata = {
        "retrieval_content_source": "vav_pdf_ingestion",
        "processing_mode": "automatic",
        "extraction_method": prepared.extraction_method,
        "page_count": prepared.page_count,
        "ocr_page_count": prepared.ocr_page_count,
        "record_count": len(prepared.records),
        "sha256": prepared.sha256,
    }
    source.last_synced_at = now
    _recount(kb)
    kb.last_synced_at = now
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action=(
            "knowledge_source.pdf_updated" if existing_source else "knowledge_source.pdf_added"
        ),
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "name": filename,
            "bytes": len(content),
            "characters": len(prepared.extracted_text),
            "records": len(prepared.records),
            "extraction_method": prepared.extraction_method,
            "ocr_pages": prepared.ocr_page_count,
            "replaced_existing": bool(existing_source),
            "approval_invalidated": approval_invalidated,
        },
    )
    await db.commit()
    unqueued = _queue_compilation(current_user.tenant_id, kb.id, [source.id])
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    if unqueued:
        _mark_unqueued_sources_failed(kb, unqueued)
        await db.commit()
    return _knowledge_response(kb)


@router.delete("/{kb_id}/sources/{source_id}", response_model=KnowledgeBaseResponse)
async def delete_knowledge_source(
    kb_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Permanently remove a source from VAV knowledge."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    approval_invalidated = invalidate_knowledge_approval(kb)
    kb.sources.remove(source)
    _recount(kb)
    mark_knowledge_bindings_live(kb)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.deleted",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "requested_source_id": str(source_id),
            "removed_source_ids": [str(source_id)],
            "removed_source_names": [source.name],
            "approval_invalidated": approval_invalidated,
        },
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/refresh", response_model=KnowledgeBaseResponse)
async def refresh_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Recount sources and merge canonical URL duplicates without touching content."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    before_signature = _retrieval_signature(kb)
    removed = await consolidate_duplicate_url_sources(db, kb)
    _recount(kb)
    approval_invalidated = False
    if removed or before_signature != _retrieval_signature(kb):
        approval_invalidated = invalidate_knowledge_approval(kb)
    kb.last_synced_at = datetime.now(UTC)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.refreshed",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "source_count": kb.source_count,
            "indexed_source_count": kb.indexed_source_count,
            "removed_source_count": removed,
            "approval_invalidated": approval_invalidated,
        },
    )
    return _knowledge_response(kb)


@router.post(
    "/{kb_id}/reindex",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Re-extract, recompile and re-measure every source with the current pipeline."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    if not kb.sources:
        raise HTTPException(status_code=409, detail="This knowledge base has no sources")
    approval_invalidated = invalidate_knowledge_approval(kb)
    compile_ids: list[UUID] = []
    repair_ids: list[UUID] = []
    for source in kb.sources:
        metadata = dict(source.source_metadata or {})
        metadata.pop("coverage", None)
        metadata.setdefault("processing_mode", "automatic")
        if source.source_type == "file":
            if not source.file_content:
                source.status = "failed"
                source.error_message = "The original PDF is not stored. Upload it again."
                source.source_metadata = metadata
                continue
            try:
                prepared: PreparedPdf = await asyncio.to_thread(
                    prepare_pdf,
                    bytes(source.file_content),
                    languages=kb.languages,
                )
            except PdfIngestionError as exc:
                source.status = "failed"
                source.error_message = str(exc)
                source.source_metadata = metadata
                continue
            source.raw_content = prepared.extracted_text
            source.content = prepared.extracted_text
            source.structured_content = {"records": records_to_payload(list(prepared.records))}
            metadata.update(
                {
                    "extraction_method": prepared.extraction_method,
                    "page_count": prepared.page_count,
                    "ocr_page_count": prepared.ocr_page_count,
                    "record_count": len(prepared.records),
                    "sha256": prepared.sha256,
                }
            )
            source.status = "processing"
            source.error_message = None
            source.source_metadata = metadata
            compile_ids.append(source.id)
        elif source.source_type == "text":
            text = str(source.raw_content or source.content or "")
            records = records_from_text(text)
            rendered = render_records(records) or text
            source.raw_content = rendered
            source.content = rendered
            source.structured_content = {"records": records_to_payload(records)}
            metadata["record_count"] = len(records)
            source.status = "processing"
            source.error_message = None
            source.source_metadata = metadata
            compile_ids.append(source.id)
        elif source.location:
            metadata["force_recompile"] = True
            metadata["recovery_attempts"] = int(metadata.get("recovery_attempts") or 0) + 1
            source.status = "processing"
            source.error_message = None
            source.source_metadata = recovery_metadata(
                metadata,
                stage="queued",
                status="queued",
                message="Queued for re-extraction and recompilation with the current pipeline.",
            )
            repair_ids.append(source.id)
    _recount(kb)
    kb.sync_error = None
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.reindex_queued",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "compile_sources": len(compile_ids),
            "repair_sources": len(repair_ids),
            "approval_invalidated": approval_invalidated,
        },
    )
    await db.commit()

    from app.tasks.knowledge_tasks import _mark_failed
    from app.tasks.knowledge_tasks import repair_website_source as repair_task

    unqueued = _queue_compilation(current_user.tenant_id, kb.id, compile_ids)
    for source_id in repair_ids:
        try:
            repair_task.apply_async(
                args=[str(current_user.tenant_id), str(kb.id), str(source_id)],
                queue="knowledge",
            )
        except Exception:
            await _mark_failed(
                current_user.tenant_id,
                kb.id,
                source_id,
                message="The page-extraction worker is temporarily unavailable. Retry this page.",
                code="worker_unavailable",
            )
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    if unqueued:
        _mark_unqueued_sources_failed(kb, unqueued)
        await db.commit()
    return _knowledge_response(kb)


@router.post("/{kb_id}/approval", response_model=KnowledgeBaseResponse)
async def set_knowledge_approval(
    kb_id: UUID,
    data: KnowledgeApprovalRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _recount(kb)
    partial_sources: list[str] = []
    if data.approved:
        if kb.sync_status != "ready" or not kb.indexed_source_count:
            raise HTTPException(
                status_code=409,
                detail="Make every source VAV-searchable before approving this knowledge base",
            )
        blocked, partial_sources = _coverage_blockers(kb)
        if blocked:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Re-index these sources so VAV can compile and measure them before "
                    "approval: " + "; ".join(blocked[:5])
                ),
            )
        if partial_sources and not data.accept_partial_coverage:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Partial coverage: some records in "
                    + ", ".join(partial_sources[:5])
                    + " were not captured as verified facts. Review the uncovered records, "
                    "or approve with accept_partial_coverage to acknowledge the gap."
                ),
            )
    kb.approval_status = "approved" if data.approved else "draft"
    kb.published_at = datetime.now(UTC) if data.approved else None
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.approval_changed",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "approved": data.approved,
            "accepted_partial_coverage": bool(data.approved and partial_sources),
            "partial_coverage_sources": partial_sources[:20],
        },
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/bindings", response_model=KnowledgeBaseResponse)
async def bind_agent(
    kb_id: UUID,
    data: AgentKnowledgeBindRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    if kb.approval_status != "approved":
        raise HTTPException(status_code=409, detail="Approve this knowledge base first")
    agent = await db.scalar(
        select(Agent).where(Agent.id == data.agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Only VAV-native agents retrieve VAV knowledge. Switch this agent to a "
                "VAV runtime (Inworld, Sarvam or ElevenLabs) before binding knowledge."
            ),
        )
    binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.agent_id == agent.id,
            AgentKnowledgeBinding.tenant_id == current_user.tenant_id,
        )
    )
    if binding:
        binding.knowledge_base_id = kb.id
    else:
        binding = AgentKnowledgeBinding(
            tenant_id=current_user.tenant_id,
            agent_id=agent.id,
            knowledge_base_id=kb.id,
        )
        db.add(binding)
    binding.provider = agent.voice_provider
    binding.sync_status = "synced"
    binding.last_synced_at = datetime.now(UTC)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.agent_bound",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"agent_id": str(agent.id)},
    )
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    return _knowledge_response(kb)


@router.delete("/{kb_id}/bindings/{agent_id}", response_model=KnowledgeBaseResponse)
async def unbind_agent(
    kb_id: UUID,
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    result = await db.execute(
        delete(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.knowledge_base_id == kb.id,
            AgentKnowledgeBinding.agent_id == agent_id,
            AgentKnowledgeBinding.tenant_id == current_user.tenant_id,
        )
    )
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Knowledge binding not found")
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.agent_unbound",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"agent_id": str(agent_id)},
    )
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb.id)
    return _knowledge_response(kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    if kb.agent_bindings:
        raise HTTPException(
            status_code=409, detail="Unbind every agent before deleting this knowledge base"
        )
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.deleted",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"provider": kb.provider},
    )
    await db.delete(kb)
