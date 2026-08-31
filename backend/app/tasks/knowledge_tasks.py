"""Background repair tasks for website knowledge sources."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import async_session_factory
from app.models.agent import AgentKnowledgeBinding, KnowledgeBase
from app.providers.smallest import SmallestAIClient, SmallestAIError, get_smallest_client
from app.services.audit import record_audit_event
from app.services.provider_credentials import load_provider_config
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
from app.tasks.worker import celery_app

logger = structlog.get_logger()
PROVIDER_READY = {"complete", "completed", "indexed", "processed", "ready", "success", "succeeded"}
PROVIDER_FAILED = {"error", "failed", "failure"}


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
    provider_item_id: str,
    artifact_name: str,
) -> None:
    """Do not report success until the provider item itself is indexed."""
    for attempt in range(30):
        items = await provider.list_knowledge_items(knowledge_base_id)
        item = next(
            (
                candidate
                for candidate in items
                if str(candidate.get("_id") or candidate.get("id") or "") == provider_item_id
                or _provider_file_name(candidate) == artifact_name
            ),
            None,
        )
        if item is not None:
            provider_state = _provider_status(item)
            if provider_state in PROVIDER_READY:
                return
            if provider_state in PROVIDER_FAILED:
                raise WebsiteRecoveryError(
                    "The provider rejected the recovered searchable document.",
                    code="provider_indexing_failed",
                )
        if attempt < 29:
            await asyncio.sleep(2)
    raise WebsiteRecoveryError(
        "The recovered page is still waiting for provider indexing.",
        code="provider_indexing_timeout",
        retryable=True,
    )


def _recount(knowledge_base: KnowledgeBase) -> None:
    knowledge_base.source_count = len(knowledge_base.sources)
    knowledge_base.indexed_source_count = sum(
        source.status == "indexed" and bool(str(source.content or "").strip())
        for source in knowledge_base.sources
    )
    if knowledge_base.indexed_source_count == knowledge_base.source_count:
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
    source = next(
        (candidate for candidate in knowledge_base.sources if candidate.id == source_id),
        None,
    ) if knowledge_base else None
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

        artifact_name = f"vav-web-recovery-{source.id}.pdf"
        existing_items = await provider.list_knowledge_items(remote_id)
        upload = await provider.upload_knowledge_pdf(
            knowledge_base_id=remote_id,
            file_name=artifact_name,
            content=provider_document,
        )
        provider_item_id = _provider_item_id(upload)
        if not provider_item_id:
            raise WebsiteRecoveryError(
                "The provider accepted the recovered page but returned no index item.",
                code="provider_response_invalid",
                retryable=True,
            )

        await _set_stage(
            tenant_id,
            kb_id,
            source_id,
            "verifying",
            "The searchable document was accepted. VAV is verifying provider indexing.",
        )
        await _wait_for_provider_index(
            provider,
            knowledge_base_id=remote_id,
            provider_item_id=provider_item_id,
            artifact_name=artifact_name,
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
            if agent.voice_provider == "sarvam":
                binding.provider = "sarvam"
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

        for item in existing_items:
            old_id = str(item.get("_id") or item.get("id") or "")
            if (
                old_id
                and old_id != provider_item_id
                and _provider_file_name(item) == artifact_name
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
