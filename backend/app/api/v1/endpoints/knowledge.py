from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import structlog
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
    KnowledgeProviderCleanup,
    KnowledgeServingRevision,
    KnowledgeSource,
    KnowledgeSpeechLexicon,
)
from app.models.call import Call
from app.providers.smallest import SmallestAIClient, SmallestAIError, get_smallest_client
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
    KnowledgeReleaseReactivationRequest,
    KnowledgeServingRevisionResponse,
    KnowledgeSourceResponse,
    KnowledgeSpeechLexiconResponse,
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
from app.services.knowledge_serving import (
    KnowledgeServingError,
    publish_serving_revision,
    validate_serving_revision_integrity,
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
from app.services.pdf_ingestion import PdfIngestionError, PreparedPdf, prepare_pdf
from app.services.provider_credentials import (
    ProviderCredentialError,
    load_provider_config,
    lock_provider_cleanup_boundary,
)
from app.services.rate_limit import enforce_rate_limit
from app.services.runtime_capacity import TERMINAL_CALL_STATUSES
from app.services.speech_lexicon import SpeechLexiconError, publish_speech_lexicon

router = APIRouter(prefix="/knowledge", tags=["Knowledge Studio"])
logger = structlog.get_logger()
MAX_KNOWLEDGE_PDF_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_PDF_CHARS = 500_000
PROVIDER_INDEXED_STATUSES = {
    "complete",
    "completed",
    "indexed",
    "processed",
    "ready",
    "success",
    "succeeded",
}


async def _tenant_smallest_client(db: AsyncSession, tenant_id: UUID) -> SmallestAIClient:
    config = await load_provider_config(db, tenant_id, "smallest")
    api_key = str((config or {}).get("api_key") or "").strip()
    return SmallestAIClient(api_key=api_key) if api_key else get_smallest_client()


PROVIDER_FAILED_STATUSES = {"error", "failed", "failure"}
BOUND_AGENT_PROVIDER_OPERATIONS = {
    "provisioning",
    "provision_unknown",
    "publishing",
    "provider_scanning",
    "publish_unknown",
}


def _provider_error(exc: SmallestAIError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


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


def _serving_revision_response(
    revision: KnowledgeServingRevision,
) -> KnowledgeServingRevisionResponse:
    return KnowledgeServingRevisionResponse(
        revision_id=revision.id,
        compiler_version=revision.compiler_version,
        source_revision_sha256=revision.source_revision_sha256,
        chunk_revision_sha256=revision.chunk_revision_sha256,
        fact_revision_sha256=revision.fact_revision_sha256,
        entity_revision_sha256=revision.entity_revision_sha256,
        content_sha256=revision.content_sha256,
        published_at=revision.published_at,
        source_count=revision.source_count,
        chunk_count=revision.chunk_count,
        fact_count=revision.fact_count,
        entity_count=revision.entity_count,
        speech_lexicon_artifact_id=revision.speech_lexicon_artifact_id,
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
    speech_lexicon = (
        KnowledgeSpeechLexiconResponse(
            artifact_id=kb.speech_lexicon.id,
            compiler_version=kb.speech_lexicon.compiler_version,
            source_revision_sha256=kb.speech_lexicon.source_revision_sha256,
            content_sha256=kb.speech_lexicon.content_sha256,
            generated_at=kb.speech_lexicon.generated_at,
            source_count=kb.speech_lexicon.source_count,
            entry_count=len(kb.speech_lexicon.entries or []),
            coverage=dict(kb.speech_lexicon.coverage or {}),
        )
        if kb.speech_lexicon is not None
        else None
    )
    serving_revision = (
        _serving_revision_response(kb.serving_revision) if kb.serving_revision is not None else None
    )
    return KnowledgeBaseResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        provider=kb.provider,
        provider_knowledge_base_id=kb.provider_knowledge_base_id,
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
        speech_lexicon=speech_lexicon,
        serving_revision=serving_revision,
        has_pending_changes=kb.approval_status == "draft" and serving_revision is not None,
        sources=[_source_response(source) for source in kb.sources],
        agent_bindings=bindings,
        crawls=[KnowledgeCrawlResponse.model_validate(crawl) for crawl in kb.crawls],
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def _ensure_bound_agents_accept_knowledge_change(kb: KnowledgeBase) -> None:
    """Reject a source mutation that could race an in-flight provider publish."""
    busy_agents = [
        binding.agent.name
        for binding in kb.agent_bindings
        if binding.agent.sync_status in BOUND_AGENT_PROVIDER_OPERATIONS
    ]
    if busy_agents:
        raise HTTPException(
            status_code=409,
            detail=(
                "Wait for the bound agent provider operation to finish before changing "
                "knowledge: " + ", ".join(sorted(busy_agents))
            ),
        )


def _ensure_no_live_provider_content_mutation(kb: KnowledgeBase) -> None:
    """Fail closed until Smallest supports a true blue/green knowledge swap.

    Smallest agents query the provider knowledge-base ID directly. Uploading a
    draft into that same collection would make unapproved content immediately
    searchable, even when VAV retains its immutable serving revision.
    """
    provider_agents = sorted(
        {
            binding.agent.name
            for binding in kb.agent_bindings
            if binding.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS
        }
    )
    if provider_agents:
        raise HTTPException(
            status_code=409,
            detail=(
                "Safe website refresh for a Smallest.ai-bound agent requires a "
                "blue/green provider knowledge-base swap. Unbind the agent before "
                "crawling or repairing this knowledge base; VAV has not changed the "
                "live provider collection: " + ", ".join(provider_agents)
            ),
        )


async def _ensure_no_pending_provider_cleanup(db: AsyncSession, kb: KnowledgeBase) -> None:
    pending = await db.scalar(
        select(KnowledgeProviderCleanup.id).where(
            KnowledgeProviderCleanup.tenant_id == kb.tenant_id,
            KnowledgeProviderCleanup.knowledge_base_id == kb.id,
            KnowledgeProviderCleanup.status != "completed",
        )
    )
    if pending is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Wait for remote knowledge cleanup to finish before changing provider "
                "content. The cleanup worker will retry automatically."
            ),
        )


def _kick_provider_cleanup(cleanup_ids: list[UUID]) -> None:
    if not cleanup_ids:
        return
    from app.tasks.knowledge_tasks import cleanup_provider_artifact

    for cleanup_id in cleanup_ids:
        try:
            cleanup_provider_artifact.apply_async(
                args=[str(cleanup_id)],
                queue="knowledge",
                ignore_result=True,
                retry=False,
            )
        except Exception:
            # The row was committed first; Celery Beat will recover a failed
            # broker kick without losing the remote-deletion intent.
            logger.warning(
                "knowledge_provider_cleanup_kick_failed",
                cleanup_id=str(cleanup_id),
            )


async def _fail_provider_upload_reservation(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    cleanup_id: UUID,
    message: str,
) -> None:
    """Make an uncommitted provider upload immediately eligible for cleanup."""
    await db.rollback()
    await lock_provider_cleanup_boundary(db, tenant_id, "smallest")
    knowledge_base = await db.scalar(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    cleanup = await db.scalar(
        select(KnowledgeProviderCleanup)
        .where(
            KnowledgeProviderCleanup.id == cleanup_id,
            KnowledgeProviderCleanup.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cleanup is not None:
        cleanup.status = "pending"
        cleanup.available_at = datetime.now(UTC)
        cleanup.lease_expires_at = None
        cleanup.last_error = message[:1000]
    if knowledge_base is not None:
        knowledge_base.sync_status = "error"
        knowledge_base.sync_error = message[:1000]
        knowledge_base.last_synced_at = datetime.now(UTC)
    await db.commit()
    if cleanup is not None:
        _kick_provider_cleanup([cleanup.id])


def _invalidate_bound_agent_deployments(kb: KnowledgeBase) -> list[UUID]:
    """Require provider-backed agents to publish the current knowledge tool.

    A provider can finish indexing a new source without changing the active agent
    revision. Marking both sides pending prevents a legacy or stale revision from
    continuing to look synced merely because the ingestion job completed.
    """
    affected_agent_ids: list[UUID] = []
    for binding in kb.agent_bindings:
        agent = binding.agent
        if getattr(agent, "voice_provider", "smallest") in VAV_NATIVE_KNOWLEDGE_PROVIDERS:
            # VAV realtime sessions retrieve approved VAV knowledge directly on every
            # turn, so indexed source changes are live without an Atoms publish.
            binding.provider = agent.voice_provider
            binding.sync_status = "synced"
            binding.last_synced_at = datetime.now(UTC)
            continue
        binding.sync_status = "pending"
        binding.last_synced_at = None
        if not agent.provider_agent_id:
            continue
        if agent.sync_status in BOUND_AGENT_PROVIDER_OPERATIONS:
            # Source endpoints run the preflight above. Keep this guard so the
            # helper remains safe if it is reused by another workflow.
            continue
        if agent.sync_status != "error":
            agent.sync_status = "dirty"
        affected_agent_ids.append(agent.id)
    return affected_agent_ids


def _knowledge_query(tenant_id: UUID):
    return (
        select(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == tenant_id)
        .options(
            selectinload(KnowledgeBase.sources),
            selectinload(KnowledgeBase.agent_bindings).selectinload(AgentKnowledgeBinding.agent),
            selectinload(KnowledgeBase.crawls).selectinload(KnowledgeCrawl.pages),
            selectinload(KnowledgeBase.speech_lexicon),
            selectinload(KnowledgeBase.serving_revision),
        )
    )


async def _get_knowledge_base(
    db: AsyncSession,
    tenant_id: UUID,
    kb_id: UUID,
    *,
    for_update: bool = True,
) -> KnowledgeBase:
    query = (
        _knowledge_query(tenant_id)
        .where(KnowledgeBase.id == kb_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        # All Knowledge Studio mutations share this lock, so approval snapshots
        # a coherent draft and pointer swaps cannot race source edits.
        query = query.with_for_update()
    result = await db.execute(query)
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


async def _mark_provider_error(
    db: AsyncSession,
    kb: KnowledgeBase,
    exc: SmallestAIError,
) -> None:
    kb.sync_status = "error"
    kb.sync_error = str(exc)
    kb.last_synced_at = datetime.now(UTC)
    # Provider failures are part of the durable operator state. Persist them
    # before raising so the request dependency's rollback cannot erase the
    # evidence users need to diagnose and safely retry the operation.
    await db.commit()


async def _ensure_remote(
    db: AsyncSession,
    kb: KnowledgeBase,
    client: SmallestAIClient,
) -> str:
    # Serialize creation across API requests and parallel crawl workers. Refresh
    # the identity-map row after acquiring the lock so a waiter observes the ID
    # committed by the creator ahead of it.
    locked_kb = await db.scalar(
        _knowledge_query(kb.tenant_id)
        .where(KnowledgeBase.id == kb.id, KnowledgeBase.tenant_id == kb.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_kb is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    if locked_kb.provider_knowledge_base_id:
        remote_id = locked_kb.provider_knowledge_base_id
        # Release the provisioning lock before the caller performs a longer
        # scrape/upload, while durably preserving any approval invalidation.
        await db.commit()
        return remote_id
    if remote_creation_outcome_unknown(locked_kb):
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail=(
                "Provider knowledge-base creation previously had an unknown outcome. "
                "Reconcile the Smallest.ai workspace before trying to create another copy."
            ),
        )
    locked_kb.sync_status = "provisioning"
    locked_kb.sync_error = None
    await db.flush()
    try:
        remote_id = await client.create_knowledge_base(
            name=locked_kb.name,
            description=locked_kb.description or "",
        )
    except SmallestAIError as exc:
        if exc.ambiguous:
            mark_remote_creation_outcome_unknown(locked_kb)
            locked_kb.last_synced_at = datetime.now(UTC)
            await db.commit()
            raise _provider_error(exc) from exc
        await _mark_provider_error(db, locked_kb, exc)
        raise _provider_error(exc) from exc
    locked_kb.provider_knowledge_base_id = remote_id
    locked_kb.sync_status = "processing" if locked_kb.source_count else "local_only"
    locked_kb.last_synced_at = datetime.now(UTC)
    # A remote resource now exists. Commit its mapping immediately so a later
    # scrape/upload failure cannot leave an orphaned provider knowledge base.
    await db.commit()
    return remote_id


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


def _retrieval_signature(kb: KnowledgeBase) -> tuple[tuple[str, str, str, str, str], ...]:
    """Capture all evidence that can change which source text agents retrieve."""
    return tuple(
        sorted(
            (
                str(source.id),
                source.status,
                canonical_source_url(source.location) or str(source.location or ""),
                str(source.content or "").strip(),
                str(source.provider_item_id or ""),
            )
            for source in kb.sources
        )
    )


def _provider_source_status(item: dict | None) -> str:
    if not item:
        return "processing"
    value = str(item.get("processingStatus") or item.get("status") or "processing")
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in PROVIDER_INDEXED_STATUSES:
        return "indexed"
    if normalized in PROVIDER_FAILED_STATUSES:
        return "failed"
    return "processing"


def _provider_url_key(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return canonical_source_url(raw) or raw.rstrip("/")


def _provider_item_urls(item: dict) -> list[object]:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return [
        value
        for value in (
            item.get("url"),
            item.get("hostUrl"),
            item.get("location"),
            item.get("sourceUrl"),
            item.get("sourceURL"),
            item.get("source_url"),
            metadata.get("url"),
            metadata.get("sourceUrl"),
            metadata.get("sourceURL"),
            metadata.get("source_url"),
            item.get("title"),
            item.get("fileName"),
            item.get("name"),
        )
        if value
    ]


def _expand_scraped_provider_items(scraped: list[dict]) -> list[dict]:
    """Expand Smallest scrape batches into URL-shaped provider records.

    Smallest returns ``scraped-urls`` as crawl batches.  The batch carries the
    authoritative ``processingStatus`` and ``hostUrl`` while any individual
    URLs live under ``scrapedUrls``.  Preserve the batch and synthesize child
    records that inherit its status so both response variants reconcile.
    """
    expanded: list[dict] = []
    for batch in scraped:
        expanded.append(batch)
        nested = batch.get("scrapedUrls")
        if not isinstance(nested, list):
            continue
        for value in nested:
            child = dict(value) if isinstance(value, dict) else {"url": value}
            if not child.get("processingStatus") and not child.get("status"):
                if batch.get("processingStatus"):
                    child["processingStatus"] = batch["processingStatus"]
                elif batch.get("status"):
                    child["status"] = batch["status"]
            if not child.get("_id") and not child.get("id"):
                if batch.get("_id"):
                    child["_id"] = batch["_id"]
                elif batch.get("id"):
                    child["id"] = batch["id"]
            expanded.append(child)
    return expanded


def _provider_file_name(item: dict) -> str:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(item.get("fileName") or metadata.get("fileName") or "")


def _provider_item_content(item: dict) -> str | None:
    """Return bounded provider-extracted text suitable for local retrieval."""
    content = item.get("content")
    if not isinstance(content, str):
        return None
    content = content.strip()
    return content[:MAX_EXTRACTED_PDF_CHARS] if content else None


def _provider_item_id(value: dict | None) -> str | None:
    """Resolve a provider item ID from the common upload response envelopes."""
    current: object = value
    for _ in range(4):
        if not isinstance(current, dict):
            return None
        item_id = current.get("_id") or current.get("id")
        if item_id:
            return str(item_id)
        current = current.get("data") or current.get("item")
    return None


def _reconcile_provider_sources(
    kb: KnowledgeBase,
    *,
    scraped: list[dict],
    items: list[dict],
    provider_knowledge_base: dict,
    now: datetime,
) -> None:
    provider_items = [*_expand_scraped_provider_items(scraped), *items]
    items_by_id = {
        str(item.get("_id") or item.get("id")): item
        for item in provider_items
        if item.get("_id") or item.get("id")
    }
    urls_by_location = {
        key: item
        for item in provider_items
        for value in _provider_item_urls(item)
        if (key := _provider_url_key(value))
    }
    files_by_name = {name: item for item in items if (name := _provider_file_name(item))}
    overall_status = _provider_source_status(provider_knowledge_base)

    for source in kb.sources:
        item = None
        if source.provider_item_id:
            item = items_by_id.get(source.provider_item_id)
        if item is None and source.location:
            item = urls_by_location.get(_provider_url_key(source.location))
        if item is None and source.source_type == "file":
            item = files_by_name.get(source.name)

        if item is not None:
            source.status = _provider_source_status(item)
            source.provider_item_id = str(item.get("_id") or item.get("id") or "") or None
            source.last_synced_at = now
            source.error_message = None
            if not str(getattr(source, "content", None) or "").strip():
                provider_content = _provider_item_content(item)
                if provider_content:
                    source.content = provider_content
                    source_metadata = dict(getattr(source, "source_metadata", None) or {})
                    source_metadata["retrieval_content_source"] = "smallest_index"
                    source.source_metadata = source_metadata
            if source.status == "failed":
                source.error_message = str(
                    item.get("error") or item.get("errorMessage") or "Provider processing failed"
                )
            elif source.status == "indexed" and not has_searchable_content(source):
                source.status = "failed"
                source.error_message = (
                    "Provider indexing completed, but VAV found no retrievable text. "
                    "Repair or re-upload the source so VAV can extract it."
                )
            continue

        # Smallest.ai's knowledge-base status is authoritative for the whole
        # ingestion job. Its per-source endpoint may normalize a URL or omit a
        # completed item, so a completed base safely closes any remaining
        # provider-backed URL/file source instead of leaving it stuck forever.
        if (
            overall_status == "indexed"
            and source.source_type in {"url", "file", "website", "sitemap"}
            and source.status in {"pending", "processing"}
        ):
            if has_searchable_content(source):
                source.status = "indexed"
                source.error_message = None
            else:
                source.status = "failed"
                source.error_message = (
                    "Provider indexing completed, but VAV found no retrievable text. "
                    "Re-upload the source to repair it."
                )
            source.last_synced_at = now

    _recount(kb)


def _provider_source_delete_target(
    source: KnowledgeSource,
    *,
    scraped: list[dict],
    items: list[dict],
) -> tuple[str, str] | None:
    """Resolve the provider collection and ID used to delete one local source."""
    scraped_batches_by_id = {
        str(batch.get("_id") or batch.get("id")): batch
        for batch in scraped
        if batch.get("_id") or batch.get("id")
    }
    scraped_parent_by_child_id: dict[str, str] = {}
    for batch_id, batch in scraped_batches_by_id.items():
        nested = batch.get("scrapedUrls")
        if not isinstance(nested, list):
            continue
        for child in nested:
            if not isinstance(child, dict):
                continue
            child_id = str(child.get("_id") or child.get("id") or "")
            if child_id:
                scraped_parent_by_child_id[child_id] = batch_id
    items_by_id = {
        str(item.get("_id") or item.get("id")): item
        for item in items
        if item.get("_id") or item.get("id")
    }
    if source.provider_item_id in scraped_batches_by_id:
        return "scraped", source.provider_item_id
    if source.provider_item_id in scraped_parent_by_child_id:
        return "scraped", scraped_parent_by_child_id[source.provider_item_id]
    if source.provider_item_id in items_by_id:
        return "item", source.provider_item_id

    if source.location:
        source_key = _provider_url_key(source.location)
        for batch_id, batch in scraped_batches_by_id.items():
            if source_key and any(
                _provider_url_key(value) == source_key
                for item in _expand_scraped_provider_items([batch])
                for value in _provider_item_urls(item)
            ):
                return "scraped", batch_id
        for item in items:
            if source_key and any(
                _provider_url_key(value) == source_key for value in _provider_item_urls(item)
            ):
                provider_id = str(item.get("_id") or item.get("id") or "")
                if provider_id:
                    return "item", provider_id

    if source.source_type == "file":
        for item in items:
            if _provider_file_name(item) == source.name:
                provider_id = str(item.get("_id") or item.get("id") or "")
                if provider_id:
                    return "item", provider_id
    return None


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
        provider="smallest",
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
    return _knowledge_response(
        await _get_knowledge_base(
            db,
            current_user.tenant_id,
            kb_id,
            for_update=False,
        )
    )


@router.get("/{kb_id}/releases", response_model=list[KnowledgeServingRevisionResponse])
async def list_knowledge_releases(
    kb_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    knowledge_exists = await db.scalar(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.tenant_id == current_user.tenant_id,
        )
    )
    if knowledge_exists is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    revisions = (
        await db.scalars(
            select(KnowledgeServingRevision)
            .where(
                KnowledgeServingRevision.tenant_id == current_user.tenant_id,
                KnowledgeServingRevision.knowledge_base_id == kb_id,
            )
            .order_by(
                KnowledgeServingRevision.published_at.desc(),
                KnowledgeServingRevision.id.desc(),
            )
        )
    ).all()
    return [_serving_revision_response(revision) for revision in revisions]


@router.post(
    "/{kb_id}/releases/{revision_id}/activate",
    response_model=KnowledgeBaseResponse,
)
async def reactivate_knowledge_release(
    kb_id: UUID,
    revision_id: UUID,
    data: KnowledgeReleaseReactivationRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reactivate one retained VAV release using an audited CAS pointer swap."""

    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    previous_revision_id = kb.serving_revision_id
    if previous_revision_id != data.expected_current_revision_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "The live release changed after this page was loaded. Refresh release "
                "history and review the newer operator decision before retrying."
            ),
        )
    if revision_id == previous_revision_id:
        raise HTTPException(status_code=409, detail="The selected release is already live")

    provider_agents = sorted(
        {
            binding.agent.name
            for binding in kb.agent_bindings
            if binding.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS
        }
    )
    if provider_agents:
        raise HTTPException(
            status_code=409,
            detail=(
                "Historical VAV release reactivation cannot change a provider-native "
                "Smallest.ai collection. Use its verified provider rollback route first: "
                + ", ".join(provider_agents)
            ),
        )

    target_row = (
        await db.execute(
            select(KnowledgeServingRevision, KnowledgeSpeechLexicon)
            .join(
                KnowledgeSpeechLexicon,
                KnowledgeSpeechLexicon.id == KnowledgeServingRevision.speech_lexicon_artifact_id,
            )
            .where(
                KnowledgeServingRevision.id == revision_id,
                KnowledgeServingRevision.tenant_id == current_user.tenant_id,
                KnowledgeServingRevision.knowledge_base_id == kb.id,
                KnowledgeSpeechLexicon.tenant_id == current_user.tenant_id,
                KnowledgeSpeechLexicon.knowledge_base_id == kb.id,
            )
            .options(selectinload(KnowledgeServingRevision.sources))
        )
    ).one_or_none()
    if target_row is None:
        raise HTTPException(status_code=404, detail="Historical knowledge release not found")
    target, speech_lexicon = target_row
    try:
        validate_serving_revision_integrity(target, speech_lexicon)
    except KnowledgeServingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    kb.serving_revision_id = target.id
    kb.speech_lexicon_artifact_id = speech_lexicon.id
    # A historical release is safe to serve, but it cannot implicitly declare
    # the current mutable working copy approved. Keep that draft review gate.
    kb.approval_status = "draft"
    kb.published_at = target.published_at
    # Deliberately preserve serving_revocation_generation. Explicit revocation
    # is a permanent admission fence for calls reserved before that event.
    await db.flush()
    await db.refresh(kb, attribute_names=["speech_lexicon", "serving_revision"])
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.serving_revision_reactivated",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "expected_current_revision_id": (
                str(data.expected_current_revision_id)
                if data.expected_current_revision_id is not None
                else None
            ),
            "previous_serving_revision_id": (
                str(previous_revision_id) if previous_revision_id is not None else None
            ),
            "reactivated_serving_revision_id": str(target.id),
            "speech_lexicon_artifact_id": str(speech_lexicon.id),
            "reason": data.reason,
        },
    )
    return _knowledge_response(kb)


@router.patch("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: UUID,
    data: KnowledgeBaseUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    updates = data.model_dump(exclude_unset=True)
    serving_fields = {
        "name",
        "description",
        "scope_type",
        "scope_label",
        "languages",
        "tags",
    }
    approval_invalidated = False
    if updates.keys() & serving_fields:
        _ensure_bound_agents_accept_knowledge_change(kb)
        approval_invalidated = invalidate_knowledge_approval(kb)
    for key, value in updates.items():
        setattr(kb, key, value)
    if kb.provider_knowledge_base_id and ({"name", "description"} & updates.keys()):
        try:
            client = await _tenant_smallest_client(db, current_user.tenant_id)
            await client.update_knowledge_base(
                knowledge_base_id=kb.provider_knowledge_base_id,
                name=kb.name,
                description=kb.description or "",
            )
        except SmallestAIError as exc:
            raise _provider_error(exc) from exc
        kb.last_synced_at = datetime.now(UTC)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.updated",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "fields": sorted(updates),
            "approval_invalidated": approval_invalidated,
            "serving_revision_retained": bool(kb.serving_revision_id),
        },
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/provision", response_model=KnowledgeBaseResponse)
async def provision_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    await _ensure_remote(db, kb, client)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.provisioned",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"provider": kb.provider},
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/sitemap/discover", response_model=SitemapDiscoveryResponse)
async def discover_sitemap(
    kb_id: UUID,
    data: SitemapDiscoveryRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    remote_id = await _ensure_remote(db, kb, client)
    try:
        urls = await client.discover_sitemap_urls(
            knowledge_base_id=remote_id,
            sitemap_url=str(data.sitemap_url),
        )
    except SmallestAIError as exc:
        await _mark_provider_error(db, kb, exc)
        raise _provider_error(exc) from exc
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
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
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
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
    crawl = next((item for item in kb.crawls if item.id == crawl_id), None)
    if crawl is None:
        raise HTTPException(status_code=404, detail="Website crawl not found")
    if crawl.status in ACTIVE_CRAWL_STATUSES:
        raise HTTPException(status_code=409, detail="The website crawl is already active")

    invalidate_knowledge_approval(kb)
    failed_pages = [page for page in crawl.pages if page.status == "failed"]
    source_by_id = {source.id: source for source in kb.sources}
    from app.tasks.knowledge_tasks import _queue_repair_metadata

    queued_sources: dict[UUID, str] = {}
    for page in failed_pages:
        source = source_by_id.get(page.knowledge_source_id)
        if source is None:
            continue
        page.status = "queued"
        page.retry_count += 1
        page.error_code = None
        page.error_message = None
        if source.id not in queued_sources:
            source.status = "processing"
            source.error_message = None
            queued_metadata, repair_run_id, _ = _queue_repair_metadata(
                source.source_metadata,
                staged_refresh=False,
                message="Failed page queued for another complete recovery attempt.",
            )
            source.source_metadata = queued_metadata
            queued_sources[source.id] = repair_run_id

    crawl.error_message = None
    crawl.completed_at = None
    crawl.status = "retrying" if queued_sources else "queued"
    crawl.failed_count = max(0, crawl.failed_count - len(failed_pages))
    crawl.queued_count = len(queued_sources)
    _recount(kb)
    await db.commit()

    from app.tasks.knowledge_tasks import _mark_failed, crawl_website, repair_website_source

    if queued_sources:
        for source_id, repair_run_id in queued_sources.items():
            try:
                repair_website_source.apply_async(
                    args=[
                        str(current_user.tenant_id),
                        str(kb.id),
                        str(source_id),
                        repair_run_id,
                    ],
                    queue="knowledge",
                )
            except Exception:
                await _mark_failed(
                    current_user.tenant_id,
                    kb.id,
                    source_id,
                    message="The recovery worker is unavailable. Retry this failed page.",
                    code="worker_unavailable",
                    repair_run_id=repair_run_id,
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


@router.post("/{kb_id}/sources/urls", response_model=KnowledgeBaseResponse)
async def add_url_sources(
    kb_id: UUID,
    data: UrlSourceCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
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
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    remote_id = await _ensure_remote(db, kb, client)
    try:
        await client.scrape_knowledge_urls(knowledge_base_id=remote_id, urls=new_urls)
    except SmallestAIError as exc:
        await _mark_provider_error(db, kb, exc)
        raise _provider_error(exc) from exc
    for url in new_urls:
        kb.sources.append(
            KnowledgeSource(
                tenant_id=current_user.tenant_id,
                source_type="url",
                name=url.rsplit("/", 1)[-1] or url,
                location=url,
                status="processing",
            )
        )
    _recount(kb)
    kb.last_synced_at = datetime.now(UTC)
    affected_agent_ids = _invalidate_bound_agent_deployments(kb)
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
            "agents_requiring_sync": [str(agent_id) for agent_id in affected_agent_ids],
        },
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/sources/text", response_model=KnowledgeBaseResponse)
async def add_text_source(
    kb_id: UUID,
    data: TextSourceCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    smallest_bindings = [
        binding.agent.name
        for binding in kb.agent_bindings
        if binding.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS
    ]
    if smallest_bindings:
        raise HTTPException(
            status_code=409,
            detail=(
                "Pasted text is available to VAV-native agents only. Unbind the "
                "Smallest.ai agent before adding it: " + ", ".join(sorted(smallest_bindings))
            ),
        )
    approval_invalidated = invalidate_knowledge_approval(kb)
    kb.sources.append(
        KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="text",
            name=data.name,
            content=data.content,
            size_bytes=len(data.content.encode()),
            status="indexed",
            source_metadata={
                "retrieval_content_source": "vav_text",
                "provider_note": "Available to VAV-native runtimes; not published to Smallest.ai",
            },
        )
    )
    _recount(kb)
    affected_agent_ids = _invalidate_bound_agent_deployments(kb)
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
            "approval_invalidated": approval_invalidated,
            "agents_requiring_sync": [str(agent_id) for agent_id in affected_agent_ids],
        },
    )
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
    """Queue VAV extraction and provider re-indexing for one failed web page."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
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
    from app.tasks.knowledge_tasks import _queue_repair_metadata

    queued_metadata, repair_run_id, attempts = _queue_repair_metadata(
        metadata,
        staged_refresh=staged_refresh,
        message="VAV queued safe download, extraction, provider indexing and verification.",
    )
    if not staged_refresh:
        source.status = "processing"
    source.error_message = None
    source.source_metadata = queued_metadata
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
            "repair_run_id": repair_run_id,
        },
    )
    # The worker must never race the request transaction that records the job.
    await db.commit()

    from app.tasks.knowledge_tasks import repair_website_source as repair_task

    try:
        repair_task.apply_async(
            args=[str(current_user.tenant_id), str(kb.id), str(source.id), repair_run_id],
            queue="knowledge",
        )
    except Exception as exc:
        from app.tasks.knowledge_tasks import _mark_failed

        message = "The website-recovery worker is temporarily unavailable."
        await _mark_failed(
            current_user.tenant_id,
            kb.id,
            source.id,
            message=message,
            code="worker_unavailable",
            repair_run_id=repair_run_id,
        )
        raise HTTPException(status_code=503, detail=message) from exc
    return _knowledge_response(kb)


@router.post("/{kb_id}/sources/pdf", response_model=KnowledgeBaseResponse)
async def upload_pdf_source(
    kb_id: UUID,
    media: UploadFile = File(...),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    # PDF extraction can be expensive; do not hold the KB publication barrier
    # while OCR runs. All mutation preconditions are rechecked under the lock
    # immediately before every durable state transition below.
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id, for_update=False)
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
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

    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    remote_id = await _ensure_remote(db, kb, client)

    # Reserve a unique remote filename *before* uploading. The committed row is
    # both a credential/bind barrier and crash compensation: if this process
    # disappears after the provider accepts bytes, the sweeper finds the item
    # by its run-unique name and removes it.
    await lock_provider_cleanup_boundary(db, current_user.tenant_id, "smallest")
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
    # Reload credentials while holding the same advisory boundary used by
    # credential rotation. Once the reservation commits, unfinished cleanup
    # itself prevents the credential from disappearing.
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    existing_source = next(
        (
            source
            for source in kb.sources
            if source.source_type == "file" and source.name.casefold() == filename.casefold()
        ),
        None,
    )
    existing_source_id = existing_source.id if existing_source is not None else None
    artifact_name = f"vav-pdf-{kb.id.hex[:12]}-{uuid4().hex}.pdf"
    reservation = KnowledgeProviderCleanup(
        tenant_id=current_user.tenant_id,
        knowledge_base_id=kb.id,
        knowledge_source_id=existing_source_id,
        provider="smallest",
        provider_knowledge_base_id=remote_id,
        provider_item_id=f"pending-upload:{uuid4()}",
        provider_artifact_name=artifact_name,
        status="processing",
        attempts=0,
        available_at=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    db.add(reservation)
    await db.flush()
    reservation_id = reservation.id
    await db.commit()

    previous_items: list[dict] = []
    try:
        previous_items = await client.list_knowledge_items(remote_id)
        upload_response = await client.upload_knowledge_pdf(
            knowledge_base_id=remote_id,
            file_name=artifact_name,
            content=prepared.provider_content,
        )
        current_items = await client.list_knowledge_items(remote_id)
    except Exception as exc:
        await _fail_provider_upload_reservation(
            db,
            tenant_id=current_user.tenant_id,
            knowledge_base_id=kb_id,
            cleanup_id=reservation_id,
            message=str(exc),
        )
        if isinstance(exc, SmallestAIError):
            raise _provider_error(exc) from exc
        raise

    previous_item_ids = {
        str(item.get("_id") or item.get("id"))
        for item in previous_items
        if item.get("_id") or item.get("id")
    }
    artifact_item_ids = {
        str(item.get("_id") or item.get("id"))
        for item in current_items
        if _provider_file_name(item) == artifact_name and (item.get("_id") or item.get("id"))
    }
    response_item_id = _provider_item_id(upload_response)
    provider_item_id = None
    if response_item_id and response_item_id not in previous_item_ids:
        # A provider listing can lag a successful upload. Accept its durable ID
        # unless the same unique filename already resolves to a different item.
        if not artifact_item_ids or response_item_id in artifact_item_ids:
            provider_item_id = response_item_id
    if provider_item_id is None:
        added_artifact_ids = artifact_item_ids - previous_item_ids
        if len(added_artifact_ids) == 1:
            provider_item_id = next(iter(added_artifact_ids))
    if provider_item_id is None:
        message = (
            "Smallest.ai accepted the PDF but VAV could not verify its unique provider "
            "artifact. The upload is quarantined for automatic cleanup; retry after cleanup."
        )
        await _fail_provider_upload_reservation(
            db,
            tenant_id=current_user.tenant_id,
            knowledge_base_id=kb_id,
            cleanup_id=reservation_id,
            message=message,
        )
        raise HTTPException(status_code=502, detail=message)

    old_artifact_name = str(
        ((existing_source.source_metadata or {}) if existing_source is not None else {}).get(
            "provider_artifact_name"
        )
        or ""
    )
    stale_provider_ids = {
        str(item.get("_id") or item.get("id"))
        for item in previous_items
        if (item.get("_id") or item.get("id"))
        and (
            _provider_file_name(item).casefold() == filename.casefold()
            or (old_artifact_name and _provider_file_name(item) == old_artifact_name)
        )
    }
    if existing_source is not None and existing_source.provider_item_id:
        stale_provider_ids.add(existing_source.provider_item_id)
    stale_provider_ids.discard(provider_item_id)

    source_metadata = {
        "retrieval_content_source": "vav_pdf_ingestion",
        "extraction_method": prepared.extraction_method,
        "page_count": prepared.page_count,
        "ocr_page_count": prepared.ocr_page_count,
        "sha256": prepared.sha256,
        "provider_artifact_name": artifact_name,
    }
    cleanup_ids: list[UUID] = []
    try:
        await lock_provider_cleanup_boundary(db, current_user.tenant_id, "smallest")
        kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
        _ensure_bound_agents_accept_knowledge_change(kb)
        _ensure_no_live_provider_content_mutation(kb)
        reservation = await db.scalar(
            select(KnowledgeProviderCleanup)
            .where(
                KnowledgeProviderCleanup.id == reservation_id,
                KnowledgeProviderCleanup.tenant_id == current_user.tenant_id,
                KnowledgeProviderCleanup.knowledge_base_id == kb.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            reservation is None
            or reservation.status != "processing"
            or reservation.attempts != 0
            or reservation.provider_artifact_name != artifact_name
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "The PDF upload lost its cleanup reservation and was not published. "
                    "Wait for automatic cleanup, then retry."
                ),
            )

        source = next(
            (candidate for candidate in kb.sources if candidate.id == existing_source_id),
            None,
        )
        if existing_source_id is not None and source is None:
            raise HTTPException(
                status_code=409,
                detail="The PDF source changed while uploading; the candidate was not published.",
            )
        approval_invalidated = invalidate_knowledge_approval(kb)
        if source is None:
            source = KnowledgeSource(
                tenant_id=current_user.tenant_id,
                source_type="file",
                name=filename,
            )
            kb.sources.append(source)
        source.name = filename
        source.content = prepared.extracted_text
        source.file_content = content
        source.mime_type = "application/pdf"
        source.size_bytes = len(content)
        source.status = "processing"
        source.provider_item_id = provider_item_id
        source.error_message = None
        if stale_provider_ids:
            source_metadata["provider_cleanup_pending_ids"] = sorted(stale_provider_ids)
        source.source_metadata = source_metadata
        source.last_synced_at = datetime.now(UTC)
        await db.flush()

        if stale_provider_ids:
            from app.tasks.knowledge_tasks import _ensure_provider_cleanup_records

            cleanup_by_item = await _ensure_provider_cleanup_records(
                db,
                tenant_id=current_user.tenant_id,
                knowledge_base_id=kb.id,
                knowledge_source_id=source.id,
                repair_run_id=None,
                provider_name="smallest",
                provider_knowledge_base_id=remote_id,
                provider_item_ids=tuple(sorted(stale_provider_ids)),
            )
            cleanup_ids = list(cleanup_by_item.values())
        await db.delete(reservation)
        _recount(kb)
        kb.last_synced_at = datetime.now(UTC)
        affected_agent_ids = _invalidate_bound_agent_deployments(kb)
        await db.flush()
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action=(
                "knowledge_source.pdf_updated"
                if existing_source_id is not None
                else "knowledge_source.pdf_added"
            ),
            resource_type="knowledge_base",
            resource_id=str(kb.id),
            details={
                "name": filename,
                "bytes": len(content),
                "characters": len(prepared.extracted_text),
                "extraction_method": prepared.extraction_method,
                "ocr_pages": prepared.ocr_page_count,
                "replaced_existing": existing_source_id is not None,
                "approval_invalidated": approval_invalidated,
                "provider_artifact_name": artifact_name,
                "provider_cleanup_ids": [str(cleanup_id) for cleanup_id in cleanup_ids],
                "agents_requiring_sync": [str(agent_id) for agent_id in affected_agent_ids],
            },
        )
        await db.commit()
    except Exception as exc:
        await _fail_provider_upload_reservation(
            db,
            tenant_id=current_user.tenant_id,
            knowledge_base_id=kb_id,
            cleanup_id=reservation_id,
            message=f"PDF candidate was not published: {exc}",
        )
        raise

    _kick_provider_cleanup(cleanup_ids)
    return _knowledge_response(kb)


@router.delete("/{kb_id}/sources/{source_id}", response_model=KnowledgeBaseResponse)
async def delete_knowledge_source(
    kb_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Stage source removal and delete its mutable/provider copy.

    Immutable VAV releases are retained for audit and for calls already pinned
    to them.  If a live release exists, new VAV calls keep using it until the
    edited draft is approved or an operator explicitly revokes approval.
    """
    # Keep provider credential mutation, cleanup creation and source deletion
    # on one global lock order: advisory boundary -> KB -> cleanup -> source.
    await lock_provider_cleanup_boundary(db, current_user.tenant_id, "smallest")
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    _ensure_no_live_provider_content_mutation(kb)
    await _ensure_no_pending_provider_cleanup(db, kb)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    provider_target: tuple[str, str] | None = None
    scraped: list[dict] = []
    items: list[dict] = []
    if kb.provider_knowledge_base_id and source.source_type != "text":
        if source.source_type == "file" and source.provider_item_id:
            # VAV-created PDFs store the authoritative remote identity. Do not
            # make deletion depend on an eventually-consistent provider list.
            provider_target = ("items", source.provider_item_id)
        else:
            client = await _tenant_smallest_client(db, current_user.tenant_id)
            try:
                scraped = await client.list_scraped_knowledge_urls(kb.provider_knowledge_base_id)
                items = await client.list_knowledge_items(kb.provider_knowledge_base_id)
                provider_target = _provider_source_delete_target(
                    source,
                    scraped=scraped,
                    items=items,
                )
                if provider_target and provider_target[0] == "scraped":
                    await client.delete_scraped_knowledge_url(
                        knowledge_base_id=kb.provider_knowledge_base_id,
                        scraped_url_id=provider_target[1],
                    )
            except SmallestAIError as exc:
                await _mark_provider_error(db, kb, exc)
                raise _provider_error(exc) from exc

    approval_invalidated = invalidate_knowledge_approval(kb)

    removed_sources = [source]
    if provider_target:
        collection, provider_item_id = provider_target
        grouped_url_keys: set[str] = set()
        if collection == "scraped":
            grouped_url_keys = {
                key
                for batch in scraped
                if str(batch.get("_id") or batch.get("id") or "") == provider_item_id
                for item in _expand_scraped_provider_items([batch])
                for value in _provider_item_urls(item)
                if (key := _provider_url_key(value))
            }
        removed_sources = [
            candidate
            for candidate in kb.sources
            if candidate is source
            or candidate.provider_item_id == provider_item_id
            or (
                collection == "scraped"
                and candidate.location is not None
                and _provider_url_key(candidate.location) in grouped_url_keys
            )
        ]

    removed_ids = {candidate.id for candidate in removed_sources}
    cleanup_ids: list[UUID] = []
    if provider_target and provider_target[0] == "items":
        from app.tasks.knowledge_tasks import _ensure_provider_cleanup_records

        cleanup_by_item = await _ensure_provider_cleanup_records(
            db,
            tenant_id=current_user.tenant_id,
            knowledge_base_id=kb.id,
            knowledge_source_id=source.id,
            repair_run_id=None,
            provider_name="smallest",
            provider_knowledge_base_id=kb.provider_knowledge_base_id,
            provider_item_ids=(provider_target[1],),
        )
        cleanup_ids = list(cleanup_by_item.values())
    kb.sources[:] = [candidate for candidate in kb.sources if candidate.id not in removed_ids]
    for candidate in removed_sources:
        await db.delete(candidate)
    _recount(kb)
    affected_agent_ids = (
        _invalidate_bound_agent_deployments(kb) if source.source_type != "text" else []
    )
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
            "removed_source_ids": [str(candidate.id) for candidate in removed_sources],
            "removed_source_names": [candidate.name for candidate in removed_sources],
            "provider_collection": provider_target[0] if provider_target else None,
            "provider_item_id": provider_target[1] if provider_target else None,
            "provider_cleanup_ids": [str(cleanup_id) for cleanup_id in cleanup_ids],
            "approval_invalidated": approval_invalidated,
            "live_release_retained": kb.serving_revision_id is not None,
            "agents_requiring_sync": [str(agent_id) for agent_id in affected_agent_ids],
        },
    )
    await db.commit()
    _kick_provider_cleanup(cleanup_ids)
    return _knowledge_response(kb)


@router.post("/{kb_id}/refresh", response_model=KnowledgeBaseResponse)
async def refresh_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    await _ensure_no_pending_provider_cleanup(db, kb)
    if not kb.provider_knowledge_base_id:
        before_signature = _retrieval_signature(kb)
        removed = await consolidate_duplicate_url_sources(db, kb)
        if removed or before_signature != _retrieval_signature(kb):
            invalidate_knowledge_approval(kb)
        _recount(kb)
        return _knowledge_response(kb)
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    try:
        provider_knowledge_base = await client.get_knowledge_base(kb.provider_knowledge_base_id)
        scraped = await client.list_scraped_knowledge_urls(kb.provider_knowledge_base_id)
        items = await client.list_knowledge_items(kb.provider_knowledge_base_id)
        before_signature = _retrieval_signature(kb)
        before_consolidation = {
            source.id: (source.status, source.error_message) for source in kb.sources
        }
        removed = await consolidate_smallest_url_duplicates(
            db,
            kb,
            client,
            scraped=scraped,
            items=items,
        )
        after_consolidation = {
            source.id: (source.status, source.error_message) for source in kb.sources
        }
    except SmallestAIError as exc:
        await _mark_provider_error(db, kb, exc)
        raise _provider_error(exc) from exc
    approval_invalidated = False
    if removed or before_consolidation != after_consolidation:
        approval_invalidated = invalidate_knowledge_approval(kb)
        _recount(kb)
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="knowledge_sources.consolidated",
            resource_type="knowledge_base",
            resource_id=str(kb.id),
            details={"removed_source_count": removed},
        )
        # Provider deletion and local consolidation are not one distributed
        # transaction. Persist the idempotent local half before continuing.
        await db.commit()
    now = datetime.now(UTC)
    _reconcile_provider_sources(
        kb,
        scraped=scraped,
        items=items,
        provider_knowledge_base=provider_knowledge_base,
        now=now,
    )
    after_signature = _retrieval_signature(kb)
    if removed or before_signature != after_signature:
        approval_invalidated = invalidate_knowledge_approval(kb) or approval_invalidated
    kb.last_synced_at = now
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
            "approval_invalidated": approval_invalidated,
        },
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/approval", response_model=KnowledgeBaseResponse)
async def set_knowledge_approval(
    kb_id: UUID,
    data: KnowledgeApprovalRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    # This row lock is the admission barrier used by LiveKit workers. Whichever
    # transaction wins defines the boundary: admitted calls keep their pin;
    # reservations that lose to a revoke/publish cannot begin speech.
    locked_id = await db.scalar(
        select(KnowledgeBase.id)
        .where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.tenant_id == current_user.tenant_id,
        )
        .with_for_update()
    )
    if locked_id is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _recount(kb)
    if data.approved:
        await _ensure_no_pending_provider_cleanup(db, kb)
    if data.approved and (kb.sync_status != "ready" or not kb.indexed_source_count):
        raise HTTPException(
            status_code=409,
            detail="Make every source VAV-searchable before approving this knowledge base",
        )
    speech_lexicon = None
    serving_revision = None
    if data.approved:
        try:
            speech_lexicon = await publish_speech_lexicon(
                db,
                tenant_id=current_user.tenant_id,
                knowledge_base=kb,
                allow_draft_for_approval=True,
            )
            serving_revision = await publish_serving_revision(
                db,
                tenant_id=current_user.tenant_id,
                knowledge_base=kb,
                speech_lexicon=speech_lexicon,
                published_by_user_id=current_user.id,
                allow_draft_for_approval=True,
            )
        except (SpeechLexiconError, KnowledgeServingError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        if kb.serving_revision_id is not None:
            kb.serving_revocation_generation += 1
        kb.serving_revision_id = None
        kb.speech_lexicon_artifact_id = None
    kb.approval_status = "approved" if data.approved else "draft"
    kb.published_at = serving_revision.published_at if serving_revision is not None else None
    await db.flush()
    # Publication reloads the locked row with its source graph. Re-read through
    # the canonical response query so every relationship consumed below,
    # especially binding.agent, is eagerly populated in the async context.
    # Building the response from a partially refreshed identity-map instance
    # would otherwise attempt an unsafe lazy load and raise MissingGreenlet.
    kb = await _get_knowledge_base(
        db,
        current_user.tenant_id,
        kb_id,
        for_update=False,
    )
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_base.approval_changed",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "approved": data.approved,
            "speech_lexicon_artifact_id": str(speech_lexicon.id) if speech_lexicon else None,
            "serving_revision_id": str(serving_revision.id) if serving_revision else None,
            "serving_revocation_generation": kb.serving_revocation_generation,
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
    agent = await db.scalar(
        select(Agent).where(Agent.id == data.agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    native_consumer = agent.voice_provider in VAV_NATIVE_KNOWLEDGE_PROVIDERS
    pending_provider_cleanup = None
    if not native_consumer:
        pending_provider_cleanup = await db.scalar(
            select(KnowledgeProviderCleanup.id).where(
                KnowledgeProviderCleanup.tenant_id == current_user.tenant_id,
                KnowledgeProviderCleanup.knowledge_base_id == kb.id,
                KnowledgeProviderCleanup.status != "completed",
            )
        )
    if pending_provider_cleanup is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Wait for remote knowledge cleanup to finish before binding a "
                "Smallest.ai agent. This prevents an orphaned draft from being searchable."
            ),
        )
    has_live_native_release = native_consumer and kb.serving_revision_id is not None
    if kb.approval_status != "approved" and not has_live_native_release:
        raise HTTPException(status_code=409, detail="Approve this knowledge base first")
    if not native_consumer and not kb.provider_knowledge_base_id:
        raise HTTPException(
            status_code=409,
            detail="Provision this knowledge base in Smallest.ai before binding this agent",
        )
    if not native_consumer and kb.sync_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=(
                "Finish provider indexing and repair every source before binding a "
                "Smallest.ai agent."
            ),
        )
    active_recovery_sources = [
        source.name
        for source in kb.sources
        if isinstance(source.source_metadata, dict)
        and isinstance(source.source_metadata.get("recovery"), dict)
        and source.source_metadata["recovery"].get("status") in {"queued", "processing"}
    ]
    active_crawl = any(crawl.status in ACTIVE_CRAWL_STATUSES for crawl in kb.crawls)
    if not native_consumer and (active_recovery_sources or active_crawl):
        recovery_detail = (
            ": " + ", ".join(sorted(active_recovery_sources)) if active_recovery_sources else ""
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Wait for website crawl and recovery work to finish before binding a "
                "Smallest.ai agent" + recovery_detail
            ),
        )
    if not native_consumer and any(source.source_type == "text" for source in kb.sources):
        raise HTTPException(
            status_code=409,
            detail="Smallest.ai agents cannot use pasted VAV text sources in this knowledge base",
        )
    binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.agent_id == agent.id,
            AgentKnowledgeBinding.tenant_id == current_user.tenant_id,
        )
    )
    if binding:
        binding.knowledge_base_id = kb.id
        binding.provider = agent.voice_provider
    else:
        binding = AgentKnowledgeBinding(
            tenant_id=current_user.tenant_id,
            agent_id=agent.id,
            knowledge_base_id=kb.id,
            provider=agent.voice_provider,
        )
        db.add(binding)
    if native_consumer:
        binding.sync_status = "synced"
        binding.last_synced_at = datetime.now(UTC)
    else:
        binding.sync_status = "pending"
        binding.last_synced_at = None
    if agent.provider_agent_id:
        agent.sync_status = "dirty"
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
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    if agent and agent.provider_agent_id:
        agent.sync_status = "dirty"
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
    # `_get_knowledge_base` holds the same KB row lock taken by every browser
    # reservation and outbound knowledge-admission transaction. A plain Call
    # read here deliberately avoids reversing the Call -> KB lock order used by
    # outbound admission: an uncommitted reservation cannot reach paid provider
    # I/O, while a committed reservation is visible before deletion proceeds.
    active_call_id = await db.scalar(
        select(Call.id)
        .where(
            Call.tenant_id == current_user.tenant_id,
            Call.status.notin_(TERMINAL_CALL_STATUSES),
            Call.call_metadata["runtime"]["knowledge_serving_knowledge_base_id"]
            .as_string()
            .is_not(None),
            Call.call_metadata["runtime"]["knowledge_serving_knowledge_base_id"].as_string()
            == str(kb.id),
        )
        .limit(1)
    )
    if active_call_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This knowledge base is reserved by an active call; end the call before deleting it"
            ),
        )
    if kb.provider_knowledge_base_id:
        try:
            client = await _tenant_smallest_client(db, current_user.tenant_id)
            await client.delete_knowledge_base(kb.provider_knowledge_base_id)
        except SmallestAIError as exc:
            raise _provider_error(exc) from exc
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
