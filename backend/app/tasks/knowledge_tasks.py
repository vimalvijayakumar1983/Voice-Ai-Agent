"""Background repair tasks for website knowledge sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import (
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeCrawl,
    KnowledgeCrawlPage,
    KnowledgeProviderCleanup,
    KnowledgeSource,
)
from app.providers.smallest import SmallestAIClient, SmallestAIError, get_smallest_client
from app.services.audit import record_audit_event
from app.services.knowledge_compiler import (
    COMPILER_VERSION,
    CompiledKnowledge,
    KnowledgeCompilerError,
    compile_website_knowledge,
)
from app.services.knowledge_sources import (
    VAV_NATIVE_KNOWLEDGE_PROVIDERS,
    canonical_source_url,
    consolidate_duplicate_url_sources,
    consolidate_smallest_url_duplicates,
    has_searchable_content,
    invalidate_knowledge_approval,
    mark_remote_creation_outcome_unknown,
    remote_creation_outcome_unknown,
)
from app.services.provider_credentials import (
    ProviderCredentialError,
    load_provider_config,
    lock_provider_cleanup_boundary,
)
from app.services.website_crawler import discover_website
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
COMPILED_SERVING_SIGNATURE_KEY = "compiled_serving_signature_v1"
REPAIR_RUN_ID_KEY = "repair_run_id"
PROVIDER_CLEANUP_PENDING_KEY = "provider_cleanup_pending_ids"
PROVIDER_CLEANUP_LEASE = timedelta(minutes=2)
PROVIDER_UPLOAD_DISCOVERY_GRACE = timedelta(minutes=15)
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
    now = datetime.now(UTC)
    for binding in knowledge_base.agent_bindings:
        agent = binding.agent
        if getattr(agent, "voice_provider", "smallest") in VAV_NATIVE_KNOWLEDGE_PROVIDERS:
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
        if knowledge_base.provider_knowledge_base_id:
            provider = await _tenant_client(session, tenant_id)
            await consolidate_smallest_url_duplicates(session, knowledge_base, provider)
        else:
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


async def _tenant_client(session, tenant_id: UUID) -> SmallestAIClient:
    config = await load_provider_config(session, tenant_id, "smallest")
    api_key = str((config or {}).get("api_key") or "").strip()
    return SmallestAIClient(api_key=api_key) if api_key else get_smallest_client()


async def _ensure_remote_locked(
    session,
    knowledge_base: KnowledgeBase,
    provider: SmallestAIClient,
) -> str:
    """Create one provider KB across all concurrent page-repair workers."""
    locked = await session.scalar(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.id == knowledge_base.id,
            KnowledgeBase.tenant_id == knowledge_base.tenant_id,
        )
        .options(
            selectinload(KnowledgeBase.sources),
            selectinload(KnowledgeBase.agent_bindings).selectinload(AgentKnowledgeBinding.agent),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise WebsiteRecoveryError(
            "The knowledge base no longer exists.",
            code="knowledge_base_missing",
        )
    if locked.provider_knowledge_base_id:
        remote_id = locked.provider_knowledge_base_id
        await session.commit()
        return remote_id
    if remote_creation_outcome_unknown(locked):
        await session.commit()
        raise WebsiteRecoveryError(
            "Provider knowledge-base creation has an unresolved outcome; automatic creation is "
            "paused to prevent duplicates.",
            code="provider_provision_unknown",
        )

    locked.sync_status = "provisioning"
    locked.sync_error = None
    await session.flush()
    try:
        remote_id = await provider.create_knowledge_base(
            name=locked.name,
            description=locked.description or "",
        )
    except SmallestAIError as exc:
        if exc.ambiguous:
            mark_remote_creation_outcome_unknown(locked)
        else:
            locked.sync_status = "error"
            locked.sync_error = str(exc)
        locked.last_synced_at = datetime.now(UTC)
        await session.commit()
        raise
    locked.provider_knowledge_base_id = remote_id
    locked.sync_status = "processing" if locked.source_count else "local_only"
    locked.last_synced_at = datetime.now(UTC)
    await session.commit()
    return remote_id


async def _provider_cleanup_candidate_is_safe(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None,
    provider_item_id: str,
) -> bool:
    """Check a cleanup candidate without holding a lock across provider I/O."""
    session = async_session_factory()
    try:
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
        if source is None or not _repair_run_is_current(source, repair_run_id):
            return False
        metadata = dict(source.source_metadata or {})
        pending = {str(item) for item in metadata.get(PROVIDER_CLEANUP_PENDING_KEY) or [] if item}
        return provider_item_id in pending and source.provider_item_id != provider_item_id
    finally:
        await session.rollback()
        await session.close()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _remove_pending_cleanup_metadata(source: KnowledgeSource, provider_item_id: str) -> None:
    metadata = dict(source.source_metadata or {})
    pending = {
        str(item)
        for item in metadata.get(PROVIDER_CLEANUP_PENDING_KEY) or []
        if item and str(item) != provider_item_id
    }
    if pending:
        metadata[PROVIDER_CLEANUP_PENDING_KEY] = sorted(pending)
    else:
        metadata.pop(PROVIDER_CLEANUP_PENDING_KEY, None)
    source.source_metadata = metadata


async def _ensure_provider_cleanup_records(
    session,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID | None,
    knowledge_source_id: UUID | None,
    repair_run_id: str | None,
    provider_name: str,
    provider_knowledge_base_id: str,
    provider_item_ids: tuple[str, ...],
) -> dict[str, UUID]:
    """Insert cleanup identities inside the caller's transaction.

    Every runtime insertion holds the knowledge-base lock first.  This makes a
    cleanup row and the provider bind/publish guard one serializable boundary.
    """
    normalized_ids = tuple(sorted({str(item) for item in provider_item_ids if item}))
    if not normalized_ids:
        return {}
    await lock_provider_cleanup_boundary(session, tenant_id, provider_name)
    existing = list(
        (
            await session.scalars(
                select(KnowledgeProviderCleanup).where(
                    KnowledgeProviderCleanup.tenant_id == tenant_id,
                    KnowledgeProviderCleanup.provider == provider_name,
                    KnowledgeProviderCleanup.provider_knowledge_base_id
                    == provider_knowledge_base_id,
                    KnowledgeProviderCleanup.provider_item_id.in_(normalized_ids),
                )
            )
        ).all()
    )
    by_item = {record.provider_item_id: record for record in existing}
    now = datetime.now(UTC)
    for item_id in normalized_ids:
        record = by_item.get(item_id)
        if record is None:
            record = KnowledgeProviderCleanup(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_source_id=knowledge_source_id,
                repair_run_id=repair_run_id,
                provider=provider_name,
                provider_knowledge_base_id=provider_knowledge_base_id,
                provider_item_id=item_id,
                status="pending",
                attempts=0,
                available_at=now,
            )
            session.add(record)
            by_item[item_id] = record
            continue
        # Reattach a legacy/orphan row to local context when it is known again;
        # never reset its retry/lease state while another worker may own it.
        if record.knowledge_base_id is None:
            record.knowledge_base_id = knowledge_base_id
        if record.knowledge_source_id is None:
            record.knowledge_source_id = knowledge_source_id
        if record.repair_run_id is None:
            record.repair_run_id = repair_run_id
    await session.flush()
    return {item_id: by_item[item_id].id for item_id in normalized_ids}


async def _persist_uncommitted_provider_cleanup(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None,
    remote_id: str,
    provider_item_id: str,
) -> UUID | None:
    """Durably reserve an orphan before attempting any remote deletion."""
    session = async_session_factory()
    try:
        await lock_provider_cleanup_boundary(session, tenant_id, "smallest")
        # Coordinate with bind/publish by taking the KB boundary first.  The KB
        # may already have been removed; the nullable FK deliberately lets the
        # cleanup survive and finish in that case.
        knowledge_base = await session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        source = None
        if knowledge_base is not None:
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
        if source is not None and source.provider_item_id == provider_item_id:
            await session.rollback()
            return None
        records = await _ensure_provider_cleanup_records(
            session,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base.id if knowledge_base is not None else None,
            knowledge_source_id=source.id if source is not None else None,
            repair_run_id=repair_run_id,
            provider_name="smallest",
            provider_knowledge_base_id=remote_id,
            provider_item_ids=(provider_item_id,),
        )
        await session.commit()
        return records[provider_item_id]
    finally:
        await session.close()


async def _reserve_provider_upload_artifact(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None,
    remote_id: str,
    artifact_name: str,
) -> UUID:
    """Commit crash compensation before a repair uploads provider bytes."""
    session = async_session_factory()
    try:
        await lock_provider_cleanup_boundary(session, tenant_id, "smallest")
        session, knowledge_base, source = await _context(
            tenant_id,
            kb_id,
            source_id,
            for_update=True,
            session=session,
        )
        if not _repair_run_is_current(source, repair_run_id):
            raise WebsiteRecoveryError(
                "This repair generation was superseded before provider upload.",
                code="repair_superseded",
            )
        if knowledge_base.provider_knowledge_base_id != remote_id:
            raise WebsiteRecoveryError(
                "The provider knowledge-base identity changed before upload.",
                code="provider_identity_changed",
            )
        live_provider_agents = [
            binding.agent.name
            for binding in knowledge_base.agent_bindings
            if binding.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS
        ]
        if live_provider_agents:
            raise WebsiteRecoveryError(
                "VAV did not upload this draft because the provider collection is live: "
                + ", ".join(sorted(live_provider_agents)),
                code="provider_blue_green_required",
            )
        unfinished_cleanup = await session.scalar(
            select(KnowledgeProviderCleanup.id).where(
                KnowledgeProviderCleanup.tenant_id == tenant_id,
                KnowledgeProviderCleanup.knowledge_base_id == kb_id,
                KnowledgeProviderCleanup.status != "completed",
            )
        )
        if unfinished_cleanup is not None:
            raise WebsiteRecoveryError(
                "Provider cleanup is still pending; VAV postponed the replacement upload.",
                code="provider_cleanup_pending",
                retryable=True,
            )
        now = datetime.now(UTC)
        reservation = KnowledgeProviderCleanup(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            knowledge_source_id=source_id,
            repair_run_id=repair_run_id,
            provider="smallest",
            provider_knowledge_base_id=remote_id,
            provider_item_id=f"pending-upload:{uuid4()}",
            provider_artifact_name=artifact_name,
            status="processing",
            attempts=0,
            available_at=now,
            lease_expires_at=now + PROVIDER_UPLOAD_DISCOVERY_GRACE,
        )
        session.add(reservation)
        await session.commit()
        return reservation.id
    finally:
        await session.close()


async def _abandon_provider_upload_reservation(
    cleanup_id: UUID,
    *,
    provider: SmallestAIClient,
    message: str,
) -> None:
    """Release a candidate reservation to the durable cleanup worker."""
    session = async_session_factory()
    try:
        discovered = await session.get(KnowledgeProviderCleanup, cleanup_id)
        if discovered is None:
            return
        await lock_provider_cleanup_boundary(
            session,
            discovered.tenant_id,
            discovered.provider,
        )
        _knowledge_base, reservation = await _lock_cleanup_after_knowledge_base(
            session,
            cleanup_id,
        )
        if reservation is None:
            return
        reservation.status = "pending"
        reservation.available_at = datetime.now(UTC)
        reservation.lease_expires_at = None
        reservation.last_error = message[:1000]
        await session.commit()
    finally:
        await session.close()
    await _process_provider_cleanup(cleanup_id, provider=provider)


async def _lock_cleanup_after_knowledge_base(session, cleanup_id: UUID):
    """Lock a cleanup row without introducing an outbox -> KB lock inversion."""
    discovered = await session.scalar(
        select(KnowledgeProviderCleanup).where(KnowledgeProviderCleanup.id == cleanup_id)
    )
    if discovered is None:
        return None, None
    knowledge_base = None
    if discovered.knowledge_base_id is not None:
        knowledge_base = await session.scalar(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.id == discovered.knowledge_base_id,
                KnowledgeBase.tenant_id == discovered.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    record = await session.scalar(
        select(KnowledgeProviderCleanup)
        .where(KnowledgeProviderCleanup.id == cleanup_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return knowledge_base, record


async def _process_provider_cleanup(
    cleanup_id: UUID,
    *,
    provider: SmallestAIClient | None = None,
) -> str:
    """Delete one remote artifact with a durable lease and idempotent retry."""
    session = async_session_factory()
    provider_client = provider
    try:
        # Credential mutation takes this same boundary before looking for
        # unfinished rows. Claiming first makes that empty-check race-free.
        discovered = await session.scalar(
            select(KnowledgeProviderCleanup).where(KnowledgeProviderCleanup.id == cleanup_id)
        )
        if discovered is None:
            return "missing"
        await lock_provider_cleanup_boundary(
            session,
            discovered.tenant_id,
            discovered.provider,
        )
        knowledge_base, record = await _lock_cleanup_after_knowledge_base(session, cleanup_id)
        if record is None:
            return "missing"
        now = datetime.now(UTC)
        available_at = _as_utc(record.available_at) or now
        lease_expires_at = _as_utc(record.lease_expires_at)
        if record.status == "processing" and lease_expires_at and lease_expires_at > now:
            await session.rollback()
            return "leased"
        if available_at > now:
            await session.rollback()
            return "deferred"

        current_source = None
        if knowledge_base is not None:
            current_source = await session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.tenant_id == record.tenant_id,
                    KnowledgeSource.knowledge_base_id == knowledge_base.id,
                    KnowledgeSource.provider_item_id == record.provider_item_id,
                )
                .order_by(KnowledgeSource.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if current_source is not None:
            _remove_pending_cleanup_metadata(current_source, record.provider_item_id)
            await session.delete(record)
            await session.commit()
            return "retained_current"

        if provider_client is None:
            provider_client = await _tenant_client(session, record.tenant_id)
        record.status = "processing"
        record.attempts += 1
        record.lease_expires_at = now + PROVIDER_CLEANUP_LEASE
        record.last_error = None
        tenant_id = record.tenant_id
        provider_knowledge_base_id = record.provider_knowledge_base_id
        provider_item_id = record.provider_item_id
        provider_artifact_name = record.provider_artifact_name
        cleanup_created_at = _as_utc(record.created_at) or now
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning(
            "knowledge_provider_cleanup_claim_failed",
            cleanup_id=str(cleanup_id),
            error_type=type(exc).__name__,
        )
        return "pending"
    finally:
        await session.close()

    try:
        if provider_item_id.startswith("pending-upload:") and provider_artifact_name:
            items = await provider_client.list_knowledge_items(provider_knowledge_base_id)
            matching_item_ids = {
                item_id
                for item in items
                if _provider_file_name(item) == provider_artifact_name
                and (item_id := _provider_item_id(item)) is not None
            }
            if (
                not matching_item_ids
                and datetime.now(UTC) < cleanup_created_at + PROVIDER_UPLOAD_DISCOVERY_GRACE
            ):
                # A successful provider upload can be absent from its listing
                # briefly. Treat "not found" as inconclusive during the grace
                # window so a crash-compensation row cannot clear just before
                # an eventually-consistent orphan becomes visible.
                raise RuntimeError("Provider upload is not visible for cleanup yet")
            for matching_item_id in matching_item_ids:
                await provider_client.delete_knowledge_item(
                    knowledge_base_id=provider_knowledge_base_id,
                    item_id=matching_item_id,
                )
        else:
            await provider_client.delete_knowledge_item(
                knowledge_base_id=provider_knowledge_base_id,
                item_id=provider_item_id,
            )
    except Exception as exc:
        retry_session = async_session_factory()
        try:
            await lock_provider_cleanup_boundary(retry_session, tenant_id, "smallest")
            _knowledge_base, retry_record = await _lock_cleanup_after_knowledge_base(
                retry_session,
                cleanup_id,
            )
            if retry_record is not None:
                retry_record.status = "pending"
                retry_record.lease_expires_at = None
                retry_record.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                retry_record.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(30 * (2 ** min(retry_record.attempts - 1, 5)), 15 * 60)
                )
                await retry_session.commit()
        finally:
            await retry_session.close()
        logger.warning(
            "knowledge_provider_cleanup_failed",
            cleanup_id=str(cleanup_id),
            provider_item_id=provider_item_id,
            error_type=type(exc).__name__,
        )
        return "pending"

    complete_session = async_session_factory()
    try:
        await lock_provider_cleanup_boundary(complete_session, tenant_id, "smallest")
        knowledge_base, complete_record = await _lock_cleanup_after_knowledge_base(
            complete_session,
            cleanup_id,
        )
        if complete_record is None:
            return "completed"
        # Final commits also hold the KB lock and reject cleanup-reserved item
        # IDs. This defensive recheck makes a violated invariant fail closed.
        current_source = None
        if knowledge_base is not None:
            current_source = await complete_session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.tenant_id == tenant_id,
                    KnowledgeSource.knowledge_base_id == knowledge_base.id,
                    KnowledgeSource.provider_item_id == provider_item_id,
                )
                .order_by(KnowledgeSource.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if current_source is not None:
            complete_record.status = "pending"
            complete_record.lease_expires_at = None
            complete_record.available_at = datetime.now(UTC) + timedelta(minutes=15)
            complete_record.last_error = (
                "Cleanup invariant violation: the deleted provider item became current"
            )
            current_source.status = "failed"
            current_source.error_message = complete_record.last_error
            if knowledge_base is not None:
                knowledge_base.sync_status = "error"
                knowledge_base.sync_error = complete_record.last_error
            await complete_session.commit()
            return "invariant_violation"

        if complete_record.knowledge_source_id is not None:
            cleanup_source = await complete_session.scalar(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.id == complete_record.knowledge_source_id,
                    KnowledgeSource.tenant_id == tenant_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if cleanup_source is not None:
                _remove_pending_cleanup_metadata(cleanup_source, provider_item_id)
        await complete_session.delete(complete_record)
        await complete_session.commit()
        return "completed"
    finally:
        await complete_session.close()


async def _cleanup_provider_artifacts(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None,
    provider: SmallestAIClient,
    remote_id: str,
    provider_item_ids: tuple[str, ...],
) -> None:
    """Retire transactionally-outboxed artifacts without blocking KB admission."""
    for item_id in provider_item_ids:
        if not await _provider_cleanup_candidate_is_safe(
            tenant_id,
            kb_id,
            source_id,
            repair_run_id=repair_run_id,
            provider_item_id=item_id,
        ):
            continue
        session = async_session_factory()
        try:
            cleanup_id = await session.scalar(
                select(KnowledgeProviderCleanup.id).where(
                    KnowledgeProviderCleanup.tenant_id == tenant_id,
                    KnowledgeProviderCleanup.provider == "smallest",
                    KnowledgeProviderCleanup.provider_knowledge_base_id == remote_id,
                    KnowledgeProviderCleanup.provider_item_id == item_id,
                )
            )
        finally:
            await session.close()
        if cleanup_id is not None:
            await _process_provider_cleanup(cleanup_id, provider=provider)


async def _cleanup_uncommitted_provider_artifact(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    provider: SmallestAIClient,
    remote_id: str,
    provider_item_id: str,
    repair_run_id: str | None = None,
) -> None:
    """Outbox then delete a run-unique upload that never became current."""
    cleanup_id = await _persist_uncommitted_provider_cleanup(
        tenant_id,
        kb_id,
        source_id,
        repair_run_id=repair_run_id,
        remote_id=remote_id,
        provider_item_id=provider_item_id,
    )
    if cleanup_id is not None:
        await _process_provider_cleanup(cleanup_id, provider=provider)


@celery_app.task(name="app.tasks.knowledge_tasks.cleanup_provider_artifact")
def cleanup_provider_artifact(cleanup_id: str):
    """Retry one durable provider cleanup identity."""
    try:
        return _run_async(_process_provider_cleanup(UUID(cleanup_id)))
    except (TypeError, ValueError):
        return "invalid_identity"


@celery_app.task(name="app.tasks.knowledge_tasks.sweep_provider_cleanup_outbox")
def sweep_provider_cleanup_outbox():
    """Recover cleanup work after worker, broker, or process interruption."""
    return _run_async(_sweep_provider_cleanup_outbox())


async def _sweep_provider_cleanup_outbox(limit: int = 500) -> int:
    now = datetime.now(UTC)
    session = async_session_factory()
    try:
        cleanup_ids = list(
            (
                await session.scalars(
                    select(KnowledgeProviderCleanup.id)
                    .where(
                        or_(
                            (KnowledgeProviderCleanup.status == "pending")
                            & (KnowledgeProviderCleanup.available_at <= now),
                            (KnowledgeProviderCleanup.status == "processing")
                            & (
                                KnowledgeProviderCleanup.lease_expires_at.is_(None)
                                | (KnowledgeProviderCleanup.lease_expires_at <= now)
                            ),
                        )
                    )
                    .order_by(KnowledgeProviderCleanup.available_at, KnowledgeProviderCleanup.id)
                    .limit(limit)
                )
            ).all()
        )
    finally:
        await session.close()
    completed = 0
    for cleanup_id in cleanup_ids:
        if await _process_provider_cleanup(cleanup_id) == "completed":
            completed += 1
    return completed


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
        provider_agents = [
            binding.agent.name
            for binding in knowledge_base.agent_bindings
            if binding.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS
        ]
        if provider_agents:
            # Do not let legacy/stale work upload into a provider collection
            # that became live after the original job was admitted.
            source.source_metadata = recovery_metadata(
                source.source_metadata,
                stage="failed",
                status="failed",
                message=(
                    "Automatic recovery paused because a Smallest.ai agent is now bound: "
                    + ", ".join(sorted(provider_agents))
                ),
            )
            await session.commit()
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


async def _ensure_provider_refresh_isolated(
    tenant_id: UUID,
    kb_id: UUID,
    source_id: UUID,
    *,
    repair_run_id: str | None,
) -> bool:
    """Refuse an in-place provider upload while a Smallest agent is bound."""
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
        provider_agents = sorted(
            {
                binding.agent.name
                for binding in knowledge_base.agent_bindings
                if binding.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS
            }
        )
        if provider_agents:
            await session.rollback()
            raise WebsiteRecoveryError(
                "VAV did not upload this draft because the live Smallest.ai knowledge "
                "collection is still bound to: " + ", ".join(provider_agents),
                code="provider_blue_green_required",
            )
        await session.rollback()
        return True
    finally:
        await session.close()


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
    provider_item_id: str,
    artifact_name: str,
    requested_mode: str,
    reused_compilation: bool,
    provider_cleanup_ids: tuple[str, ...] = (),
    provider_upload_reservation_id: UUID | None = None,
) -> bool:
    """Atomically publish a repair result only for its current generation."""
    session = async_session_factory()
    try:
        await lock_provider_cleanup_boundary(session, tenant_id, "smallest")
        session, knowledge_base, source = await _context(
            tenant_id,
            kb_id,
            source_id,
            for_update=True,
            session=session,
        )
    except Exception:
        await session.close()
        raise
    committed = False
    try:
        if not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
            return False

        upload_reservation = None
        if provider_upload_reservation_id is not None:
            upload_reservation = await session.scalar(
                select(KnowledgeProviderCleanup)
                .where(
                    KnowledgeProviderCleanup.id == provider_upload_reservation_id,
                    KnowledgeProviderCleanup.tenant_id == tenant_id,
                    KnowledgeProviderCleanup.knowledge_base_id == knowledge_base.id,
                    KnowledgeProviderCleanup.knowledge_source_id == source.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                upload_reservation is None
                or upload_reservation.status != "processing"
                or upload_reservation.attempts != 0
                or upload_reservation.repair_run_id != repair_run_id
                or upload_reservation.provider_artifact_name != artifact_name
            ):
                await session.rollback()
                return False

        cleanup_reserved = await session.scalar(
            select(KnowledgeProviderCleanup.id).where(
                KnowledgeProviderCleanup.tenant_id == tenant_id,
                KnowledgeProviderCleanup.provider == "smallest",
                KnowledgeProviderCleanup.provider_item_id == provider_item_id,
                KnowledgeProviderCleanup.provider_knowledge_base_id
                == knowledge_base.provider_knowledge_base_id,
            )
        )
        if cleanup_reserved is not None:
            # Once an artifact is durably reserved for deletion it can never be
            # promoted back into serving state by an overlapping repair.
            await session.rollback()
            return False

        metadata = dict(source.source_metadata or {})
        previous_serving_signature = _compiled_serving_signature(
            content=source.content,
            structured=source.structured_content,
        )
        next_serving_signature = _compiled_serving_signature(
            content=compiled.content,
            structured=compiled.structured,
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
        source.structured_content = compiled.structured
        source.content_sha256 = raw_content_sha256
        source.mime_type = "text/html"
        source.size_bytes = page.downloaded_bytes
        source.status = "indexed"
        source.provider_item_id = provider_item_id
        source.error_message = None
        metadata.pop("staged_refresh", None)
        pending_cleanup_ids = {
            str(item) for item in metadata.get(PROVIDER_CLEANUP_PENDING_KEY) or [] if item
        }
        pending_cleanup_ids.update(provider_cleanup_ids)
        pending_cleanup_ids.discard(provider_item_id)
        if pending_cleanup_ids:
            metadata[PROVIDER_CLEANUP_PENDING_KEY] = sorted(pending_cleanup_ids)
            if not knowledge_base.provider_knowledge_base_id:
                raise WebsiteRecoveryError(
                    "The provider cleanup could not be recorded without a remote knowledge ID.",
                    code="provider_cleanup_identity_missing",
                )
            await _ensure_provider_cleanup_records(
                session,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base.id,
                knowledge_source_id=source.id,
                repair_run_id=repair_run_id,
                provider_name="smallest",
                provider_knowledge_base_id=knowledge_base.provider_knowledge_base_id,
                provider_item_ids=tuple(sorted(pending_cleanup_ids)),
            )
        else:
            metadata.pop(PROVIDER_CLEANUP_PENDING_KEY, None)
        metadata.update(
            {
                "extraction_method": page.method,
                "content_sha256": compiled_content_sha256,
                COMPILED_SERVING_SIGNATURE_KEY: next_serving_signature,
                "provider_artifact_name": artifact_name,
                "retrieval_content_source": "vav_website_recovery",
                "compiler": {
                    **(compiled.structured.get("compiler") or {}),
                    "reused": reused_compilation,
                },
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
        source.compiled_at = source.compiled_at if reused_compilation else now
        source.last_synced_at = now
        knowledge_base.last_synced_at = now
        if upload_reservation is not None:
            # Candidate promotion and compensation cancellation are one commit:
            # neither a crash nor a concurrent sweeper can leave an accepted
            # artifact still eligible for deletion.
            await session.delete(upload_reservation)

        affected_agent_ids: list[str] = []
        for binding in knowledge_base.agent_bindings:
            agent = binding.agent
            if agent.voice_provider in VAV_NATIVE_KNOWLEDGE_PROVIDERS:
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
                "agents_requiring_sync": affected_agent_ids,
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
        title, text = extract_readable_text(static_html, url=final_url)
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
        title, text = extract_readable_text(rendered_html, url=final_url)
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
    provider_document = await asyncio.to_thread(
        searchable_pdf,
        title=page.title,
        url=page.url,
        text=compiled.content,
    )

    if not await _ensure_provider_refresh_isolated(
        tenant_id,
        kb_id,
        source_id,
        repair_run_id=repair_run_id,
    ):
        return
    if not await _set_stage(
        tenant_id,
        kb_id,
        source_id,
        "provider_indexing",
        "Sending the recovered searchable document to the knowledge provider.",
        repair_run_id=repair_run_id,
    ):
        return
    session, knowledge_base, source = await _context(tenant_id, kb_id, source_id)
    provider: SmallestAIClient | None = None
    remote_id: str | None = None
    provider_item_id: str | None = None
    provider_upload_reservation_id: UUID | None = None
    try:
        if not _repair_run_is_current(source, repair_run_id):
            await session.rollback()
            return
        provider = await _tenant_client(session, tenant_id)
        remote_id = await _ensure_remote_locked(session, knowledge_base, provider)

        content_sha256 = hashlib.sha256(compiled.content.encode("utf-8")).hexdigest()
        artifact_prefix = f"vav-web-recovery-{source.id}-"
        legacy_artifact_name = f"vav-web-recovery-{source.id}.pdf"
        # Each execution gets a unique candidate name. A stale/superseded run
        # can therefore delete its own upload without racing another run that
        # happens to compile identical content.
        artifact_name = f"{artifact_prefix}{content_sha256[:16]}-{uuid4().hex[:12]}.pdf"
        existing_items = await provider.list_knowledge_items(remote_id)
        pending_cleanup_ids = {
            str(item)
            for item in (source.source_metadata or {}).get(PROVIDER_CLEANUP_PENDING_KEY) or []
            if item
        }
        reusable_item = next(
            (
                item
                for item in existing_items
                if _provider_item_id(item) == source.provider_item_id
                and (source.source_metadata or {}).get("content_sha256") == content_sha256
                and _provider_status(item) in PROVIDER_READY
                and _provider_item_id(item)
                and _provider_item_id(item) not in pending_cleanup_ids
            ),
            None,
        )
        excluded_item_ids: set[str] = set()
        if reusable_item is not None:
            provider_item_id = _provider_item_id(reusable_item)
            artifact_name = (
                _provider_file_name(reusable_item)
                or str((source.source_metadata or {}).get("provider_artifact_name") or "")
                or artifact_name
            )
        else:
            provider_upload_reservation_id = await _reserve_provider_upload_artifact(
                tenant_id,
                kb_id,
                source_id,
                repair_run_id=repair_run_id,
                remote_id=remote_id,
                artifact_name=artifact_name,
            )
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
        if not await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "verifying",
            "The searchable document was accepted. VAV is verifying provider indexing.",
            repair_run_id=repair_run_id,
        ):
            if provider_upload_reservation_id is not None:
                await _abandon_provider_upload_reservation(
                    provider_upload_reservation_id,
                    provider=provider,
                    message="The repair generation was superseded after provider upload.",
                )
            return
        provider_item_id = await _wait_for_provider_index(
            provider,
            knowledge_base_id=remote_id,
            provider_item_id=provider_item_id,
            artifact_name=artifact_name,
            excluded_item_ids=excluded_item_ids,
        )
        provider_cleanup_ids = tuple(
            sorted(
                pending_cleanup_ids
                | {
                    item_id
                    for item in existing_items
                    if (item_id := _provider_item_id(item))
                    and item_id != provider_item_id
                    and (
                        _provider_file_name(item).startswith(artifact_prefix)
                        or _provider_file_name(item) == legacy_artifact_name
                    )
                }
            )
        )
    except Exception as exc:
        if provider_upload_reservation_id is not None and provider is not None:
            await _abandon_provider_upload_reservation(
                provider_upload_reservation_id,
                provider=provider,
                message=f"The provider candidate was not published: {exc}",
            )
        raise
    finally:
        await session.close()

    if provider is None or remote_id is None or provider_item_id is None:
        raise WebsiteRecoveryError(
            "The provider did not return a durable knowledge artifact.",
            code="provider_item_missing",
        )

    committed = await _commit_repair_success(
        tenant_id,
        kb_id,
        source_id,
        repair_run_id=repair_run_id,
        page=page,
        compiled=compiled,
        raw_content_sha256=raw_content_sha256,
        compiled_content_sha256=content_sha256,
        provider_item_id=provider_item_id,
        artifact_name=artifact_name,
        requested_mode=requested_mode,
        reused_compilation=reused_compilation,
        provider_cleanup_ids=provider_cleanup_ids,
        provider_upload_reservation_id=provider_upload_reservation_id,
    )
    if not committed:
        if provider_upload_reservation_id is not None:
            await _abandon_provider_upload_reservation(
                provider_upload_reservation_id,
                provider=provider,
                message="The provider candidate lost its publication race.",
            )
        return
    await _cleanup_provider_artifacts(
        tenant_id,
        kb_id,
        source_id,
        repair_run_id=repair_run_id,
        provider=provider,
        remote_id=remote_id,
        provider_item_ids=provider_cleanup_ids,
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
                repair_run_id=repair_run_id,
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
                repair_run_id=repair_run_id,
            )
        )
