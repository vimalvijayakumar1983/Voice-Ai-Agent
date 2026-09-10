"""Background repair tasks for website knowledge sources."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

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
    compile_website_knowledge,
)
from app.services.knowledge_records import (
    KnowledgeRecord,
    coverage_report,
    records_to_payload,
    render_records,
)
from app.services.knowledge_sources import (
    canonical_source_url,
    consolidate_duplicate_url_sources,
    has_searchable_content,
    invalidate_knowledge_approval,
    mark_native_bindings_live,
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
COMPILED_SERVING_SIGNATURE_KEY = "compiled_serving_signature_v1"
REPAIR_RUN_ID_KEY = "repair_run_id"
REPAIR_STALE_AFTER = timedelta(minutes=15)


def _compiled_serving_signature(*, content: str | None, structured: dict | None) -> str:
    """Hash only the compiled representation that can affect agent answers.

    Compiler cost, token and warning diagnostics deliberately do not participate:
    recomputing those values must not create a false pending knowledge release.
    The compiler contract itself does participate, so a version/mode change is
    reviewable even when the downloaded raw page is byte-for-byte identical.
    """
    source = dict(structured or {})
    compiler = source.pop("compiler", None)
    # Validation counts describe the compiler run, not knowledge served to an
    # agent. The source-grounded facts/entities they summarize remain included.
    source.pop("validation", None)
    # Extraction records feed the coverage report; the compiled facts they were
    # measured against are what agents receive, so records do not participate.
    source.pop("records", None)
    compiler = compiler if isinstance(compiler, dict) else {}
    payload = {
        "schema": "vav.compiled-serving-signature.v1",
        "content": str(content or ""),
        "structured": source,
        "compiler": {
            "version": str(compiler.get("version") or ""),
            "requested_mode": str(compiler.get("requested_mode") or ""),
            "effective_mode": str(compiler.get("effective_mode") or ""),
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _queue_repair_metadata(
    metadata: dict | None,
    *,
    staged_refresh: bool,
    message: str,
) -> tuple[dict, str, int]:
    """Stamp one immutable generation token before a repair is dispatched."""
    updated = dict(metadata or {})
    attempts = int(updated.get("recovery_attempts") or 0) + 1
    # Keep the fencing generation monotonic even for records created while an
    # older deployment tracked attempts and generations independently.
    generation = (
        max(
            int(updated.get("repair_generation") or 0),
            int(updated.get("recovery_attempts") or 0),
        )
        + 1
    )
    run_id = str(uuid4())
    updated.update(
        {
            "recovery_attempts": attempts,
            "repair_generation": generation,
            REPAIR_RUN_ID_KEY: run_id,
            "staged_refresh": staged_refresh,
        }
    )
    return (
        recovery_metadata(
            updated,
            stage="queued",
            status="queued",
            message=message,
        ),
        run_id,
        attempts,
    )


def _repair_run_is_current(source: KnowledgeSource, repair_run_id: str | None) -> bool:
    current = str((source.source_metadata or {}).get(REPAIR_RUN_ID_KEY) or "")
    if repair_run_id is None:
        # Backwards compatibility for tasks already queued during deployment:
        # they may finish only if no newer generation token has been issued.
        return not current
    return current == repair_run_id


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


async def _context(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    for_update: bool = False,
    session=None,
):
    session = session or async_session_factory()
    knowledge_query = (
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id)
        .options(
            selectinload(KnowledgeBase.sources),
            selectinload(KnowledgeBase.agent_bindings).selectinload(AgentKnowledgeBinding.agent),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        knowledge_query = knowledge_query.with_for_update()
    knowledge_base = await session.scalar(knowledge_query)
    if knowledge_base is None:
        await session.close()
        raise WebsiteRecoveryError(
            "The website source no longer exists.",
            code="source_missing",
        )
    if for_update:
        # Global mutation order: knowledge base -> bindings -> sources/calls.
        # Lock every row in deterministic order because final recounting and
        # binding synchronization operate on the complete loaded collections.
        list(
            (
                await session.scalars(
                    select(AgentKnowledgeBinding)
                    .where(
                        AgentKnowledgeBinding.tenant_id == tenant_id,
                        AgentKnowledgeBinding.knowledge_base_id == kb_id,
                    )
                    .options(selectinload(AgentKnowledgeBinding.agent))
                    .order_by(AgentKnowledgeBinding.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        locked_sources = list(
            (
                await session.scalars(
                    select(KnowledgeSource)
                    .where(
                        KnowledgeSource.tenant_id == tenant_id,
                        KnowledgeSource.knowledge_base_id == kb_id,
                    )
                    .order_by(KnowledgeSource.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        source = next((item for item in locked_sources if item.id == source_id), None)
    else:
        source_query = (
            select(KnowledgeSource)
            .where(
                KnowledgeSource.id == source_id,
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.knowledge_base_id == kb_id,
            )
            .execution_options(populate_existing=True)
        )
        source = await session.scalar(source_query)
    if source is None:
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
    *,
    repair_run_id: str | None = None,
) -> bool:
    session, knowledge_base, source = await _context(
        tenant_id,
        kb_id,
        source_id,
        for_update=True,
    )
    try:
        if not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
            return False

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
        repair_run_id=repair_run_id,
    )
    return True


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
    repair_run_id: str | None = None,
) -> None:
    session = async_session_factory()
    try:
        source = await session.scalar(
            select(KnowledgeSource)
            .where(KnowledgeSource.id == source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source is None or not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
            return
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
    repair_run_id: str | None = None,
) -> None:
    """Retain the crawl ledger while removing a source that has no usable knowledge."""
    session = async_session_factory()
    crawl_ids: set[UUID] = set()
    try:
        knowledge_base = await session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id)
            .options(selectinload(KnowledgeBase.sources))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        source = await session.scalar(
            select(KnowledgeSource)
            .where(
                KnowledgeSource.id == source_id,
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.knowledge_base_id == kb_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if knowledge_base is None or source is None:
            return
        if not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
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


def _invalidate_crawl_bindings(knowledge_base: KnowledgeBase) -> None:
    mark_native_bindings_live(knowledge_base)


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
    queued_sources: dict[UUID, str] = {}
    try:
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
            .execution_options(populate_existing=True)
        )
        if knowledge_base is None:
            return
        # This transaction mutates binding sync state and source generations.
        # Follow the global order used by call admission and repair commits:
        # knowledge base -> bindings -> sources -> crawl ledger.
        list(
            (
                await session.scalars(
                    select(AgentKnowledgeBinding)
                    .where(
                        AgentKnowledgeBinding.tenant_id == tenant_id,
                        AgentKnowledgeBinding.knowledge_base_id == kb_id,
                    )
                    .options(selectinload(AgentKnowledgeBinding.agent))
                    .order_by(AgentKnowledgeBinding.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        list(
            (
                await session.scalars(
                    select(KnowledgeSource)
                    .where(
                        KnowledgeSource.tenant_id == tenant_id,
                        KnowledgeSource.knowledge_base_id == kb_id,
                    )
                    .order_by(KnowledgeSource.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        crawl = await session.scalar(
            select(KnowledgeCrawl)
            .where(KnowledgeCrawl.id == crawl_id, KnowledgeCrawl.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
                queued_metadata, repair_run_id, _ = _queue_repair_metadata(
                    {
                        "crawl_root": discovery.root_url,
                        "discovered_via": discovered.discovered_via,
                        "crawl_depth": discovered.depth,
                        "processing_mode": processing_mode,
                    },
                    staged_refresh=False,
                    message="Discovered automatically and queued for extraction.",
                )
                source = KnowledgeSource(
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base.id,
                    source_type="website",
                    name=discovered.url.rsplit("/", 1)[-1] or discovery.allowed_host,
                    location=discovered.url,
                    status="processing",
                    source_metadata=queued_metadata,
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
                queued_metadata, repair_run_id, _ = _queue_repair_metadata(
                    metadata,
                    staged_refresh=False,
                    message="Queued by the whole-site crawler for extraction and verification.",
                )
                source.status = "processing"
                source.error_message = None
                source.source_metadata = queued_metadata
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
            if not ready:
                queued_sources[source.id] = repair_run_id

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

    for source_id, repair_run_id in queued_sources.items():
        try:
            repair_website_source.apply_async(
                args=[str(tenant_id), str(kb_id), str(source_id), repair_run_id],
                queue="knowledge",
            )
        except Exception:
            await _mark_failed(
                tenant_id,
                kb_id,
                source_id,
                message="The page was discovered, but the recovery worker could not be queued.",
                code="worker_unavailable",
                repair_run_id=repair_run_id,
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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _supersede_stale_repair(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    cutoff: datetime,
) -> str | None:
    """Fence a lost execution with a new repair generation under the KB lock."""
    session, knowledge_base, source = await _context(
        tenant_id,
        kb_id,
        source_id,
        for_update=True,
    )
    try:
        recovery = (
            source.source_metadata.get("recovery")
            if isinstance(source.source_metadata, dict)
            else None
        )
        if not isinstance(recovery, dict) or recovery.get("status") not in {
            "queued",
            "processing",
        }:
            await session.rollback()
            return None
        updated_at = _as_utc(source.updated_at)
        if updated_at is None or updated_at > cutoff:
            await session.rollback()
            return None
        metadata = dict(source.source_metadata or {})
        staged_refresh = bool(
            metadata.get("staged_refresh")
            and source.status == "indexed"
            and has_searchable_content(source)
        )
        queued_metadata, new_run_id, _attempts = _queue_repair_metadata(
            metadata,
            staged_refresh=staged_refresh,
            message=(
                "The previous worker lease expired. VAV fenced it and queued a new "
                "repair generation automatically."
            ),
        )
        source.source_metadata = queued_metadata
        source.error_message = None
        if not staged_refresh:
            source.status = "processing"
            knowledge_base.sync_status = "processing"
            invalidate_knowledge_approval(knowledge_base)
        knowledge_base.sync_error = None
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="knowledge_source.repair_recovered",
            resource_type="knowledge_base",
            resource_id=str(knowledge_base.id),
            details={
                "source_id": str(source.id),
                "previous_repair_run_id": metadata.get(REPAIR_RUN_ID_KEY),
                "repair_run_id": new_run_id,
                "repair_generation": queued_metadata.get("repair_generation"),
            },
        )
        await session.commit()
        return new_run_id
    finally:
        await session.close()


async def _sweep_stale_knowledge_repairs(limit: int = 500) -> int:
    """Requeue lost repairs using a new generation, never the stale run ID."""
    cutoff = datetime.now(UTC) - REPAIR_STALE_AFTER
    session = async_session_factory()
    try:
        candidates = list(
            (
                await session.execute(
                    select(
                        KnowledgeSource.tenant_id,
                        KnowledgeSource.knowledge_base_id,
                        KnowledgeSource.id,
                    )
                    .where(
                        KnowledgeSource.source_metadata["recovery"]["status"]
                        .as_string()
                        .in_(("queued", "processing")),
                        KnowledgeSource.updated_at <= cutoff,
                    )
                    .order_by(KnowledgeSource.updated_at, KnowledgeSource.id)
                    .limit(limit)
                )
            ).all()
        )
    finally:
        await session.close()

    requeued = 0
    for tenant_id, kb_id, source_id in candidates:
        new_run_id = await _supersede_stale_repair(
            tenant_id,
            kb_id,
            source_id,
            cutoff=cutoff,
        )
        if new_run_id is None:
            continue
        try:
            repair_website_source.apply_async(
                args=[str(tenant_id), str(kb_id), str(source_id), new_run_id],
                queue="knowledge",
            )
        except Exception:
            await _mark_failed(
                tenant_id,
                kb_id,
                source_id,
                message="The stale repair was recovered, but its replacement could not be queued.",
                code="worker_unavailable",
                repair_run_id=new_run_id,
            )
            continue
        requeued += 1
    return requeued


@celery_app.task(name="app.tasks.knowledge_tasks.sweep_stale_knowledge_repairs")
def sweep_stale_knowledge_repairs():
    """Recover repairs left behind by abrupt worker or broker loss."""
    return _run_async(_sweep_stale_knowledge_repairs())


async def _commit_repair_success(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None,
    page: RecoveredPage,
    compiled: CompiledKnowledge,
    raw_content_sha256: str,
    compiled_content_sha256: str,
    requested_mode: str,
    reused_compilation: bool,
    records: Sequence[KnowledgeRecord] = (),
) -> bool:
    """Atomically publish a repair result only for its current generation."""
    session, knowledge_base, source = await _context(
        tenant_id,
        kb_id,
        source_id,
        for_update=True,
    )
    committed = False
    try:
        if not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
            return False

        metadata = dict(source.source_metadata or {})
        previous_serving_signature = _compiled_serving_signature(
            content=source.content,
            structured=source.structured_content,
        )
        compiled_structured = {**compiled.structured, "records": records_to_payload(records)}
        coverage = coverage_report(
            records,
            compiled.structured,
            requested_mode=requested_mode,
            effective_mode=compiled.effective_mode,
        )
        next_serving_signature = _compiled_serving_signature(
            content=compiled.content,
            structured=compiled_structured,
        )
        staged_refresh = bool(metadata.get("staged_refresh"))
        serving_content_changed = previous_serving_signature != next_serving_signature
        approval_invalidated = False
        if staged_refresh and serving_content_changed:
            # Keep the immutable serving pointer live. Only the mutable draft
            # moves back through review, and only after a successful compile.
            approval_invalidated = invalidate_knowledge_approval(knowledge_base)

        source.name = page.title[:255]
        source.location = page.url
        source.raw_content = page.text
        source.content = compiled.content
        source.structured_content = compiled_structured
        source.content_sha256 = raw_content_sha256
        source.mime_type = "text/html"
        source.size_bytes = page.downloaded_bytes
        source.status = "indexed"
        source.provider_item_id = None
        source.error_message = None
        metadata.pop("staged_refresh", None)
        metadata.pop("provider_artifact_name", None)
        metadata.pop("provider_cleanup_pending_ids", None)
        metadata.update(
            {
                "extraction_method": page.method,
                "content_sha256": compiled_content_sha256,
                COMPILED_SERVING_SIGNATURE_KEY: next_serving_signature,
                "retrieval_content_source": "vav_website_recovery",
                "compiler": {
                    **(compiled.structured.get("compiler") or {}),
                    "reused": reused_compilation,
                },
                "coverage": coverage,
                "record_count": len(records),
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
        source.compiled_at = source.compiled_at if reused_compilation else now
        source.last_synced_at = now
        knowledge_base.last_synced_at = now
        mark_native_bindings_live(knowledge_base)
        _recount(knowledge_base)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="knowledge_source.website_repaired",
            resource_type="knowledge_source",
            resource_id=str(source.id),
            details={
                "repair_run_id": repair_run_id,
                "repair_generation": metadata.get("repair_generation"),
                "method": page.method,
                "characters": len(page.text),
                "processing_mode": requested_mode,
                "compiler_mode": compiled.effective_mode,
                "compiler_model": compiled.model,
                "compiler_input_tokens": compiled.input_tokens,
                "compiler_output_tokens": compiled.output_tokens,
                "compiler_estimated_cost_usd": round(compiled.estimated_cost_usd, 8),
                "compilation_reused": reused_compilation,
                "compiled_serving_signature_before": previous_serving_signature,
                "compiled_serving_signature_after": next_serving_signature,
                "serving_content_changed": serving_content_changed,
                "approval_invalidated": approval_invalidated,
                "serving_revision_retained": bool(knowledge_base.serving_revision_id),
                "coverage_status": coverage["status"],
                "records_covered": coverage["records_covered"],
                "record_total": coverage["record_total"],
            },
        )
        await session.commit()
        committed = True
    finally:
        await session.close()

    if committed:
        await _mark_crawl_pages_for_source(
            source_id,
            status="indexed",
            repair_run_id=repair_run_id,
        )
    return committed


async def _repair(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None = None,
) -> None:
    if not await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "fetching",
        "Downloading the approved public page and retrying temporary failures.",
        repair_run_id=repair_run_id,
    ):
        return
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    if not _repair_run_is_current(source, repair_run_id):
        await session.close()
        return
    location = str(source.location or "")
    source_metadata = dict(source.source_metadata or {})
    requested_mode = str(source_metadata.get("processing_mode") or "automatic")
    if requested_mode not in {"automatic", "fast", "ai_verified"}:
        requested_mode = "automatic"
    existing_content_sha256 = source.content_sha256
    existing_compiled_content = source.content
    existing_structured_content = source.structured_content
    try:
        openai_config = await load_provider_config(session, tenant_id, "openai")
    except ProviderCredentialError:
        openai_config = None
    openai_api_key = str((openai_config or {}).get("api_key") or settings.openai_api_key).strip()
    await session.close()
    if not location:
        raise WebsiteRecoveryError("The source has no website URL.", code="invalid_source")

    final_url, static_html, downloaded_bytes = await download_html(location)
    try:
        title, records = extract_page_records(static_html, url=final_url)
        text = render_records(records)
        static_page = RecoveredPage(final_url, title, text, "static_html", downloaded_bytes)
        if should_render_javascript(static_html, text):
            if not await _set_stage(
                tenant_id,
                kb_id,
                source_id,
                "rendering",
                "The page appears JavaScript-driven. VAV is rendering the complete content.",
                repair_run_id=repair_run_id,
            ):
                return
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
        if not await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "rendering",
            "The raw page had no usable text. VAV is rendering its JavaScript content.",
            repair_run_id=repair_run_id,
        ):
            return
        rendered_html, rendered_bytes = await render_html(final_url)
        title, records = extract_page_records(rendered_html, url=final_url)
        text = render_records(records)
        page = RecoveredPage(final_url, title, text, "javascript_render", rendered_bytes)
    if not await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "extracting",
        f"Extracted {len(page.text):,} readable characters using {page.method}.",
        repair_run_id=repair_run_id,
    ):
        return
    raw_content_sha256 = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
    reused_compilation = bool(
        existing_content_sha256 == raw_content_sha256
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
        if not await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "compiling",
            "Structuring extracted knowledge and verifying every AI fact against its source.",
            repair_run_id=repair_run_id,
        ):
            return
        compiled = await compile_website_knowledge(
            title=page.title,
            url=page.url,
            text=page.text,
            requested_mode=requested_mode,
            api_key=openai_api_key or None,
        )
    content_sha256 = hashlib.sha256(compiled.content.encode("utf-8")).hexdigest()
    if not await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "indexing",
        "Storing the compiled knowledge for VAV retrieval.",
        repair_run_id=repair_run_id,
    ):
        return
    await _commit_repair_success(
        tenant_id,
        kb_id,
        source_id,
        repair_run_id=repair_run_id,
        page=page,
        compiled=compiled,
        raw_content_sha256=raw_content_sha256,
        compiled_content_sha256=content_sha256,
        requested_mode=requested_mode,
        reused_compilation=reused_compilation,
        records=records,
    )


async def _mark_failed(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    message: str,
    code: str,
    repair_run_id: str | None = None,
) -> None:
    try:
        session, knowledge_base, source = await _context(
            tenant_id,
            kb_id,
            source_id,
            for_update=True,
        )
    except WebsiteRecoveryError:
        return
    skip_non_content = False
    try:
        if not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
            return
        metadata = dict(source.source_metadata or {})
        preserve_previous = bool(
            metadata.pop("staged_refresh", False)
            and source.status == "indexed"
            and has_searchable_content(source)
        )
        if _is_permanent_non_content_failure(code=code, message=message) and not preserve_previous:
            # Release the complete KB/source lock set before the exclusion
            # helper reacquires its own transaction. Its generation fence
            # prevents a newer repair from being removed in the gap.
            skip_non_content = True
            await session.commit()
        else:
            source.status = "indexed" if preserve_previous else "failed"
            source.error_message = None if preserve_previous else message[:1000]
            metadata["recovery_error_code"] = code
            source.source_metadata = recovery_metadata(
                metadata,
                stage="failed",
                status="failed",
                message=(
                    "Latest refresh failed; the previous approved content remains active. "
                    + message
                    if preserve_previous
                    else message
                ),
            )
            source.last_synced_at = datetime.now(UTC)
            _recount(knowledge_base)
            await session.commit()
    finally:
        await session.close()
    if skip_non_content:
        await _mark_non_content_skipped(
            tenant_id,
            kb_id,
            source_id,
            original_code=code,
            repair_run_id=repair_run_id,
        )
        return
    await _mark_crawl_pages_for_source(
        source_id,
        status="indexed" if preserve_previous else "failed",
        error_code=code,
        error_message=message,
        repair_run_id=repair_run_id,
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
    reject_on_worker_lost=True,
)
def repair_website_source(
    self,
    tenant_id: str,
    knowledge_base_id: str,
    source_id: str,
    repair_run_id: str | None = None,
):
    tenant_uuid = UUID(tenant_id)
    knowledge_uuid = UUID(knowledge_base_id)
    source_uuid = UUID(source_id)
    try:
        _run_async(
            _repair(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                repair_run_id=repair_run_id,
            )
        )
    except KnowledgeCompilerError as exc:
        _run_async(
            _mark_failed(
                tenant_uuid,
                knowledge_uuid,
                source_uuid,
                message=str(exc),
                code="knowledge_compilation_failed",
                repair_run_id=repair_run_id,
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
                    repair_run_id=repair_run_id,
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
                repair_run_id=repair_run_id,
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
                repair_run_id=repair_run_id,
            )
        )
