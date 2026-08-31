from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from pathlib import PurePath
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pypdf import PdfReader
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource
from app.providers.smallest import SmallestAIClient, SmallestAIError, get_smallest_client
from app.schemas.knowledge import (
    AgentKnowledgeBindRequest,
    KnowledgeAgentBindingResponse,
    KnowledgeApprovalRequest,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeSourceResponse,
    SitemapDiscoveryRequest,
    SitemapDiscoveryResponse,
    TextSourceCreate,
    UrlSourceCreate,
)
from app.services.audit import record_audit_event
from app.services.provider_credentials import load_provider_config

router = APIRouter(prefix="/knowledge", tags=["Knowledge Studio"])
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


def _extract_pdf_text(content: bytes) -> str | None:
    """Best-effort local text extraction keeps VAV retrieval provider-neutral."""
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        # Smallest remains authoritative for ingestion of PDFs its parser can
        # accept. A malformed or image-only document must not create invented
        # local text or expose raw bytes.
        return None
    text = "\n\n".join(" ".join(page.split()) for page in pages if page.strip()).strip()
    return text[:MAX_EXTRACTED_PDF_CHARS] or None


def _provider_error(exc: SmallestAIError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _source_response(source: KnowledgeSource) -> KnowledgeSourceResponse:
    content = source.content if isinstance(source.content, str) else ""
    response = KnowledgeSourceResponse.model_validate(source)
    return response.model_copy(
        update={
            "retrieval_ready": bool(content.strip()),
            "extracted_character_count": len(content.strip()),
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
        sources=[_source_response(source) for source in kb.sources],
        agent_bindings=bindings,
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


def _invalidate_bound_agent_deployments(kb: KnowledgeBase) -> list[UUID]:
    """Require provider-backed agents to publish the current knowledge tool.

    A provider can finish indexing a new source without changing the active agent
    revision. Marking both sides pending prevents a legacy or stale revision from
    continuing to look synced merely because the ingestion job completed.
    """
    affected_agent_ids: list[UUID] = []
    for binding in kb.agent_bindings:
        agent = binding.agent
        if getattr(agent, "voice_provider", "smallest") == "sarvam":
            # Sarvam sessions retrieve approved VAV knowledge directly on every
            # turn, so indexed source changes are live without an Atoms publish.
            binding.provider = "sarvam"
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
        )
    )


async def _get_knowledge_base(db: AsyncSession, tenant_id: UUID, kb_id: UUID) -> KnowledgeBase:
    result = await db.execute(_knowledge_query(tenant_id).where(KnowledgeBase.id == kb_id))
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
    if kb.provider_knowledge_base_id:
        return kb.provider_knowledge_base_id
    kb.sync_status = "provisioning"
    kb.sync_error = None
    await db.commit()
    try:
        remote_id = await client.create_knowledge_base(
            name=kb.name,
            description=kb.description or "",
        )
    except SmallestAIError as exc:
        await _mark_provider_error(db, kb, exc)
        raise _provider_error(exc) from exc
    kb.provider_knowledge_base_id = remote_id
    kb.sync_status = "processing" if kb.source_count else "local_only"
    kb.last_synced_at = datetime.now(UTC)
    # A remote resource now exists. Commit its mapping immediately so a later
    # scrape/upload failure cannot leave an orphaned provider knowledge base.
    await db.commit()
    return remote_id


def _recount(kb: KnowledgeBase) -> None:
    kb.source_count = len(kb.sources)
    kb.indexed_source_count = sum(source.status == "indexed" for source in kb.sources)
    if not kb.source_count:
        kb.sync_status = "local_only"
        kb.sync_error = None
    elif kb.indexed_source_count == kb.source_count:
        kb.sync_status = "ready"
        kb.sync_error = None
    elif any(source.status == "failed" for source in kb.sources):
        kb.sync_status = "error"
    elif any(source.status in {"pending", "processing"} for source in kb.sources):
        kb.sync_status = "processing"


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
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return raw.rstrip("/")
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


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
            source.status = "indexed"
            source.error_message = None
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
    return _knowledge_response(kb)


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
        details={"fields": sorted(updates)},
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


@router.post("/{kb_id}/sources/urls", response_model=KnowledgeBaseResponse)
async def add_url_sources(
    kb_id: UUID,
    data: UrlSourceCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    urls = list(dict.fromkeys(str(url) for url in data.urls))
    existing = {source.location for source in kb.sources if source.location}
    new_urls = [url for url in urls if url not in existing]
    if not new_urls:
        raise HTTPException(
            status_code=409, detail="Every selected URL is already in this knowledge base"
        )
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
    kb.sources.append(
        KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="text",
            name=data.name,
            content=data.content,
            size_bytes=len(data.content.encode()),
            status="local_only",
            source_metadata={"provider_note": "Awaiting provider text-ingestion support"},
        )
    )
    _recount(kb)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.text_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"name": data.name, "bytes": len(data.content.encode())},
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/sources/pdf", response_model=KnowledgeBaseResponse)
async def upload_pdf_source(
    kb_id: UUID,
    media: UploadFile = File(...),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    filename = PurePath(media.filename or "knowledge.pdf").name
    if media.content_type != "application/pdf" or not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF documents are supported")
    content = await media.read(MAX_KNOWLEDGE_PDF_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF must be 8 MB or smaller")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="The uploaded file is not a valid PDF")
    extracted_text = await asyncio.to_thread(_extract_pdf_text, content)
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    remote_id = await _ensure_remote(db, kb, client)
    try:
        await client.upload_knowledge_pdf(
            knowledge_base_id=remote_id,
            file_name=filename,
            content=content,
        )
    except SmallestAIError as exc:
        await _mark_provider_error(db, kb, exc)
        raise _provider_error(exc) from exc
    kb.sources.append(
        KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="file",
            name=filename,
            content=extracted_text,
            mime_type="application/pdf",
            size_bytes=len(content),
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
        action="knowledge_source.pdf_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={
            "name": filename,
            "bytes": len(content),
            "local_text_extracted": bool(extracted_text),
            "agents_requiring_sync": [str(agent_id) for agent_id in affected_agent_ids],
        },
    )
    return _knowledge_response(kb)


@router.delete("/{kb_id}/sources/{source_id}", response_model=KnowledgeBaseResponse)
async def delete_knowledge_source(
    kb_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Permanently remove a source from VAV and its Smallest knowledge base."""
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    _ensure_bound_agents_accept_knowledge_change(kb)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    provider_target: tuple[str, str] | None = None
    scraped: list[dict] = []
    items: list[dict] = []
    if kb.provider_knowledge_base_id and source.source_type != "text":
        client = await _tenant_smallest_client(db, current_user.tenant_id)
        try:
            scraped = await client.list_scraped_knowledge_urls(kb.provider_knowledge_base_id)
            items = await client.list_knowledge_items(kb.provider_knowledge_base_id)
            provider_target = _provider_source_delete_target(
                source,
                scraped=scraped,
                items=items,
            )
            if provider_target:
                collection, provider_item_id = provider_target
                if collection == "scraped":
                    await client.delete_scraped_knowledge_url(
                        knowledge_base_id=kb.provider_knowledge_base_id,
                        scraped_url_id=provider_item_id,
                    )
                else:
                    await client.delete_knowledge_item(
                        knowledge_base_id=kb.provider_knowledge_base_id,
                        item_id=provider_item_id,
                    )
        except SmallestAIError as exc:
            await _mark_provider_error(db, kb, exc)
            raise _provider_error(exc) from exc

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
    kb.sources[:] = [candidate for candidate in kb.sources if candidate.id not in removed_ids]
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
            "agents_requiring_sync": [str(agent_id) for agent_id in affected_agent_ids],
        },
    )
    return _knowledge_response(kb)


@router.post("/{kb_id}/refresh", response_model=KnowledgeBaseResponse)
async def refresh_knowledge_base(
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    if not kb.provider_knowledge_base_id:
        return _knowledge_response(kb)
    client = await _tenant_smallest_client(db, current_user.tenant_id)
    try:
        provider_knowledge_base = await client.get_knowledge_base(kb.provider_knowledge_base_id)
        scraped = await client.list_scraped_knowledge_urls(kb.provider_knowledge_base_id)
        items = await client.list_knowledge_items(kb.provider_knowledge_base_id)
    except SmallestAIError as exc:
        await _mark_provider_error(db, kb, exc)
        raise _provider_error(exc) from exc
    now = datetime.now(UTC)
    _reconcile_provider_sources(
        kb,
        scraped=scraped,
        items=items,
        provider_knowledge_base=provider_knowledge_base,
        now=now,
    )
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
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    if data.approved and (kb.sync_status != "ready" or not kb.indexed_source_count):
        raise HTTPException(
            status_code=409,
            detail="Index every provider source before approving this knowledge base",
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
        details={"approved": data.approved},
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
    if kb.approval_status != "approved" or not kb.provider_knowledge_base_id:
        raise HTTPException(
            status_code=409, detail="Approve and provision this knowledge base first"
        )
    agent = await db.scalar(
        select(Agent).where(Agent.id == data.agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
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
    if agent.voice_provider == "sarvam":
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
