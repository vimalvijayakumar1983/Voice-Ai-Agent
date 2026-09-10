from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import PurePath
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
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
    KnowledgeServingRevision,
    KnowledgeSource,
    KnowledgeSpeechLexicon,
)
from app.models.call import Call
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
    KnowledgeProcessingMode,
    KnowledgeReleaseReactivationRequest,
    KnowledgeServingRevisionResponse,
    KnowledgeSourceCompileRequest,
    KnowledgeSourcePreviewResponse,
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
from app.services.knowledge_compiler import (
    COMPILER_VERSION,
    CompiledKnowledge,
    KnowledgeCompilerError,
    compile_source_knowledge,
)
from app.services.knowledge_records import (
    coverage_blocks_approval,
    coverage_report,
    records_from_text,
    records_to_payload,
)
from app.services.knowledge_serving import (
    KnowledgeServingError,
    publish_serving_revision,
    validate_serving_revision_integrity,
)
from app.services.knowledge_sources import (
    KNOWLEDGE_PROVIDER,
    VAV_NATIVE_KNOWLEDGE_PROVIDERS,
    canonical_source_url,
    consolidate_duplicate_url_sources,
    has_searchable_content,
    invalidate_knowledge_approval,
    mark_native_bindings_live,
)
from app.services.pdf_ingestion import PdfIngestionError, PreparedPdf, prepare_pdf
from app.services.provider_credentials import ProviderCredentialError, load_provider_config
from app.services.rate_limit import enforce_rate_limit
from app.services.runtime_capacity import TERMINAL_CALL_STATUSES
from app.services.speech_lexicon import SpeechLexiconError, publish_speech_lexicon
from app.services.website_crawler import discover_sitemap_urls
from app.services.website_recovery import WebsiteRecoveryError

router = APIRouter(prefix="/knowledge", tags=["Knowledge Studio"])
logger = structlog.get_logger()
MAX_KNOWLEDGE_PDF_BYTES = 8 * 1024 * 1024
MAX_SITEMAP_URLS = 500


async def _compile_uploaded_content(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    request: Request,
    name: str,
    text: str,
    processing_mode: KnowledgeProcessingMode,
) -> CompiledKnowledge:
    """Compile before publication locks or remote uploads; failures leave drafts intact."""
    api_key = None
    if processing_mode != "fast":
        # One tenant-wide budget across text, PDF and in-place compilation;
        # changing routes or client addresses cannot multiply the allowance.
        await enforce_rate_limit(
            request,
            scope="knowledge-source-compile",
            limit=6,
            window_seconds=60,
            subject=str(tenant_id),
            bind_to_client=False,
            limit_detail="Too many knowledge compilations. Please retry in a minute.",
            unavailable_detail="Knowledge compilation is temporarily unavailable. Retry shortly.",
        )
        try:
            config = await load_provider_config(db, tenant_id, "openai")
        except ProviderCredentialError as exc:
            if processing_mode == "ai_verified":
                raise HTTPException(
                    status_code=503, detail="The OpenAI credential is unavailable."
                ) from exc
            config = None
        api_key = str((config or {}).get("api_key") or settings.openai_api_key).strip() or None
    # These routes have performed only reads so far. Release their connection
    # before slow provider inference; publication reacquires and rechecks later.
    await db.rollback()
    try:
        return await compile_source_knowledge(
            title=name,
            url="",
            text=text,
            requested_mode=processing_mode,
            api_key=api_key,
            require_structured_facts=True,
        )
    except KnowledgeCompilerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _apply_uploaded_compilation(
    source: KnowledgeSource,
    *,
    raw_text: str,
    compiled: CompiledKnowledge,
    records=None,
) -> None:
    # Records are the unit of coverage: what the source presented together
    # (a card, a table row, a field) must have been captured as verified facts.
    records = list(records) if records else records_from_text(raw_text)
    compiler = compiled.structured.get("compiler") or {}
    coverage = coverage_report(
        records,
        compiled.structured,
        requested_mode=str(compiler.get("requested_mode") or "automatic"),
        effective_mode=str(compiled.effective_mode or compiler.get("effective_mode") or "fast"),
    )
    source.raw_content = raw_text
    source.content = compiled.content
    source.structured_content = {**compiled.structured, "records": records_to_payload(records)}
    source.content_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    source.compiled_at = datetime.now(UTC)
    source.source_metadata = {
        **(source.source_metadata or {}),
        "compiler": compiler,
        "processing_mode": compiler.get("requested_mode"),
        "coverage": coverage,
        "record_count": len(records),
    }
    if source.source_metadata.get("upload_compile"):
        previous_job = source.source_metadata["upload_compile"]
        source.status = "indexed"
        source.error_message = None
        source.source_metadata = {
            **source.source_metadata,
            "upload_compile": {
                **previous_job,
                "status": "completed",
                "message": "Processing complete. Review facts before approval.",
            },
        }


async def _stage_background_compilation(db, kb, source, *, raw_text, mode, actor, request):
    from app.tasks.knowledge_compile_tasks import job_for, queue_source

    if job_for(source).get("status") in {"queued", "processing"}:
        return _knowledge_response(kb)
    await enforce_rate_limit(
        request,
        scope="knowledge-source-compile",
        limit=6,
        window_seconds=60,
        subject=str(actor.tenant_id),
        bind_to_client=False,
        limit_detail="Too many knowledge compilations. Please retry in a minute.",
        unavailable_detail="Knowledge compilation is temporarily unavailable. Retry shortly.",
    )
    source.raw_content = raw_text
    queue_source(source, actor_id=actor.id, mode=mode)
    invalidate_knowledge_approval(kb)
    _recount(kb)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.id,
        action="knowledge_source.compilation_queued",
        resource_type="knowledge_source",
        resource_id=str(source.id),
        details={"processing_mode": mode},
    )
    # The source row is the durable outbox. Beat dispatches it; no provider or
    # broker I/O can make this HTTP request wait for AI or lose the original.
    await db.commit()
    return _knowledge_response(kb)


def _source_response(
    source: KnowledgeSource, *, published_revision_id=None, owner_company=None, check=None
) -> KnowledgeSourceResponse:
    from app.services.knowledge_quality import (
        QUALITY_CHECK_VERSION,
        source_fingerprint,
        source_quality,
    )

    quality_status, quality_issues = source_quality(source)
    check = check or {}
    if (
        quality_status not in {"needs_repair", "processing"}
        and check.get("revision_id") == str(published_revision_id)
        and check.get("owner_company") == owner_company
        and check.get("source_fingerprint") == source_fingerprint(source)
        and check.get("status") == "passed"
        and check.get("checker_version") == QUALITY_CHECK_VERSION
    ):
        quality_status = "sample_checks_passed"
        quality_issues = [
            f"{check.get('checks_count', 0)} representative retrieval checks passed against "
            "the published company-scoped revision. This does not prove complete coverage."
        ]
    content = source.content if isinstance(source.content, str) else ""
    extracted = source.raw_content if isinstance(source.raw_content, str) else content
    response = KnowledgeSourceResponse.model_validate(source)
    return response.model_copy(
        update={
            "quality_status": quality_status,
            "quality_issues": quality_issues,
            "retrieval_ready": bool(content.strip()),
            "extracted_character_count": len(extracted.strip()),
            "company_fact_count": sum(
                1
                for fact in (source.structured_content or {}).get("facts", [])
                if isinstance(fact, dict)
                and all(
                    str(fact.get(field) or "").strip()
                    for field in ("subject", "predicate", "value", "evidence")
                )
            ),
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
        sync_status=kb.sync_status,
        sync_error=kb.sync_error,
        approval_status=kb.approval_status,
        scope_type=kb.scope_type,
        scope_label=kb.scope_label,
        owner_company=kb.owner_company,
        languages=kb.languages or ["en"],
        tags=kb.tags or [],
        source_count=kb.source_count,
        indexed_source_count=kb.indexed_source_count,
        last_synced_at=kb.last_synced_at,
        published_at=kb.published_at,
        speech_lexicon=speech_lexicon,
        serving_revision=serving_revision,
        has_pending_changes=kb.approval_status == "draft" and serving_revision is not None,
        sources=[
            _source_response(
                source,
                published_revision_id=kb.serving_revision_id,
                owner_company=kb.owner_company,
                check=(kb.readiness_report or {}).get(str(source.id)),
            )
            for source in kb.sources
        ],
        agent_bindings=bindings,
        crawls=[KnowledgeCrawlResponse.model_validate(crawl) for crawl in kb.crawls],
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def _mark_native_bindings_live(kb: KnowledgeBase) -> None:
    """VAV-native agents read the current knowledge directly; nothing to publish.

    Legacy bindings to agents on another voice provider are left untouched so
    they are never reported as synced with knowledge they cannot read.
    """
    mark_native_bindings_live(kb)


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


def _recount(kb: KnowledgeBase) -> None:
    # Legacy pasted-text rows were stored as local_only even though their content
    # is immediately searchable by VAV-native runtimes. Promote them lazily so an
    # existing workspace is repaired the next time it is governed or refreshed.
    for source in kb.sources:
        compilation_status = (
            (getattr(source, "source_metadata", None) or {}).get("upload_compile") or {}
        ).get("status")
        if compilation_status in {"queued", "processing", "failed"}:
            source.status = "failed" if compilation_status == "failed" else "processing"
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
        owner_company=data.owner_company,
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
            .execution_options(populate_existing=True)
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
        "owner_company",
        "languages",
        "tags",
    }
    approval_invalidated = False
    if updates.keys() & serving_fields:
        approval_invalidated = invalidate_knowledge_approval(kb)
    for key, value in updates.items():
        setattr(kb, key, value)
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


@router.post("/{kb_id}/sitemap/discover", response_model=SitemapDiscoveryResponse)
async def discover_sitemap(
    kb_id: UUID,
    data: SitemapDiscoveryRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """List the public page URLs declared by one sitemap, without indexing them."""
    await _get_knowledge_base(db, current_user.tenant_id, kb_id, for_update=False)
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
    from app.tasks.knowledge_tasks import _queue_repair_metadata

    queued: dict[UUID, str] = {}
    for url in new_urls:
        queued_metadata, repair_run_id, _ = _queue_repair_metadata(
            {"processing_mode": "automatic"},
            staged_refresh=False,
            message="Queued for VAV download, extraction and compilation.",
        )
        source = KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="url",
            name=url.rsplit("/", 1)[-1] or url,
            location=url,
            status="processing",
            source_metadata=queued_metadata,
        )
        kb.sources.append(source)
        await db.flush()
        queued[source.id] = repair_run_id
    _recount(kb)
    kb.last_synced_at = datetime.now(UTC)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.urls_added",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        details={"count": len(new_urls), "approval_invalidated": approval_invalidated},
    )
    # The worker must never race the request transaction that records the sources.
    await db.commit()

    from app.tasks.knowledge_tasks import _mark_failed, repair_website_source

    for source_id, repair_run_id in queued.items():
        try:
            repair_website_source.apply_async(
                args=[str(current_user.tenant_id), str(kb.id), str(source_id), repair_run_id],
                queue="knowledge",
            )
        except Exception:
            await _mark_failed(
                current_user.tenant_id,
                kb.id,
                source_id,
                message="The page-extraction worker is temporarily unavailable. Retry this page.",
                code="worker_unavailable",
                repair_run_id=repair_run_id,
            )
    refreshed = await _get_knowledge_base(db, current_user.tenant_id, kb.id, for_update=False)
    return _knowledge_response(refreshed)


@router.post("/{kb_id}/sources/text", response_model=KnowledgeBaseResponse)
async def add_text_source(
    kb_id: UUID,
    data: TextSourceCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    for existing in kb.sources:
        metadata = (existing.structured_content or {}).get("compiler") or {}
        if (
            existing.source_type == "text"
            and existing.status == "indexed"
            and existing.name.casefold() == data.name.casefold()
            and existing.raw_content == data.content
            and metadata.get("version") == COMPILER_VERSION
            and metadata.get("requested_mode") == data.processing_mode
            and not metadata.get("warning")
        ):
            return _knowledge_response(kb)
    from app.tasks.knowledge_compile_tasks import job_for

    existing = next(
        (
            s
            for s in kb.sources
            if s.source_type == "text"
            and s.name.casefold() == data.name.casefold()
            and (s.raw_content or s.content) == data.content
        ),
        None,
    )
    if existing is not None and job_for(existing).get("status") in {"queued", "processing"}:
        return _knowledge_response(kb)
    if data.processing_mode != "fast":
        if existing is None:
            existing = KnowledgeSource(
                tenant_id=current_user.tenant_id,
                source_type="text",
                name=data.name,
                size_bytes=len(data.content.encode()),
                status="pending",
                source_metadata={"retrieval_content_source": "vav_text"},
            )
            kb.sources.append(existing)
        return await _stage_background_compilation(
            db,
            kb,
            existing,
            raw_text=data.content,
            mode=data.processing_mode,
            actor=current_user,
            request=request,
        )
    compiled = await _compile_uploaded_content(
        db,
        current_user.tenant_id,
        request=request,
        name=data.name,
        text=data.content,
        processing_mode=data.processing_mode,
    )
    # Recheck authorization/bindings after external inference, under the same
    # publication barrier used by approval. Never hold that lock during inference.
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    approval_invalidated = invalidate_knowledge_approval(kb)
    # Retries/double-clicks must not create a second copy of an identical source.
    # Different content with the same name is not silently overwritten.
    source = next(
        (
            item
            for item in kb.sources
            if item.source_type == "text"
            and item.name.casefold() == data.name.casefold()
            and (item.raw_content or (item.content if not item.structured_content else None))
            == data.content
        ),
        None,
    )
    if source is None:
        source = KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="text",
            name=data.name,
            size_bytes=len(data.content.encode()),
            status="indexed",
            source_metadata={"retrieval_content_source": "vav_text"},
        )
        kb.sources.append(source)
    _apply_uploaded_compilation(source, raw_text=data.content, compiled=compiled)
    _recount(kb)
    _mark_native_bindings_live(kb)
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
        },
    )
    return _knowledge_response(kb)


@router.get(
    "/{kb_id}/sources/{source_id}/preview",
    response_model=KnowledgeSourcePreviewResponse,
)
async def preview_knowledge_source(
    kb_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id, for_update=False)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return KnowledgeSourcePreviewResponse(
        source_id=source.id,
        raw_text=source.raw_content or (source.content if not source.structured_content else None),
        structured_content=source.structured_content or {},
        compiled_at=source.compiled_at,
    )


@router.post(
    "/{kb_id}/sources/{source_id}/compile",
    response_model=KnowledgeBaseResponse,
)
async def compile_existing_uploaded_source(
    kb_id: UUID,
    source_id: UUID,
    data: KnowledgeSourceCompileRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Upgrade an existing PDF/text draft in place, never its approved snapshot.

    The original extracted text is retained. This operation only structures
    VAV's retrieval representation.
    """
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    if source.source_type not in {"file", "text"}:
        raise HTTPException(status_code=422, detail="Use Refresh page for website sources.")
    # Legacy local sources stored extracted text directly in content. Never
    # recursively compile an already generated document if its original is lost.
    raw_text = source.raw_content or (source.content if not source.structured_content else None)
    if not raw_text or not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail="No extracted original text is available. Re-upload the PDF or add its text.",
        )
    from app.tasks.knowledge_compile_tasks import job_for

    if job_for(source).get("status") in {"queued", "processing"}:
        return _knowledge_response(kb)
    previous_updated_at = source.updated_at
    source_name = source.name
    compiler = (source.structured_content or {}).get("compiler") or {}
    if (
        source.content_sha256 == hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        and source.status == "indexed"
        and source.raw_content
        and source.content
        and compiler.get("version") == COMPILER_VERSION
        and compiler.get("requested_mode") == data.processing_mode
        and not compiler.get("warning")
    ):
        return _knowledge_response(kb)
    if data.processing_mode != "fast":
        return await _stage_background_compilation(
            db,
            kb,
            source,
            raw_text=raw_text,
            mode=data.processing_mode,
            actor=current_user,
            request=request,
        )
    compiled = await _compile_uploaded_content(
        db,
        current_user.tenant_id,
        request=request,
        name=source_name,
        text=raw_text,
        processing_mode=data.processing_mode,
    )
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None or source.updated_at != previous_updated_at:
        raise HTTPException(status_code=409, detail="The source changed. Refresh and retry.")
    approval_invalidated = invalidate_knowledge_approval(kb)
    _apply_uploaded_compilation(source, raw_text=raw_text, compiled=compiled)
    _mark_native_bindings_live(kb)
    _recount(kb)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="knowledge_source.compiled",
        resource_type="knowledge_source",
        resource_id=str(source.id),
        details={
            "processing_mode": data.processing_mode,
            "compiler": compiled.structured.get("compiler"),
            "approval_invalidated": approval_invalidated,
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
    from app.tasks.knowledge_tasks import _queue_repair_metadata

    queued_metadata, repair_run_id, attempts = _queue_repair_metadata(
        metadata,
        staged_refresh=staged_refresh,
        message="VAV queued safe download, extraction, compilation and indexing.",
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
    request: Request,
    media: UploadFile = File(...),
    processing_mode: KnowledgeProcessingMode = Form("automatic"),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    # PDF extraction can be expensive; do not hold the KB publication barrier
    # while OCR or compilation runs. Preconditions are rechecked under the
    # lock immediately before the durable state transition below.
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id, for_update=False)
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

    compiled = await _compile_uploaded_content(
        db,
        current_user.tenant_id,
        request=request,
        name=filename,
        text=prepared.extracted_text,
        processing_mode=processing_mode,
    )

    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    existing_source = next(
        (
            source
            for source in kb.sources
            if source.source_type == "file" and source.name.casefold() == filename.casefold()
        ),
        None,
    )
    approval_invalidated = invalidate_knowledge_approval(kb)
    source = existing_source
    if source is None:
        source = KnowledgeSource(
            tenant_id=current_user.tenant_id,
            source_type="file",
            name=filename,
        )
        kb.sources.append(source)
    source.name = filename
    source.file_content = content
    source.mime_type = "application/pdf"
    source.size_bytes = len(content)
    source.status = "indexed"
    source.provider_item_id = None
    source.error_message = None
    source.source_metadata = {
        "retrieval_content_source": "vav_pdf_ingestion",
        "extraction_method": prepared.extraction_method,
        "page_count": prepared.page_count,
        "ocr_page_count": prepared.ocr_page_count,
        "sha256": prepared.sha256,
    }
    _apply_uploaded_compilation(
        source,
        raw_text=prepared.extracted_text,
        compiled=compiled,
        records=prepared.records,
    )
    source.last_synced_at = datetime.now(UTC)
    _recount(kb)
    kb.last_synced_at = datetime.now(UTC)
    _mark_native_bindings_live(kb)
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
            "replaced_existing": existing_source is not None,
            "approval_invalidated": approval_invalidated,
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
    """Stage source removal by deleting its mutable draft copy.

    Immutable VAV releases are retained for audit and for calls already pinned
    to them.  If a live release exists, new VAV calls keep using it until the
    edited draft is approved or an operator explicitly revokes approval.
    """
    kb = await _get_knowledge_base(db, current_user.tenant_id, kb_id)
    source = next((item for item in kb.sources if item.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    approval_invalidated = invalidate_knowledge_approval(kb)
    kb.sources.remove(source)
    await db.delete(source)
    _recount(kb)
    _mark_native_bindings_live(kb)
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
            "live_release_retained": kb.serving_revision_id is not None,
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
    if data.approved and (kb.sync_status != "ready" or not kb.indexed_source_count):
        raise HTTPException(
            status_code=409,
            detail="Make every source VAV-searchable before approving this knowledge base",
        )
    partial_sources: list[str] = []
    if data.approved:
        blocked, partial_sources = _coverage_blockers(kb)
        if blocked:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Recompile these sources so VAV can verify and measure them before "
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
            "accepted_partial_coverage": bool(data.approved and partial_sources),
            "partial_coverage_sources": partial_sources[:20],
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
    if agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Only VAV-native agents retrieve VAV knowledge. Switch this agent to a "
                "VAV runtime (Inworld, Sarvam or ElevenLabs) before binding knowledge."
            ),
        )
    has_live_native_release = kb.serving_revision_id is not None
    if kb.approval_status != "approved" and not has_live_native_release:
        raise HTTPException(status_code=409, detail="Approve this knowledge base first")
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
