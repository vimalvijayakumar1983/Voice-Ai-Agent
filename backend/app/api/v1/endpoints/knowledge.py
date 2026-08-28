from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePath
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
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

router = APIRouter(prefix="/knowledge", tags=["Knowledge Studio"])
MAX_KNOWLEDGE_PDF_BYTES = 8 * 1024 * 1024
PROVIDER_INDEXED_STATUSES = {
    "complete",
    "completed",
    "indexed",
    "processed",
    "ready",
    "success",
    "succeeded",
}
PROVIDER_FAILED_STATUSES = {"error", "failed", "failure"}


def _provider_error(exc: SmallestAIError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _source_response(source: KnowledgeSource) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse.model_validate(source)


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
    if kb.source_count and kb.indexed_source_count == kb.source_count:
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


def _provider_file_name(item: dict) -> str:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(item.get("fileName") or metadata.get("fileName") or "")


def _reconcile_provider_sources(
    kb: KnowledgeBase,
    *,
    scraped: list[dict],
    items: list[dict],
    provider_knowledge_base: dict,
    now: datetime,
) -> None:
    provider_items = [*scraped, *items]
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
            await get_smallest_client().update_knowledge_base(
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
    await _ensure_remote(db, kb, get_smallest_client())
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
    client = get_smallest_client()
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
    urls = list(dict.fromkeys(str(url) for url in data.urls))
    existing = {source.location for source in kb.sources if source.location}
    new_urls = [url for url in urls if url not in existing]
    if not new_urls:
        raise HTTPException(
            status_code=409, detail="Every selected URL is already in this knowledge base"
        )
    client = get_smallest_client()
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
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.urls_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"count": len(new_urls)},
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
    filename = PurePath(media.filename or "knowledge.pdf").name
    if media.content_type != "application/pdf" or not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF documents are supported")
    content = await media.read(MAX_KNOWLEDGE_PDF_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF must be 8 MB or smaller")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="The uploaded file is not a valid PDF")
    client = get_smallest_client()
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
            mime_type="application/pdf",
            size_bytes=len(content),
            status="processing",
        )
    )
    _recount(kb)
    kb.last_synced_at = datetime.now(UTC)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.pdf_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"name": filename, "bytes": len(content)},
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
    client = get_smallest_client()
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
        binding.sync_status = "pending"
    else:
        db.add(
            AgentKnowledgeBinding(
                tenant_id=current_user.tenant_id,
                agent_id=agent.id,
                knowledge_base_id=kb.id,
                provider=agent.voice_provider,
                sync_status="pending",
            )
        )
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
            await get_smallest_client().delete_knowledge_base(kb.provider_knowledge_base_id)
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
