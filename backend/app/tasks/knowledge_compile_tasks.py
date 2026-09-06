"""Durable source compilation outbox; HTTP requests never wait for AI inference."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import KnowledgeSource
from app.models.user import User
from app.services.knowledge_compiler import compile_source_knowledge
from app.services.knowledge_sources import VAV_NATIVE_KNOWLEDGE_PROVIDERS
from app.services.provider_credentials import ProviderCredentialError, load_provider_config
from app.tasks.async_runner import run_async
from app.tasks.worker import celery_app

logger = structlog.get_logger()
KEY = "upload_compile"


def job_for(source):
    return dict((source.source_metadata or {}).get(KEY) or {})


def set_job(source, job):
    source.source_metadata = {**(source.source_metadata or {}), KEY: job}


def queue_source(source, *, actor_id, mode):
    previous = job_for(source)
    set_job(
        source,
        {
            "run_id": str(uuid4()),
            "status": "queued",
            "mode": mode,
            "actor_id": str(actor_id),
            "queued_at": datetime.now(UTC).isoformat(),
            "provider_status": previous.get("provider_status", source.status)
            if previous.get("status") == "failed"
            else source.status,
            "message": "Original saved. Waiting for background AI processing.",
        },
    )
    source.status = "processing"
    source.error_message = None


async def _authorized(db, kb, job):
    from app.api.v1.endpoints.knowledge import _ensure_bound_agents_accept_knowledge_change

    actor = await db.get(User, UUID(job["actor_id"]))
    if not actor or actor.tenant_id != kb.tenant_id or not actor.is_active:
        raise ValueError("Source editor no longer has access.")
    if actor.role not in {"owner", "admin", "member"}:
        raise ValueError("Source editor no longer has edit permission.")
    _ensure_bound_agents_accept_knowledge_change(kb)
    if any(b.agent.voice_provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS for b in kb.agent_bindings):
        raise ValueError("Review agent bindings before compiling this source.")


async def _load(db, tenant_id, kb_id, source_id):
    from app.api.v1.endpoints.knowledge import _get_knowledge_base

    try:
        kb = await _get_knowledge_base(db, UUID(tenant_id), UUID(kb_id))
    except HTTPException as exc:
        if exc.status_code == 404:
            return None, None
        raise
    source = next((s for s in kb.sources if str(s.id) == source_id), None)
    return kb, source


async def _finish(tenant_id, kb_id, source_id, run_id, compiled=None, error=None):
    from app.api.v1.endpoints.knowledge import (
        _apply_uploaded_compilation,
        _invalidate_bound_agent_deployments,
        _recount,
    )
    from app.services.audit import record_audit_event
    from app.services.knowledge_sources import invalidate_knowledge_approval

    async with async_session_factory() as db:
        kb, source = await _load(db, tenant_id, kb_id, source_id)
        if source is None:
            return
        job = job_for(source)
        if job.get("run_id") != run_id or job.get("status") != "processing":
            return  # Deleted, replaced, timed out, or a newer retry owns the draft.
        if compiled is not None:
            try:
                await _authorized(db, kb, job)
            except (ValueError, HTTPException):
                error = "Permissions or agent bindings changed. Review them and retry."
                compiled = None
        if compiled is None:
            source.status = "failed"
            source.error_message = (
                error or "AI processing failed. Original saved; retry processing."
            )
            job.update(status="failed", message=source.error_message)
        else:
            _apply_uploaded_compilation(source, raw_text=source.raw_content, compiled=compiled)
            source.status = "indexed" if source.source_type == "text" else job["provider_status"]
            source.error_message = None
            job.update(
                status="completed", message="Processing complete. Review facts before approval."
            )
            invalidate_knowledge_approval(kb)
            _invalidate_bound_agent_deployments(kb)
        job["finished_at"] = datetime.now(UTC).isoformat()
        set_job(source, job)
        _recount(kb)
        await record_audit_event(
            db,
            tenant_id=kb.tenant_id,
            actor_user_id=None,
            action="knowledge_source.background_compilation_" + job["status"],
            resource_type="knowledge_source",
            resource_id=source_id,
            details={"run_id": run_id, "mode": job["mode"]},
        )
        await db.commit()


async def _compile(tenant_id, kb_id, source_id, run_id):
    try:
        async with async_session_factory() as db:
            kb, source = await _load(db, tenant_id, kb_id, source_id)
            if source is None:
                return
            job = job_for(source)
            if job.get("run_id") != run_id or job.get("status") != "queued":
                return
            job.update(
                status="processing",
                started_at=datetime.now(UTC).isoformat(),
                message="Structuring and validating source-backed facts…",
            )
            set_job(source, job)
            text, title = source.raw_content, source.name
            await db.commit()  # Claim before inference; duplicate deliveries do no paid work.
            await _authorized(db, kb, job)
            try:
                config = await load_provider_config(db, kb.tenant_id, "openai")
            except ProviderCredentialError:
                if job["mode"] == "ai_verified":
                    raise
                config = None
            api_key = str((config or {}).get("api_key") or settings.openai_api_key).strip() or None
            await db.rollback()  # Do not hold a DB connection or publication lock during AI.
        compiled = await compile_source_knowledge(
            title=title,
            url="",
            text=text,
            requested_mode=job["mode"],
            api_key=api_key,
            require_structured_facts=True,
        )
        await _finish(tenant_id, kb_id, source_id, run_id, compiled=compiled)
    except Exception:
        # Never expose provider response bodies, credentials or uploaded text in logs/UI.
        logger.warning("knowledge_compilation_failed", source_id=source_id, run_id=run_id)
        await _finish(
            tenant_id,
            kb_id,
            source_id,
            run_id,
            error="AI processing could not complete. Your original is saved. "
            "Check the OpenAI connection and retry processing.",
        )


@celery_app.task(
    name="app.tasks.knowledge_compile_tasks.compile_upload", soft_time_limit=240, time_limit=270
)
def compile_upload(tenant_id, kb_id, source_id, run_id):
    run_async(_compile(tenant_id, kb_id, source_id, run_id))


async def _sweep():
    from app.api.v1.endpoints.knowledge import _recount

    now = datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(
                    KnowledgeSource.tenant_id,
                    KnowledgeSource.knowledge_base_id,
                    KnowledgeSource.id,
                )
                .where(
                    KnowledgeSource.source_metadata[KEY]["status"]
                    .as_string()
                    .in_(["queued", "processing"])
                )
                .order_by(KnowledgeSource.updated_at)
                .limit(100)
            )
        ).all()
    for tenant_id, kb_id, source_id in rows:
        args = tuple(map(str, (tenant_id, kb_id, source_id)))
        async with async_session_factory() as db:
            kb, source = await _load(db, *args)
            if source is None:
                continue
            job = job_for(source)
            state = job.get("status")
            if state not in {"queued", "processing"}:
                continue
            started = datetime.fromisoformat(job.get("started_at") or job["queued_at"])
            if now - started > timedelta(minutes=10 if state == "queued" else 5):
                source.status = "failed"
                source.error_message = (
                    "Background processing timed out. Original saved; retry processing."
                )
                job.update(status="failed", message=source.error_message)
                set_job(source, job)
                _recount(kb)
                await db.commit()
                continue
            if state == "processing":
                continue
            last = datetime.fromisoformat(job.get("enqueued_at") or job["queued_at"])
            if job.get("enqueued_at") and now - last < timedelta(seconds=60):
                continue
            job["enqueued_at"] = now.isoformat()
            set_job(source, job)
            await db.commit()  # Durable outbox retries broker outages/lost deliveries.
            try:
                await asyncio.to_thread(
                    compile_upload.apply_async,
                    args=[*args, job["run_id"]],
                    retry=False,
                )
            except Exception:
                logger.warning("knowledge_compile_enqueue_deferred", source_id=str(source_id))


@celery_app.task(name="app.tasks.knowledge_compile_tasks.sweep_uploads")
def sweep_uploads():
    run_async(_sweep())
