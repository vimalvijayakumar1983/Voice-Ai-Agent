"""Background repair tasks for website knowledge sources."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import (
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeCrawlPage,
    KnowledgeSource,
)
from app.services.audit import record_audit_event
from app.services.knowledge_compiler import (
    COMPILER_VERSION,
    CompiledKnowledge,
    KnowledgeCompilerError,
    compile_knowledge,
)
from app.services.knowledge_records import (
    KnowledgeRecord,
    coverage_report,
    records_from_payload,
    records_from_text,
    records_to_payload,
    render_records,
)
from app.services.knowledge_sources import (
    canonical_source_url,
    consolidate_duplicate_url_sources,
    has_searchable_content,
    invalidate_knowledge_approval,
    mark_knowledge_bindings_live,
)
from app.services.provider_credentials import ProviderCredentialError, load_provider_config
from app.services.website_crawler import discover_website
from app.services.website_recovery import (
    RecoveredPage,
    WebsiteRecoveryError,
    download_html,
    extract_page_records,
    recovery_metadata,
    render_html,
    should_render_javascript,
)
from app.tasks.async_runner import run_async as _run_async
from app.tasks.worker import celery_app

logger = structlog.get_logger()


def _recount(knowledge_base: KnowledgeBase) -> None:
    for source in knowledge_base.sources:
        if (
            source.source_type == "text"
            and source.status == "local_only"
            and has_searchable_content(source)
        ):
            source.status = "indexed"
    knowledge_base.source_count = len(knowledge_base.sources)
    knowledge_base.indexed_source_count = sum(
        source.status == "indexed" and has_searchable_content(source)
        for source in knowledge_base.sources
    )
    if not knowledge_base.source_count:
        knowledge_base.sync_status = "local_only"
        knowledge_base.sync_error = None
    elif knowledge_base.indexed_source_count == knowledge_base.source_count:
        knowledge_base.sync_status = "ready"
        knowledge_base.sync_error = None
    elif any(source.status == "failed" for source in knowledge_base.sources):
        knowledge_base.sync_status = "error"
        knowledge_base.sync_error = next(
            (source.error_message for source in knowledge_base.sources if source.error_message),
            "One or more website pages need attention.",
        )
    else:
        knowledge_base.sync_status = "processing"


async def _context(tenant_id: UUID, kb_id: UUID, source_id: UUID):
    session = async_session_factory()
    knowledge_base = await session.scalar(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id)
        .options(
            selectinload(KnowledgeBase.sources),
            selectinload(KnowledgeBase.agent_bindings).selectinload(AgentKnowledgeBinding.agent),
        )
    )
    source = (
        next(
            (candidate for candidate in knowledge_base.sources if candidate.id == source_id),
            None,
        )
        if knowledge_base
        else None
    )
    if knowledge_base is None or source is None:
        await session.close()
        raise WebsiteRecoveryError(
            "The website source no longer exists.",
            code="source_missing",
        )
    return session, knowledge_base, source


async def _set_stage(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    stage: str,
    message: str,
) -> None:
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    try:
        metadata = dict(source.source_metadata or {})
        staged_refresh = bool(
            metadata.get("staged_refresh")
            and source.status == "indexed"
            and has_searchable_content(source)
        )
        if not staged_refresh:
            source.status = "processing"
        source.error_message = None
        source.source_metadata = recovery_metadata(
            metadata,
            stage=stage,
            message=message,
        )
        if not staged_refresh:
            knowledge_base.sync_status = "processing"
        knowledge_base.sync_error = None
        if not staged_refresh:
            invalidate_knowledge_approval(knowledge_base)
        await session.commit()
    finally:
        await session.close()
    await _mark_crawl_pages_for_source(
        source_id,
        status="indexed" if staged_refresh else "processing",
    )


async def _refresh_crawl(crawl_id: UUID) -> None:
    session = async_session_factory()
    try:
        crawl = await session.scalar(
            select(KnowledgeCrawl).where(KnowledgeCrawl.id == crawl_id).with_for_update()
        )
        if crawl is None:
            return
        pages = list(
            (
                await session.scalars(
                    select(KnowledgeCrawlPage).where(KnowledgeCrawlPage.crawl_id == crawl.id)
                )
            ).all()
        )
        crawl.discovered_count = len(pages)
        crawl.indexed_count = sum(page.status == "indexed" for page in pages)
        crawl.failed_count = sum(page.status == "failed" for page in pages)
        skipped_pages = sum(page.status == "skipped" for page in pages)
        crawl.queued_count = sum(
            page.status in {"discovered", "queued", "processing"} for page in pages
        )
        options = dict(crawl.options or {})
        previous_non_content = int(options.get("non_content_skipped_count") or 0)
        discovery_skipped = max(0, int(crawl.skipped_count or 0) - previous_non_content)
        options["non_content_skipped_count"] = skipped_pages
        crawl.options = options
        crawl.skipped_count = discovery_skipped + skipped_pages
        terminal = (
            crawl.indexed_count + crawl.failed_count + skipped_pages == crawl.discovered_count
        )
        if terminal and crawl.discovered_count:
            crawl.status = "completed_with_errors" if crawl.failed_count else "completed"
            crawl.completed_at = datetime.now(UTC)
        elif crawl.status not in {"discovering", "failed", "cancelled"}:
            crawl.status = "indexing"
        await session.commit()
    finally:
        await session.close()


async def _mark_crawl_pages_for_source(
    source_id: UUID,
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    session = async_session_factory()
    try:
        pages = list(
            (
                await session.scalars(
                    select(KnowledgeCrawlPage).where(
                        KnowledgeCrawlPage.knowledge_source_id == source_id,
                        KnowledgeCrawlPage.status.in_(
                            {"discovered", "queued", "processing", "failed"}
                        ),
                    )
                )
            ).all()
        )
        crawl_ids: set[UUID] = set()
        now = datetime.now(UTC)
        for page in pages:
            page.status = status
            page.last_attempted_at = now
            page.error_code = error_code
            page.error_message = error_message[:1000] if error_message else None
            crawl_ids.add(page.crawl_id)
        await session.commit()
    finally:
        await session.close()
    for crawl_id in crawl_ids:
        await _refresh_crawl(crawl_id)


def _is_permanent_non_content_failure(*, code: str, message: str) -> bool:
    if code == "no_readable_text":
        return True
    return code == "http_error" and any(marker in message for marker in ("HTTP 404", "HTTP 410"))


async def _mark_non_content_skipped(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    original_code: str,
) -> None:
    """Retain the crawl ledger while removing a source that has no usable knowledge."""
    session = async_session_factory()
    crawl_ids: set[UUID] = set()
    try:
        knowledge_base = await session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id)
            .options(selectinload(KnowledgeBase.sources))
        )
        source = await session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.id == source_id,
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.knowledge_base_id == kb_id,
            )
        )
        if knowledge_base is None or source is None:
            return
        pages = list(
            (
                await session.scalars(
                    select(KnowledgeCrawlPage).where(
                        KnowledgeCrawlPage.knowledge_source_id == source_id
                    )
                )
            ).all()
        )
        now = datetime.now(UTC)
        for page in pages:
            page.status = "skipped"
            page.error_code = "excluded_non_content"
            page.error_message = (
                "Excluded automatically because the page is missing or contains no useful "
                "voice-searchable business content."
            )
            page.last_attempted_at = now
            page.knowledge_source_id = None
            crawl_ids.add(page.crawl_id)
        if source in knowledge_base.sources:
            knowledge_base.sources.remove(source)
        await session.delete(source)
        invalidate_knowledge_approval(knowledge_base)
        _recount(knowledge_base)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="knowledge_crawl.page_excluded",
            resource_type="knowledge_source",
            resource_id=str(source_id),
            details={"reason": original_code, "pages": len(pages)},
        )
        await session.commit()
    finally:
        await session.close()
    for crawl_id in crawl_ids:
        await _refresh_crawl(crawl_id)


async def _crawl_website(tenant_id: UUID, kb_id: UUID, crawl_id: UUID) -> None:
    session = async_session_factory()
    try:
        crawl = await session.scalar(
            select(KnowledgeCrawl).where(
                KnowledgeCrawl.id == crawl_id,
                KnowledgeCrawl.tenant_id == tenant_id,
                KnowledgeCrawl.knowledge_base_id == kb_id,
            )
        )
        if crawl is None:
            return
        crawl.status = "discovering"
        crawl.started_at = crawl.started_at or datetime.now(UTC)
        crawl.error_message = None
        await session.commit()
        root_url = crawl.root_url
        max_pages = crawl.max_pages
        max_depth = crawl.max_depth
        include_subdomains = crawl.include_subdomains
        processing_mode = str((crawl.options or {}).get("processing_mode") or "automatic")
    finally:
        await session.close()

    discovery = await discover_website(
        root_url,
        max_pages=max_pages,
        max_depth=max_depth,
        include_subdomains=include_subdomains,
    )

    session = async_session_factory()
    queued_sources: list[UUID] = []
    try:
        crawl = await session.scalar(
            select(KnowledgeCrawl)
            .where(KnowledgeCrawl.id == crawl_id, KnowledgeCrawl.tenant_id == tenant_id)
            .with_for_update()
        )
        knowledge_base = await session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id)
            .options(
                selectinload(KnowledgeBase.sources),
                selectinload(KnowledgeBase.agent_bindings).selectinload(
                    AgentKnowledgeBinding.agent
                ),
            )
            .with_for_update()
        )
        if crawl is None or knowledge_base is None or crawl.status == "cancelled":
            return
        invalidate_knowledge_approval(knowledge_base)
        await consolidate_duplicate_url_sources(session, knowledge_base)
        existing_by_url = {
            canonical: source
            for source in knowledge_base.sources
            if source.location and (canonical := canonical_source_url(source.location))
        }
        for discovered in discovery.pages:
            source = existing_by_url.get(discovered.canonical_url)
            # A new crawl is also a content refresh. Existing pages are
            # re-extracted; content-addressed provider artifacts below avoid
            # uploading an unchanged page again.
            ready = False
            if source is None:
                source = KnowledgeSource(
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base.id,
                    source_type="website",
                    name=discovered.url.rsplit("/", 1)[-1] or discovery.allowed_host,
                    location=discovered.url,
                    status="processing",
                    source_metadata=recovery_metadata(
                        {
                            "crawl_root": discovery.root_url,
                            "discovered_via": discovered.discovered_via,
                            "crawl_depth": discovered.depth,
                            "processing_mode": processing_mode,
                        },
                        stage="queued",
                        status="queued",
                        message="Discovered automatically and queued for extraction.",
                    ),
                )
                knowledge_base.sources.append(source)
                await session.flush()
                existing_by_url[discovered.canonical_url] = source
            elif not ready:
                metadata = dict(source.source_metadata or {})
                metadata.update(
                    {
                        "crawl_root": discovery.root_url,
                        "discovered_via": discovered.discovered_via,
                        "crawl_depth": discovered.depth,
                        "processing_mode": processing_mode,
                    }
                )
                source.status = "processing"
                source.error_message = None
                source.source_metadata = recovery_metadata(
                    metadata,
                    stage="queued",
                    status="queued",
                    message="Queued by the whole-site crawler for extraction and verification.",
                )
            page = KnowledgeCrawlPage(
                tenant_id=tenant_id,
                crawl_id=crawl.id,
                knowledge_source_id=source.id,
                url=discovered.url,
                canonical_url=discovered.canonical_url,
                depth=discovered.depth,
                discovered_via=discovered.discovered_via,
                status="indexed" if ready else "queued",
            )
            session.add(page)
            if not ready and source.id not in queued_sources:
                queued_sources.append(source.id)

        crawl.root_url = discovery.root_url
        crawl.allowed_host = discovery.allowed_host
        crawl.discovered_count = len(discovery.pages)
        crawl.skipped_count = discovery.skipped_count
        crawl.options = {
            **(crawl.options or {}),
            "warnings": list(discovery.warnings),
            "respects_robots": True,
            "discovery_methods": ["robots", "sitemap", "same_site_links", "javascript_links"],
            "processing_mode": processing_mode,
        }
        crawl.indexed_count = len(discovery.pages) - len(queued_sources)
        crawl.queued_count = len(queued_sources)
        crawl.failed_count = 0
        crawl.status = "indexing" if queued_sources else "completed"
        if not queued_sources:
            crawl.completed_at = datetime.now(UTC)
        _recount(knowledge_base)
        if discovery.pages:
            mark_knowledge_bindings_live(knowledge_base)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="knowledge_crawl.discovered",
            resource_type="knowledge_crawl",
            resource_id=str(crawl.id),
            details={
                "root_url": discovery.root_url,
                "pages": len(discovery.pages),
                "skipped": discovery.skipped_count,
                "queued": len(queued_sources),
            },
        )
        await session.commit()
    finally:
        await session.close()

    for source_id in queued_sources:
        try:
            repair_website_source.apply_async(
                args=[str(tenant_id), str(kb_id), str(source_id)],
                queue="knowledge",
            )
        except Exception:
            await _mark_failed(
                tenant_id,
                kb_id,
                source_id,
                message="The page was discovered, but the recovery worker could not be queued.",
                code="worker_unavailable",
            )


async def _mark_crawl_failed(crawl_id: UUID, message: str) -> None:
    session = async_session_factory()
    try:
        crawl = await session.get(KnowledgeCrawl, crawl_id)
        if crawl is not None:
            crawl.status = "failed"
            crawl.error_message = message[:1000]
            crawl.completed_at = datetime.now(UTC)
            await session.commit()
    finally:
        await session.close()


async def _openai_api_key(session, tenant_id: UUID) -> str:
    """Resolve the compilation key: tenant override first, then the platform key."""
    try:
        openai_config = await load_provider_config(session, tenant_id, "openai")
    except ProviderCredentialError:
        openai_config = None
    return str((openai_config or {}).get("api_key") or settings.openai_api_key).strip()


def _store_compiled(
    source: KnowledgeSource,
    *,
    compiled: CompiledKnowledge,
    records: list[KnowledgeRecord],
    requested_mode: str,
    reused: bool,
) -> dict:
    """Persist compiled knowledge, records and the coverage report on one source."""
    structured = dict(compiled.structured or {})
    structured["records"] = records_to_payload(records)
    coverage = coverage_report(
        records,
        structured,
        requested_mode=requested_mode,
        effective_mode=compiled.effective_mode,
    )
    now = datetime.now(UTC)
    source.content = compiled.content
    source.structured_content = structured
    source.compiled_at = source.compiled_at if reused and source.compiled_at else now
    source.last_synced_at = now
    metadata = dict(source.source_metadata or {})
    metadata.pop("force_recompile", None)
    metadata["coverage"] = coverage
    metadata["record_count"] = len(records)
    metadata["compiler"] = {**(structured.get("compiler") or {}), "reused": reused}
    source.source_metadata = metadata
    return coverage


async def _compile_source(tenant_id: UUID, kb_id: UUID, source_id: UUID) -> None:
    """Compile a PDF or pasted-text source that VAV already extracted."""
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    try:
        metadata = dict(source.source_metadata or {})
        requested_mode = str(metadata.get("processing_mode") or "automatic")
        if requested_mode not in {"automatic", "fast", "ai_verified"}:
            requested_mode = "automatic"
        text = str(source.raw_content or source.content or "")
        records = records_from_payload((source.structured_content or {}).get("records"))
        if not records:
            records = records_from_text(text)
            text = render_records(records) or text
            source.raw_content = text
        source_kind = "pdf" if source.source_type == "file" else "text"
        api_key = await _openai_api_key(session, tenant_id)
    finally:
        await session.close()
    if not text.strip():
        raise KnowledgeCompilerError("The source has no extracted text to compile.")

    compiled = await compile_knowledge(
        title=source.name,
        url=source.location or source.name,
        text=text,
        requested_mode=requested_mode,  # type: ignore[arg-type]
        api_key=api_key or None,
        source_kind=source_kind,
    )

    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    try:
        coverage = _store_compiled(
            source,
            compiled=compiled,
            records=records,
            requested_mode=requested_mode,
            reused=False,
        )
        source.status = "indexed"
        source.error_message = None
        knowledge_base.last_synced_at = datetime.now(UTC)
        _recount(knowledge_base)
        mark_knowledge_bindings_live(knowledge_base)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="knowledge_source.compiled",
            resource_type="knowledge_source",
            resource_id=str(source.id),
            details={
                "source_type": source.source_type,
                "processing_mode": requested_mode,
                "compiler_mode": compiled.effective_mode,
                "compiler_model": compiled.model,
                "compiler_input_tokens": compiled.input_tokens,
                "compiler_output_tokens": compiled.output_tokens,
                "compiler_estimated_cost_usd": round(compiled.estimated_cost_usd, 8),
                "coverage_status": coverage["status"],
                "records_covered": coverage["records_covered"],
                "record_total": coverage["record_total"],
            },
        )
        await session.commit()
    finally:
        await session.close()


async def _mark_compile_failed(
    tenant_id: UUID, kb_id: UUID, source_id: UUID, *, message: str
) -> None:
    try:
        session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    except WebsiteRecoveryError:
        return
    try:
        source.status = "failed"
        source.error_message = message[:1000]
        source.last_synced_at = datetime.now(UTC)
        _recount(knowledge_base)
        await session.commit()
    finally:
        await session.close()


async def _repair(tenant_id: UUID, kb_id: UUID, source_id: UUID) -> None:
    await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "fetching",
        "Downloading the approved public page and retrying temporary failures.",
    )
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    location = str(source.location or "")
    source_metadata = dict(source.source_metadata or {})
    requested_mode = str(source_metadata.get("processing_mode") or "automatic")
    if requested_mode not in {"automatic", "fast", "ai_verified"}:
        requested_mode = "automatic"
    existing_content_sha256 = source.content_sha256
    existing_compiled_content = source.content
    existing_structured_content = source.structured_content
    staged_refresh = bool(source_metadata.get("staged_refresh"))
    force_recompile = bool(source_metadata.get("force_recompile"))
    openai_api_key = await _openai_api_key(session, tenant_id)
    await session.close()
    if not location:
        raise WebsiteRecoveryError("The source has no website URL.", code="invalid_source")

    final_url, static_html, downloaded_bytes = await download_html(location)
    try:
        title, records = extract_page_records(static_html, url=final_url)
        text = render_records(records)
        static_page = RecoveredPage(final_url, title, text, "static_html", downloaded_bytes)
        if should_render_javascript(static_html, text):
            await _set_stage(
                tenant_id,
                kb_id,
                source_id,
                "rendering",
                "The page appears JavaScript-driven. VAV is rendering the complete content.",
            )
            try:
                rendered_html, rendered_bytes = await render_html(final_url)
                title, records = extract_page_records(rendered_html, url=final_url)
                text = render_records(records)
                page = RecoveredPage(
                    final_url,
                    title,
                    text,
                    "javascript_render",
                    rendered_bytes,
                )
            except WebsiteRecoveryError:
                page = static_page
        else:
            page = static_page
    except WebsiteRecoveryError as exc:
        if exc.code != "no_readable_text":
            raise
        await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "rendering",
            "The raw page had no usable text. VAV is rendering its JavaScript content.",
        )
        rendered_html, rendered_bytes = await render_html(final_url)
        title, records = extract_page_records(rendered_html, url=final_url)
        text = render_records(records)
        page = RecoveredPage(final_url, title, text, "javascript_render", rendered_bytes)
    await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "extracting",
        f"Extracted {len(page.text):,} readable characters using {page.method}.",
    )
    raw_content_sha256 = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
    if staged_refresh and existing_content_sha256 != raw_content_sha256:
        # Keep the last approved version live while downloading. Only move the
        # knowledge base back through review after an actual content change is
        # proven, never merely because an operator clicked Refresh.
        session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
        try:
            metadata = dict(source.source_metadata or {})
            metadata["staged_refresh"] = False
            source.source_metadata = metadata
            source.status = "processing"
            knowledge_base.sync_status = "processing"
            invalidate_knowledge_approval(knowledge_base)
            await session.commit()
        finally:
            await session.close()
        staged_refresh = False
    reused_compilation = bool(
        not force_recompile
        and existing_content_sha256 == raw_content_sha256
        and existing_compiled_content
        and existing_structured_content
        and (existing_structured_content.get("compiler") or {}).get("version") == COMPILER_VERSION
        and (existing_structured_content.get("compiler") or {}).get("requested_mode")
        == requested_mode
        and not (existing_structured_content.get("compiler") or {}).get("warning")
    )
    if reused_compilation:
        compiler = existing_structured_content.get("compiler") or {}
        compiled = CompiledKnowledge(
            content=existing_compiled_content,
            structured=existing_structured_content,
            effective_mode=str(compiler.get("effective_mode") or "fast"),
            model=str(compiler.get("model")) if compiler.get("model") else None,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            warning=None,
        )
    else:
        await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "compiling",
            "Structuring extracted knowledge and verifying every AI fact against its source.",
        )
        compiled = await compile_knowledge(
            title=page.title,
            url=page.url,
            text=page.text,
            requested_mode=requested_mode,
            api_key=openai_api_key or None,
            source_kind="website",
        )
    await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "indexing",
        "Storing the compiled knowledge for VAV retrieval.",
    )
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    try:
        content_sha256 = hashlib.sha256(compiled.content.encode("utf-8")).hexdigest()
        source.name = page.title[:255]
        source.location = page.url
        source.raw_content = page.text
        source.content_sha256 = raw_content_sha256
        source.mime_type = "text/html"
        source.size_bytes = page.downloaded_bytes
        source.status = "indexed"
        source.provider_item_id = None
        source.error_message = None
        coverage = _store_compiled(
            source,
            compiled=compiled,
            records=records,
            requested_mode=requested_mode,
            reused=reused_compilation,
        )
        metadata = dict(source.source_metadata or {})
        metadata.pop("staged_refresh", None)
        metadata.pop("provider_artifact_name", None)
        metadata.update(
            {
                "extraction_method": page.method,
                "content_sha256": content_sha256,
                "retrieval_content_source": "vav_website_recovery",
            }
        )
        source.source_metadata = recovery_metadata(
            metadata,
            stage="verified",
            status="completed",
            message=(
                "Readable text was extracted, compiled and indexed for agent retrieval."
                if coverage["status"] in {"complete", "skipped"}
                else "Indexed, but some records were not captured as facts; review coverage."
            ),
            method=page.method,
            extracted_characters=len(page.text),
        )
        now = datetime.now(UTC)
        knowledge_base.last_synced_at = now

        await consolidate_duplicate_url_sources(
            session,
            knowledge_base,
            preferred_source=source,
        )
        _recount(knowledge_base)
        mark_knowledge_bindings_live(knowledge_base)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="knowledge_source.website_repaired",
            resource_type="knowledge_source",
            resource_id=str(source.id),
            details={
                "method": page.method,
                "characters": len(page.text),
                "processing_mode": requested_mode,
                "compiler_mode": compiled.effective_mode,
                "compiler_model": compiled.model,
                "compiler_input_tokens": compiled.input_tokens,
                "compiler_output_tokens": compiled.output_tokens,
                "compiler_estimated_cost_usd": round(compiled.estimated_cost_usd, 8),
                "compilation_reused": reused_compilation,
                "coverage_status": coverage["status"],
                "records_covered": coverage["records_covered"],
                "record_total": coverage["record_total"],
            },
        )
        await session.commit()
    finally:
        await session.close()

    await _mark_crawl_pages_for_source(source_id, status="indexed")


async def _mark_failed(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    message: str,
    code: str,
) -> None:
    if _is_permanent_non_content_failure(code=code, message=message):
        await _mark_non_content_skipped(
            tenant_id,
            kb_id,
            source_id,
            original_code=code,
        )
        return
    try:
        session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    except WebsiteRecoveryError:
        return
    try:
        metadata = dict(source.source_metadata or {})
        preserve_previous = bool(
            metadata.pop("staged_refresh", False)
            and source.status == "indexed"
            and has_searchable_content(source)
        )
        source.status = "indexed" if preserve_previous else "failed"
        source.error_message = None if preserve_previous else message[:1000]
        metadata["recovery_error_code"] = code
        source.source_metadata = recovery_metadata(
            metadata,
            stage="failed",
            status="failed",
            message=(
                f"Latest refresh failed; the previous approved content remains active. {message}"
                if preserve_previous
                else message
            ),
        )
        source.last_synced_at = datetime.now(UTC)
        _recount(knowledge_base)
        await session.commit()
    finally:
        await session.close()
    await _mark_crawl_pages_for_source(
        source_id,
        status="indexed" if preserve_previous else "failed",
        error_code=code,
        error_message=message,
    )


@celery_app.task(
    name="app.tasks.knowledge_tasks.crawl_website",
    bind=True,
    max_retries=2,
)
def crawl_website(self, tenant_id: str, knowledge_base_id: str, crawl_id: str):
    tenant_uuid = UUID(tenant_id)
    knowledge_uuid = UUID(knowledge_base_id)
    crawl_uuid = UUID(crawl_id)
    try:
        _run_async(_crawl_website(tenant_uuid, knowledge_uuid, crawl_uuid))
    except WebsiteRecoveryError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=10 * (self.request.retries + 1))
        _run_async(_mark_crawl_failed(crawl_uuid, str(exc)))
        logger.warning(
            "knowledge_site_crawl_failed",
            knowledge_base_id=knowledge_base_id,
            crawl_id=crawl_id,
            error_code=exc.code,
        )
    except Exception:
        logger.exception(
            "knowledge_site_crawl_unexpected_failure",
            knowledge_base_id=knowledge_base_id,
            crawl_id=crawl_id,
        )
        _run_async(
            _mark_crawl_failed(
                crawl_uuid,
                "VAV could not complete website discovery. Retry the site crawl.",
            )
        )


@celery_app.task(
    name="app.tasks.knowledge_tasks.repair_website_source",
    bind=True,
    max_retries=2,
)
def repair_website_source(self, tenant_id: str, knowledge_base_id: str, source_id: str):
    tenant_uuid = UUID(tenant_id)
    knowledge_uuid = UUID(knowledge_base_id)
    source_uuid = UUID(source_id)
    try:
        _run_async(_repair(tenant_uuid, knowledge_uuid, source_uuid))
    except KnowledgeCompilerError as exc:
        _run_async(
            _mark_failed(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                message=str(exc),
                code="knowledge_compilation_failed",
            )
        )
    except WebsiteRecoveryError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            _run_async(
                _set_stage(
                    tenant_uuid,
                    knowledge_uuid,
                    source_uuid,
                    "queued",
                    "A temporary problem occurred. VAV scheduled an automatic retry.",
                )
            )
            raise self.retry(exc=exc, countdown=5 * (self.request.retries + 1))
        _run_async(
            _mark_failed(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                message=str(exc),
                code=exc.code,
            )
        )
        logger.warning(
            "knowledge_website_repair_failed",
            knowledge_base_id=knowledge_base_id,
            source_id=source_id,
            error_code=exc.code,
        )
    except Exception:
        logger.exception(
            "knowledge_website_repair_unexpected_failure",
            knowledge_base_id=knowledge_base_id,
            source_id=source_id,
        )
        _run_async(
            _mark_failed(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                message="VAV could not complete website recovery. Retry the page.",
                code="unexpected_failure",
            )
        )


@celery_app.task(
    name="app.tasks.knowledge_tasks.compile_knowledge_source",
    bind=True,
    max_retries=2,
)
def compile_knowledge_source(self, tenant_id: str, knowledge_base_id: str, source_id: str):
    tenant_uuid = UUID(tenant_id)
    knowledge_uuid = UUID(knowledge_base_id)
    source_uuid = UUID(source_id)
    try:
        _run_async(_compile_source(tenant_uuid, knowledge_uuid, source_uuid))
    except KnowledgeCompilerError as exc:
        _run_async(_mark_compile_failed(tenant_uuid, knowledge_uuid, source_uuid, message=str(exc)))
    except WebsiteRecoveryError:
        # The source or knowledge base was deleted while queued.
        return
    except Exception:
        logger.exception(
            "knowledge_source_compile_unexpected_failure",
            knowledge_base_id=knowledge_base_id,
            source_id=source_id,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=5 * (self.request.retries + 1))
        _run_async(
            _mark_compile_failed(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                message="VAV could not compile this source. Re-index it to retry.",
            )
        )
