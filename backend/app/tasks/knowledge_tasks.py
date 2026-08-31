"""Background repair tasks for website knowledge sources."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import async_session_factory
from app.models.agent import (
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeCrawlPage,
    KnowledgeSource,
)
from app.providers.smallest import SmallestAIClient, SmallestAIError, get_smallest_client
from app.services.audit import record_audit_event
from app.services.provider_credentials import load_provider_config
from app.services.website_crawler import canonicalize_page_url, discover_website
from app.services.website_recovery import (
    RecoveredPage,
    WebsiteRecoveryError,
    download_html,
    extract_readable_text,
    recovery_metadata,
    render_html,
    searchable_pdf,
    should_render_javascript,
)
from app.tasks.async_runner import run_async as _run_async
from app.tasks.worker import celery_app

logger = structlog.get_logger()
PROVIDER_READY = {"complete", "completed", "indexed", "processed", "ready", "success", "succeeded"}
PROVIDER_FAILED = {"error", "failed", "failure"}


def _provider_item_id(value: dict | None) -> str | None:
    current: object = value
    for _ in range(4):
        if not isinstance(current, dict):
            return None
        item_id = current.get("_id") or current.get("id")
        if item_id:
            return str(item_id)
        current = current.get("data") or current.get("item")
    return None


def _provider_file_name(item: dict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return str(item.get("fileName") or metadata.get("fileName") or "")


def _provider_status(item: dict) -> str:
    value = str(item.get("processingStatus") or item.get("status") or "processing")
    return value.strip().lower().replace("-", "_").replace(" ", "_")


async def _wait_for_provider_index(
    provider: SmallestAIClient,
    *,
    knowledge_base_id: str,
    provider_item_id: str | None,
    artifact_name: str,
    excluded_item_ids: set[str] | None = None,
) -> str:
    """Do not report success until the provider item itself is indexed."""
    for attempt in range(30):
        items = await provider.list_knowledge_items(knowledge_base_id)
        excluded = excluded_item_ids or set()
        item = next(
            (
                candidate
                for candidate in items
                if (
                    str(candidate.get("_id") or candidate.get("id") or "") == provider_item_id
                    or (
                        _provider_file_name(candidate) == artifact_name
                        and str(candidate.get("_id") or candidate.get("id") or "") not in excluded
                    )
                )
            ),
            None,
        )
        if item is not None:
            provider_state = _provider_status(item)
            if provider_state in PROVIDER_READY:
                indexed_item_id = _provider_item_id(item)
                if indexed_item_id:
                    return indexed_item_id
                raise WebsiteRecoveryError(
                    "The provider indexed the recovered page but returned no item identifier.",
                    code="provider_response_invalid",
                    retryable=True,
                )
            if provider_state in PROVIDER_FAILED:
                raise WebsiteRecoveryError(
                    "The provider rejected the recovered searchable document.",
                    code="provider_indexing_failed",
                )
        if attempt < 29:
            await _provider_poll_wait(2)
    raise WebsiteRecoveryError(
        "The recovered page is still waiting for provider indexing.",
        code="provider_indexing_timeout",
        retryable=True,
    )


async def _provider_poll_wait(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _recount(knowledge_base: KnowledgeBase) -> None:
    knowledge_base.source_count = len(knowledge_base.sources)
    knowledge_base.indexed_source_count = sum(
        source.status == "indexed" and bool(str(source.content or "").strip())
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
        source.status = "processing"
        source.error_message = None
        source.source_metadata = recovery_metadata(
            source.source_metadata,
            stage=stage,
            message=message,
        )
        knowledge_base.sync_status = "processing"
        knowledge_base.sync_error = None
        await session.commit()
    finally:
        await session.close()
    await _mark_crawl_pages_for_source(source_id, status="processing")


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


def _invalidate_crawl_bindings(knowledge_base: KnowledgeBase) -> None:
    now = datetime.now(UTC)
    for binding in knowledge_base.agent_bindings:
        agent = binding.agent
        if getattr(agent, "voice_provider", "smallest") in {"sarvam", "elevenlabs"}:
            binding.provider = agent.voice_provider
            binding.sync_status = "synced"
            binding.last_synced_at = now
            continue
        binding.sync_status = "pending"
        binding.last_synced_at = None
        if agent.provider_agent_id and agent.sync_status != "error":
            agent.sync_status = "dirty"


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
        existing_by_url = {
            canonical: source
            for source in knowledge_base.sources
            if source.location and (canonical := canonicalize_page_url(source.location))
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
        }
        crawl.indexed_count = len(discovery.pages) - len(queued_sources)
        crawl.queued_count = len(queued_sources)
        crawl.failed_count = 0
        crawl.status = "indexing" if queued_sources else "completed"
        if not queued_sources:
            crawl.completed_at = datetime.now(UTC)
        _recount(knowledge_base)
        if discovery.pages:
            _invalidate_crawl_bindings(knowledge_base)
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


async def _tenant_client(session, tenant_id: UUID) -> SmallestAIClient:
    config = await load_provider_config(session, tenant_id, "smallest")
    api_key = str((config or {}).get("api_key") or "").strip()
    return SmallestAIClient(api_key=api_key) if api_key else get_smallest_client()


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
    await session.close()
    if not location:
        raise WebsiteRecoveryError("The source has no website URL.", code="invalid_source")

    final_url, static_html, downloaded_bytes = await download_html(location)
    try:
        title, text = extract_readable_text(static_html, url=final_url)
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
                title, text = extract_readable_text(rendered_html, url=final_url)
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
        title, text = extract_readable_text(rendered_html, url=final_url)
        page = RecoveredPage(final_url, title, text, "javascript_render", rendered_bytes)
    await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "extracting",
        f"Extracted {len(page.text):,} readable characters using {page.method}.",
    )
    provider_document = await asyncio.to_thread(
        searchable_pdf,
        title=page.title,
        url=page.url,
        text=page.text,
    )

    await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "provider_indexing",
        "Sending the recovered searchable document to the knowledge provider.",
    )
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    try:
        provider = await _tenant_client(session, tenant_id)
        remote_id = knowledge_base.provider_knowledge_base_id
        if not remote_id:
            remote_id = await provider.create_knowledge_base(
                name=knowledge_base.name,
                description=knowledge_base.description or "",
            )
            knowledge_base.provider_knowledge_base_id = remote_id
            await session.commit()

        content_sha256 = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
        artifact_prefix = f"vav-web-recovery-{source.id}-"
        legacy_artifact_name = f"vav-web-recovery-{source.id}.pdf"
        artifact_name = f"{artifact_prefix}{content_sha256[:16]}.pdf"
        existing_items = await provider.list_knowledge_items(remote_id)
        reusable_item = next(
            (
                item
                for item in existing_items
                if _provider_file_name(item) == artifact_name
                and _provider_status(item) in PROVIDER_READY
                and _provider_item_id(item)
            ),
            None,
        )
        excluded_item_ids: set[str] = set()
        if reusable_item is not None:
            provider_item_id = _provider_item_id(reusable_item)
        else:
            upload = await provider.upload_knowledge_pdf(
                knowledge_base_id=remote_id,
                file_name=artifact_name,
                content=provider_document,
            )
            provider_item_id = _provider_item_id(upload)
            if not provider_item_id:
                excluded_item_ids = {
                    item_id
                    for item in existing_items
                    if (item_id := _provider_item_id(item)) is not None
                }
        await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "verifying",
            "The searchable document was accepted. VAV is verifying provider indexing.",
        )
        provider_item_id = await _wait_for_provider_index(
            provider,
            knowledge_base_id=remote_id,
            provider_item_id=provider_item_id,
            artifact_name=artifact_name,
            excluded_item_ids=excluded_item_ids,
        )

        source.name = page.title[:255]
        source.location = page.url
        source.content = page.text
        source.mime_type = "text/html"
        source.size_bytes = page.downloaded_bytes
        source.status = "indexed"
        source.provider_item_id = provider_item_id
        source.error_message = None
        metadata = dict(source.source_metadata or {})
        metadata.update(
            {
                "extraction_method": page.method,
                "content_sha256": content_sha256,
                "provider_artifact_name": artifact_name,
                "retrieval_content_source": "vav_website_recovery",
            }
        )
        source.source_metadata = recovery_metadata(
            metadata,
            stage="verified",
            status="completed",
            message="Readable text was extracted, indexed and verified for agent retrieval.",
            method=page.method,
            extracted_characters=len(page.text),
        )
        now = datetime.now(UTC)
        source.last_synced_at = now
        knowledge_base.last_synced_at = now

        affected_agent_ids: list[str] = []
        for binding in knowledge_base.agent_bindings:
            agent = binding.agent
            if agent.voice_provider in {"sarvam", "elevenlabs"}:
                binding.provider = agent.voice_provider
                binding.sync_status = "synced"
                binding.last_synced_at = now
                continue
            binding.sync_status = "pending"
            binding.last_synced_at = None
            if agent.provider_agent_id and agent.sync_status != "error":
                agent.sync_status = "dirty"
                affected_agent_ids.append(str(agent.id))

        _recount(knowledge_base)
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
                "agents_requiring_sync": affected_agent_ids,
            },
        )
        await session.commit()

        await _mark_crawl_pages_for_source(source_id, status="indexed")

        for item in existing_items:
            old_id = str(item.get("_id") or item.get("id") or "")
            if (
                old_id
                and old_id != provider_item_id
                and (
                    _provider_file_name(item).startswith(artifact_prefix)
                    or _provider_file_name(item) == legacy_artifact_name
                )
            ):
                try:
                    await provider.delete_knowledge_item(
                        knowledge_base_id=remote_id,
                        item_id=old_id,
                    )
                except SmallestAIError:
                    logger.warning(
                        "knowledge_repair_stale_artifact_cleanup_failed",
                        knowledge_base_id=str(kb_id),
                        source_id=str(source_id),
                    )
    finally:
        await session.close()


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
        source.status = "failed"
        source.error_message = message[:1000]
        metadata = dict(source.source_metadata or {})
        metadata["recovery_error_code"] = code
        source.source_metadata = recovery_metadata(
            metadata,
            stage="failed",
            status="failed",
            message=message,
        )
        source.last_synced_at = datetime.now(UTC)
        _recount(knowledge_base)
        await session.commit()
    finally:
        await session.close()
    await _mark_crawl_pages_for_source(
        source_id,
        status="failed",
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
    except SmallestAIError as exc:
        retryable = (exc.upstream_status_code or exc.status_code) >= 500 or (
            exc.upstream_status_code == 429
        )
        if retryable and self.request.retries < self.max_retries and not exc.ambiguous:
            raise self.retry(exc=exc, countdown=5 * (self.request.retries + 1))
        _run_async(
            _mark_failed(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                message=f"Provider indexing failed: {exc}",
                code="provider_indexing_failed",
            )
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
