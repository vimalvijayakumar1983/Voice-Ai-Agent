"""Agent builder endpoints - CRUD for AI voice agents."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent, KnowledgeBase
from app.models.call import Call
from app.models.campaign import Campaign, CampaignContactAttempt
from app.providers.smallest import SmallestAIError, get_smallest_client
from app.schemas.agent import (
    AgentCreate,
    AgentProviderCatalog,
    AgentResponse,
    AgentUpdate,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    SmallestProviderResolution,
    SmallestSessionRequest,
    SmallestSessionResponse,
)
from app.services.agent_catalog import (
    AGENT_TEMPLATES,
    PRO_VOICE_IDS,
    language_catalog,
    normalize_voices,
    unsupported_voice_languages,
    voice_synthesizer_model,
)
from app.services.audit import record_audit_event

router = APIRouter(prefix="/agents", tags=["Agents"])

SMALLEST_SYNC_FIELDS = {
    "name",
    "system_prompt",
    "greeting_message",
    "model_name",
    "voice_id",
    "language",
    "supported_languages",
    "speech_rate",
    "timezone",
}

UNRESOLVED_PROVIDER_STATES = frozenset(
    {
        "provisioning",
        "provision_unknown",
        "publishing",
        "provider_scanning",
        "publish_unknown",
    }
)
RECONCILABLE_PUBLISH_STATES = frozenset({"publishing", "provider_scanning", "publish_unknown"})
PROVIDER_ACTIVE_STATES = frozenset(
    {"active", "pending", "queued", "running", "scanning", "processing", "in_progress"}
)
PROVIDER_FAILED_STATES = frozenset(
    {"failed", "error", "errored", "rejected", "blocked", "cancelled", "canceled"}
)
PROVIDER_OPERATION_LEASE = timedelta(minutes=2)
TERMINAL_CALL_STATUSES = frozenset(
    {"completed", "failed", "busy", "no_answer", "canceled", "cancelled"}
)
TERMINAL_CAMPAIGN_ATTEMPT_STATES = frozenset({"completed", "failed", "rejected", "cancelled"})


def _provider_config(agent: Agent) -> dict:
    return dict(agent.provider_config or {})


def _set_provider_operation(agent: Agent, operation: str, **values) -> None:
    config = _provider_config(agent)
    current = dict(config.get(operation) or {})
    current.update(values)
    config[operation] = current
    agent.provider_config = config


def _replace_provider_operation(agent: Agent, operation: str, **values) -> None:
    config = _provider_config(agent)
    config[operation] = values
    agent.provider_config = config


def _publish_operation(agent: Agent) -> dict:
    config = agent.provider_config or {}
    operation = config.get("publish") if isinstance(config, dict) else None
    return operation if isinstance(operation, dict) else {}


def _provision_operation(agent: Agent) -> dict:
    config = agent.provider_config or {}
    operation = config.get("provision") if isinstance(config, dict) else None
    return operation if isinstance(operation, dict) else {}


def _expire_stale_provisioning(agent: Agent) -> bool:
    """Fail closed when a create worker disappeared after its durable lease."""
    if agent.sync_status != "provisioning" or _lease_active(_provision_operation(agent)):
        return False
    agent.sync_status = "provision_unknown"
    _set_provider_operation(
        agent,
        "provision",
        phase="provision_unknown",
        lease_expires_at=None,
        last_error=(
            "Provisioning lease expired; the remote create outcome requires reconciliation"
        ),
        last_checked_at=datetime.now(UTC).isoformat(),
    )
    return True


def _publish_reconciliation_is_current(agent: Agent, operation_id: str | None) -> bool:
    if agent.sync_status not in RECONCILABLE_PUBLISH_STATES:
        return False
    current_id = _publish_operation(agent).get("id")
    return operation_id is None or current_id == operation_id


def _lease_active(operation: dict) -> bool:
    raw_expiry = operation.get("lease_expires_at")
    if not isinstance(raw_expiry, str):
        return False
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expiry > datetime.now(UTC)


def _revision_id(revision: dict | None) -> str | None:
    if not isinstance(revision, dict):
        return None
    value = revision.get("_id") or revision.get("id") or revision.get("revisionId")
    return str(value) if value else None


def _revision_lifecycle(revision: dict) -> tuple[str, str]:
    revision_status = str(revision.get("status") or revision.get("state") or "unknown").lower()
    security = revision.get("securityCheck") or revision.get("security_check") or {}
    if isinstance(security, dict):
        security_status = str(security.get("status") or security.get("state") or "unknown").lower()
    else:
        security_status = str(security or "unknown").lower()
    return revision_status, security_status


def _pending_publish_state(draft: dict | None) -> str | None:
    pending_publish = _pending_publish_value(draft)
    if not isinstance(pending_publish, dict):
        return None
    state = pending_publish.get("state") or pending_publish.get("status")
    return str(state).strip().lower() if state else None


def _pending_publish_value(draft: dict | None):
    if not isinstance(draft, dict):
        return None
    if "pendingPublish" in draft:
        return draft["pendingPublish"]
    return draft.get("pending_publish")


def _pending_publish_is_authoritatively_absent(draft: dict | None) -> bool:
    if draft is None:
        return True
    if not isinstance(draft, dict):
        return False
    if "pendingPublish" in draft:
        return draft["pendingPublish"] is None
    if "pending_publish" in draft:
        return draft["pending_publish"] is None
    return False


def _revision_label(revision: dict | None) -> str | None:
    if not isinstance(revision, dict):
        return None
    label = revision.get("label")
    return str(label) if label is not None else None


def _publish_kwargs(agent: Agent, voice_model: str | None) -> dict:
    return {
        "global_prompt": agent.system_prompt,
        "first_message": agent.greeting_message,
        "slm_model": agent.model_name,
        "language": agent.language,
        "supported_languages": list(agent.supported_languages),
        "timezone": agent.timezone,
        "voice_id": agent.voice_id,
        "speech_rate": agent.speech_rate,
        "synthesizer_model": voice_model,
    }


async def _tenant_agent(
    db: AsyncSession,
    agent_id: UUID,
    tenant_id: UUID,
    *,
    for_update: bool = False,
) -> Agent:
    query = select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = await db.execute(query)
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _require_public_voice(
    client,
    voice_id: str,
    selected_languages: list[str],
) -> str | None:
    """Reject private/cloned voice IDs until tenant entitlements exist."""
    if not voice_id:
        return None
    try:
        provider_voices = await client.list_voices()
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    public_voice_ids = {
        str(voice.get("voiceId") or voice.get("id"))
        for voice in provider_voices
        if voice.get("voiceId") or voice.get("id")
    }
    if voice_id not in public_voice_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                "Voice is not available in the shared public catalog. "
                "Private voice clones require a tenant-owned entitlement."
            ),
        )
    unsupported = unsupported_voice_languages(provider_voices, voice_id, selected_languages)
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail=(
                "Selected voice does not support these agent languages: " + ", ".join(unsupported)
            ),
        )
    selected_voice = next(
        voice
        for voice in provider_voices
        if str(voice.get("voiceId") or voice.get("id")) == voice_id
    )
    voice_model = voice_synthesizer_model(selected_voice)
    if not voice_model:
        is_pro_voice = voice_id.strip().casefold() in PRO_VOICE_IDS
        raise HTTPException(
            status_code=422,
            detail=(
                "Selected Pro voice is catalog-visible but Smallest.ai has not documented an "
                "Atoms-compatible synthesizer model"
                if is_pro_voice
                else "Selected voice has no compatible Smallest.ai synthesizer model"
            ),
        )
    return voice_model


async def _mark_publish_failure(
    db: AsyncSession,
    *,
    agent_id: UUID,
    tenant_id: UUID,
    operation_id: str,
    sync_status: str,
    error: SmallestAIError,
) -> Agent:
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    operation = _publish_operation(agent)
    if operation.get("id") == operation_id and agent.sync_status == "publishing":
        agent.sync_status = sync_status
        _set_provider_operation(
            agent,
            "publish",
            phase=sync_status,
            lease_expires_at=None,
            last_error=str(error),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
    return agent


async def _reconcile_smallest_publish(
    db: AsyncSession,
    *,
    agent_id: UUID,
    tenant_id: UUID,
    client,
) -> Agent:
    """Observe Smallest revision state without ever replaying a publish request."""
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    if agent.sync_status not in RECONCILABLE_PUBLISH_STATES:
        return agent
    if not agent.provider_agent_id:
        raise HTTPException(
            status_code=409,
            detail="Provider mapping is incomplete and requires operator reconciliation",
        )

    operation = _publish_operation(agent)
    operation_id = operation.get("id")
    if agent.sync_status == "publishing" and _lease_active(operation):
        raise HTTPException(
            status_code=409,
            detail="Agent publishing is still in progress. Check its status shortly.",
        )

    # A crash during branch lookup or draft update happened before the publish
    # request could be attempted. Make the local draft retryable, but never
    # perform that retry in the same reconciliation request.
    if agent.sync_status == "publishing" and operation.get("phase") in {
        "branch_lookup",
        "draft_update",
    }:
        agent.sync_status = "dirty"
        _set_provider_operation(
            agent,
            "publish",
            phase="interrupted_before_publish",
            lease_expires_at=None,
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        return agent

    if not agent.provider_branch_id:
        raise HTTPException(
            status_code=409,
            detail="Provider branch mapping is incomplete and requires operator reconciliation",
        )

    status_before = agent.sync_status
    provider_agent_id = agent.provider_agent_id
    branch_id = agent.provider_branch_id
    baseline_known = "baseline_revision_id" in operation
    baseline_revision_id = operation.get("baseline_revision_id")
    await db.commit()

    try:
        latest = await client.get_latest_branch_revision(
            agent_id=provider_agent_id,
            branch_id=branch_id,
        )
    except SmallestAIError as exc:
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        if agent.sync_status == "publishing":
            agent.sync_status = "publish_unknown"
        _set_provider_operation(
            agent,
            "publish",
            phase=agent.sync_status,
            lease_expires_at=None,
            last_error=str(exc),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    latest_revision_id = _revision_id(latest)
    if latest is not None and not latest_revision_id:
        exc = SmallestAIError("Smallest.ai returned a revision without an ID.")
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        agent.sync_status = (
            "provider_scanning" if status_before == "provider_scanning" else "publish_unknown"
        )
        _set_provider_operation(
            agent,
            "publish",
            phase=agent.sync_status,
            lease_expires_at=None,
            last_error=str(exc),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # A legacy/crash-created unresolved row with no baseline cannot prove that
    # the latest revision belongs to this publish attempt. Keep it fail closed.
    if status_before in {"publishing", "publish_unknown"} and not baseline_known:
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        agent.sync_status = "publish_unknown"
        _set_provider_operation(
            agent,
            "publish",
            phase="manual_reconciliation_required",
            lease_expires_at=None,
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        return agent

    if latest_revision_id is None or latest_revision_id == baseline_revision_id:
        try:
            draft = await client.get_open_branch_draft(
                agent_id=provider_agent_id,
                branch_id=branch_id,
            )
        except SmallestAIError as exc:
            agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
            if not _publish_reconciliation_is_current(agent, operation_id):
                return agent
            if agent.sync_status == "publishing":
                agent.sync_status = "publish_unknown"
            _set_provider_operation(
                agent,
                "publish",
                phase=agent.sync_status,
                lease_expires_at=None,
                last_error=str(exc),
                last_checked_at=datetime.now(UTC).isoformat(),
            )
            await db.commit()
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        pending_state = _pending_publish_state(draft)
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        if pending_state in PROVIDER_ACTIVE_STATES:
            agent.sync_status = "provider_scanning"
            phase = "provider_scanning"
            last_error = None
        elif pending_state in PROVIDER_FAILED_STATES:
            agent.sync_status = "error"
            phase = "publish_failed"
            last_error = (
                f"Smallest.ai draft publish failed with state '{pending_state}'. "
                "Review the provider draft and security result, correct the issue, then sync again."
            )
        else:
            agent.sync_status = "publish_unknown"
            phase = "publish_unknown"
            last_error = (
                "Smallest.ai reports no active draft publish and no matching committed revision. "
                "Confirm the provider outcome before retrying."
            )
        _set_provider_operation(
            agent,
            "publish",
            phase=phase,
            lease_expires_at=None,
            provider_state=pending_state,
            last_error=last_error,
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        return agent

    try:
        revision = await client.get_branch_revision(
            agent_id=provider_agent_id,
            branch_id=branch_id,
            revision_id=latest_revision_id,
        )
    except SmallestAIError as exc:
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        expected_label = operation.get("label")
        summary_label = _revision_label(latest)
        if isinstance(expected_label, str) and expected_label and summary_label != expected_label:
            agent.sync_status = "publish_unknown"
            _set_provider_operation(
                agent,
                "publish",
                phase="revision_label_mismatch",
                lease_expires_at=None,
                observed_revision_id=latest_revision_id,
                observed_revision_label=summary_label,
                last_error=(
                    "The newest Smallest.ai revision is missing this operation's exact label. "
                    "Confirm the provider revision before retrying."
                ),
                last_checked_at=datetime.now(UTC).isoformat(),
            )
        else:
            agent.provider_revision_id = latest_revision_id
            agent.sync_status = "provider_scanning"
            _set_provider_operation(
                agent,
                "publish",
                phase="provider_scanning",
                lease_expires_at=None,
                last_error=str(exc),
                last_checked_at=datetime.now(UTC).isoformat(),
            )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    expected_label = operation.get("label")
    actual_label = _revision_label(revision) or _revision_label(latest)
    if isinstance(expected_label, str) and expected_label and actual_label != expected_label:
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        agent.sync_status = "publish_unknown"
        _set_provider_operation(
            agent,
            "publish",
            phase="revision_label_mismatch",
            lease_expires_at=None,
            observed_revision_id=latest_revision_id,
            observed_revision_label=actual_label,
            last_error=(
                "The newest Smallest.ai revision is missing this operation's exact label. "
                "Confirm the provider revision before retrying."
            ),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        return agent

    revision_status, security_status = _revision_lifecycle(revision)
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    if not _publish_reconciliation_is_current(agent, operation_id):
        return agent
    agent.provider_revision_id = latest_revision_id
    if revision_status in PROVIDER_FAILED_STATES or security_status in PROVIDER_FAILED_STATES:
        agent.sync_status = "error"
        phase = "security_failed" if security_status in PROVIDER_FAILED_STATES else "publish_failed"
        last_error = (
            "Smallest.ai rejected the revision "
            f"(revision state: {revision_status}; security state: {security_status}). "
            "Review the provider revision and security result, correct the issue, then sync again."
        )
    elif revision_status == "published" and security_status == "passed":
        agent.sync_status = "synced"
        agent.last_synced_at = datetime.now(UTC)
        phase = "complete"
        last_error = None
    else:
        agent.sync_status = "provider_scanning"
        phase = "provider_scanning"
        last_error = None
    _set_provider_operation(
        agent,
        "publish",
        phase=phase,
        lease_expires_at=None,
        revision_id=latest_revision_id,
        revision_status=revision_status,
        security_status=security_status,
        last_error=last_error,
        last_checked_at=datetime.now(UTC).isoformat(),
    )
    await db.commit()
    return agent


async def _publish_smallest_agent(
    db: AsyncSession,
    *,
    agent_id: UUID,
    tenant_id: UUID,
    client,
    label: str,
    voice_model: str | None,
) -> Agent:
    """Update a draft, publish once, then reconcile the resulting revision."""
    operation_id = str(uuid4())
    operation_label = f"{label} [{operation_id}]"
    now = datetime.now(UTC)
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    agent.sync_status = "publishing"
    config = _provider_config(agent)
    config["voice_model"] = voice_model
    agent.provider_config = config
    _replace_provider_operation(
        agent,
        "publish",
        id=operation_id,
        phase="branch_lookup",
        label=operation_label,
        started_at=now.isoformat(),
        lease_expires_at=(now + PROVIDER_OPERATION_LEASE).isoformat(),
    )
    provider_agent_id = agent.provider_agent_id
    branch_id = agent.provider_branch_id
    await db.commit()

    if not provider_agent_id:
        raise HTTPException(status_code=409, detail="Provider mapping is incomplete")

    try:
        await client.set_agent_webhook_subscriptions(
            agent_id=provider_agent_id,
            webhook_id=settings.smallest_webhook_id,
        )
        if not branch_id:
            branch_id = await client.get_default_branch_id(provider_agent_id)
    except SmallestAIError as exc:
        await _mark_publish_failure(
            db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            sync_status="dirty",
            error=exc,
        )
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    operation = _publish_operation(agent)
    if operation.get("id") != operation_id or agent.sync_status != "publishing":
        raise HTTPException(status_code=409, detail="Provider operation was superseded")
    agent.provider_branch_id = branch_id
    snapshot = _publish_kwargs(agent, voice_model)
    _set_provider_operation(
        agent,
        "publish",
        phase="draft_update",
        lease_expires_at=(datetime.now(UTC) + PROVIDER_OPERATION_LEASE).isoformat(),
    )
    await db.commit()

    try:
        await client.update_agent_draft(
            agent_id=provider_agent_id,
            branch_id=branch_id,
            **snapshot,
        )
        baseline = await client.get_latest_branch_revision(
            agent_id=provider_agent_id,
            branch_id=branch_id,
        )
    except SmallestAIError as exc:
        await _mark_publish_failure(
            db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            sync_status="dirty",
            error=exc,
        )
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    baseline_revision_id = _revision_id(baseline)
    if baseline is not None and not baseline_revision_id:
        exc = SmallestAIError("Smallest.ai returned a revision without an ID.")
        await _mark_publish_failure(
            db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            sync_status="dirty",
            error=exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    operation = _publish_operation(agent)
    if operation.get("id") != operation_id or agent.sync_status != "publishing":
        raise HTTPException(status_code=409, detail="Provider operation was superseded")
    _set_provider_operation(
        agent,
        "publish",
        phase="publish_request",
        baseline_revision_id=baseline_revision_id,
        lease_expires_at=(datetime.now(UTC) + PROVIDER_OPERATION_LEASE).isoformat(),
    )
    await db.commit()

    try:
        publish = await client.publish_draft(
            agent_id=provider_agent_id,
            branch_id=branch_id,
            label=operation_label,
        )
    except SmallestAIError as exc:
        await _mark_publish_failure(
            db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            sync_status="publish_unknown" if exc.ambiguous else "error",
            error=exc,
        )
        detail = (
            "Smallest.ai publish outcome is unknown; use Check status before any retry"
            if exc.ambiguous
            else str(exc)
        )
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    # Publish responses contain only committed/scanning state. Resolve the
    # revision through the branch APIs rather than expecting an ID here.
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    operation = _publish_operation(agent)
    if operation.get("id") != operation_id:
        raise HTTPException(status_code=409, detail="Provider operation was superseded")
    if agent.sync_status in {"synced", "error"}:
        return agent
    if agent.sync_status not in {"publishing", "publish_unknown", "provider_scanning"}:
        raise HTTPException(status_code=409, detail="Provider operation was superseded")
    agent.sync_status = "provider_scanning"
    _set_provider_operation(
        agent,
        "publish",
        phase="provider_scanning",
        provider_state=publish["state"],
        lease_expires_at=None,
        last_error=None,
    )
    await db.commit()
    return await _reconcile_smallest_publish(
        db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        client=client,
    )


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Agent)
        .where(Agent.tenant_id == current_user.tenant_id)
        .order_by(Agent.created_at.desc())
    )
    return [AgentResponse.model_validate(a) for a in result.scalars().all()]


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    agent = Agent(tenant_id=current_user.tenant_id, **data.model_dump())
    db.add(agent)
    await db.flush()
    return AgentResponse.model_validate(agent)


@router.get("/provider/status")
async def get_provider_status(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return non-sensitive provider readiness for the operator console."""
    client = get_smallest_client()
    return {
        "provider": "smallest",
        "configured": client.is_configured,
        "webhook_configured": bool(
            settings.smallest_webhook_secret and settings.smallest_webhook_id
        ),
        "base_url": settings.smallest_base_url,
    }


@router.get("/provider/catalog", response_model=AgentProviderCatalog)
async def get_provider_catalog(
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return the current Waves voice catalog and local agent templates."""
    client = get_smallest_client()
    try:
        provider_voices = await client.list_voices()
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Could not load the Smallest.ai voice catalog"
        ) from exc

    # The provider account is shared across tenants. Private clones must not be
    # listed or selectable until a tenant-owned entitlement mapping exists.
    voices = normalize_voices(provider_voices, [])
    return AgentProviderCatalog(
        voices=voices,
        languages=language_catalog(voices),
        templates=AGENT_TEMPLATES,
    )


@router.post("/{agent_id}/smallest/provision", response_model=AgentResponse)
async def provision_smallest_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create the remote Atoms agent only after an operator explicitly requests it."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if _expire_stale_provisioning(agent):
        await db.commit()
        return AgentResponse.model_validate(agent)
    if agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Agent is already provisioned on Smallest.ai")
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(
            status_code=409,
            detail="Agent has an unresolved provider operation and cannot be provisioned again",
        )
    if not settings.smallest_webhook_id.strip():
        raise HTTPException(
            status_code=503,
            detail="SMALLEST_WEBHOOK_ID must be configured before provisioning agents",
        )

    client = get_smallest_client()
    voice_selection = (agent.voice_id, tuple(agent.supported_languages))
    # Never retain a row lock while waiting on the provider catalog. A second
    # provisioner can race here, so re-lock and revalidate before recording the
    # durable create lease.
    await db.commit()
    voice_model = await _require_public_voice(
        client,
        voice_selection[0],
        list(voice_selection[1]),
    )
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Agent is already provisioned on Smallest.ai")
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(
            status_code=409,
            detail="Agent has an unresolved provider operation and cannot be provisioned again",
        )
    if (agent.voice_id, tuple(agent.supported_languages)) != voice_selection:
        raise HTTPException(
            status_code=409,
            detail=(
                "Agent voice configuration changed during provider validation; retry provisioning"
            ),
        )

    # Commit a durable lease before the external create. If the process dies
    # after Smallest.ai accepts the request, retries fail closed instead of
    # creating a second paid/orphan provider agent.
    operation_id = str(uuid4())
    now = datetime.now(UTC)
    name = agent.name
    description = agent.description
    agent.sync_status = "provisioning"
    _replace_provider_operation(
        agent,
        "provision",
        id=operation_id,
        phase="create_request",
        started_at=now.isoformat(),
        lease_expires_at=(now + PROVIDER_OPERATION_LEASE).isoformat(),
    )
    await db.commit()

    try:
        provider_agent_id = await client.create_agent(
            name=name,
            description=description,
        )
    except SmallestAIError as exc:
        agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
        agent.sync_status = "provision_unknown" if exc.ambiguous else "error"
        _set_provider_operation(
            agent,
            "provision",
            phase=agent.sync_status,
            lease_expires_at=None,
            last_error=str(exc),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
        detail = (
            "Smallest.ai create outcome is unknown; do not retry until the "
            "provider mapping is reconciled"
            if exc.ambiguous
            else str(exc)
        )
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    # Persist the remote mapping before later provider calls so a partial failure
    # can be resumed with Sync instead of creating an orphaned duplicate agent.
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    agent.voice_provider = "smallest"
    agent.provider_agent_id = provider_agent_id
    agent.sync_status = "dirty"
    _set_provider_operation(
        agent,
        "provision",
        phase="mapped",
        lease_expires_at=None,
        provider_agent_id=provider_agent_id,
        last_error=None,
        completed_at=datetime.now(UTC).isoformat(),
    )
    await db.commit()
    agent = await _publish_smallest_agent(
        db,
        agent_id=agent_id,
        tenant_id=current_user.tenant_id,
        client=client,
        label=f"VAV Voice AI initial release {datetime.now(UTC).date().isoformat()}",
        voice_model=voice_model,
    )
    return AgentResponse.model_validate(agent)


@router.post("/{agent_id}/smallest/sync", response_model=AgentResponse)
async def sync_smallest_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Publish a dirty draft, or reconcile an unresolved provider revision."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if _expire_stale_provisioning(agent):
        await db.commit()
        return AgentResponse.model_validate(agent)
    if not agent.provider_agent_id:
        if agent.sync_status == "provision_unknown":
            raise HTTPException(
                status_code=409,
                detail="Provider create outcome is unknown and requires operator reconciliation",
            )
        raise HTTPException(status_code=409, detail="Provision this agent on Smallest.ai first")
    if agent.sync_status == "synced":
        raise HTTPException(status_code=409, detail="Agent is already in sync")

    client = get_smallest_client()
    if agent.sync_status in RECONCILABLE_PUBLISH_STATES:
        agent = await _reconcile_smallest_publish(
            db,
            agent_id=agent_id,
            tenant_id=current_user.tenant_id,
            client=client,
        )
        return AgentResponse.model_validate(agent)
    if agent.sync_status in {"provisioning", "provision_unknown"}:
        raise HTTPException(status_code=409, detail="Agent provisioning is unresolved")
    if not settings.smallest_webhook_id.strip():
        raise HTTPException(
            status_code=503,
            detail="SMALLEST_WEBHOOK_ID must be configured before publishing agents",
        )
    voice_selection = (agent.voice_id, tuple(agent.supported_languages))
    # Release the row lock around the provider catalog read, then re-lock and
    # prove no competing provider operation or voice edit superseded this sync.
    await db.commit()
    voice_model = await _require_public_voice(
        client,
        voice_selection[0],
        list(voice_selection[1]),
    )
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(status_code=409, detail="Agent provider operation is unresolved")
    if agent.sync_status == "synced":
        raise HTTPException(status_code=409, detail="Agent is already in sync")
    if not agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Provider mapping is incomplete")
    if (agent.voice_id, tuple(agent.supported_languages)) != voice_selection:
        raise HTTPException(
            status_code=409,
            detail="Agent voice configuration changed during provider validation; retry sync",
        )
    agent = await _publish_smallest_agent(
        db,
        agent_id=agent_id,
        tenant_id=current_user.tenant_id,
        client=client,
        label=f"VAV Voice AI sync {datetime.now(UTC).date().isoformat()}",
        voice_model=voice_model,
    )
    return AgentResponse.model_validate(agent)


@router.post("/{agent_id}/smallest/resolve", response_model=AgentResponse)
async def resolve_smallest_provider_operation(
    agent_id: UUID,
    data: SmallestProviderResolution,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a provider-verified ambiguous create or publish outcome."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    client = get_smallest_client()

    if data.action == "confirm_create_absent":
        if agent.sync_status != "provision_unknown":
            raise HTTPException(status_code=409, detail="Agent has no ambiguous create to reset")
        agent.sync_status = "local_only"
        _set_provider_operation(
            agent,
            "provision",
            phase="manually_confirmed_absent",
            lease_expires_at=None,
            resolved_by=str(current_user.id),
            resolved_at=datetime.now(UTC).isoformat(),
            last_error=None,
        )
        audit_action = "agent.provider_create_confirmed_absent"
        audit_details = {}
    else:
        if agent.sync_status != "publish_unknown":
            raise HTTPException(status_code=409, detail="Agent has no ambiguous publish to reset")
        operation = _publish_operation(agent)
        if "baseline_revision_id" not in operation:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Publish baseline is unavailable; platform support must reconcile "
                    "this operation"
                ),
            )
        if not agent.provider_agent_id or not agent.provider_branch_id:
            raise HTTPException(status_code=409, detail="Provider mapping is incomplete")
        operation_id = operation.get("id")
        baseline_revision_id = operation.get("baseline_revision_id")
        provider_agent_id = agent.provider_agent_id
        branch_id = agent.provider_branch_id
        await db.commit()
        try:
            latest = await client.get_latest_branch_revision(
                agent_id=provider_agent_id,
                branch_id=branch_id,
            )
        except SmallestAIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if _revision_id(latest) != baseline_revision_id:
            raise HTTPException(
                status_code=409,
                detail="A new provider revision exists; use Check status instead of abandoning it",
            )
        try:
            draft = await client.get_open_branch_draft(
                agent_id=provider_agent_id,
                branch_id=branch_id,
            )
            latest_after_draft = await client.get_latest_branch_revision(
                agent_id=provider_agent_id,
                branch_id=branch_id,
            )
        except SmallestAIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if _revision_id(latest_after_draft) != baseline_revision_id:
            raise HTTPException(
                status_code=409,
                detail="A new provider revision exists; use Check status instead of abandoning it",
            )
        if not _pending_publish_is_authoritatively_absent(draft):
            pending_state = _pending_publish_state(draft) or "unknown"
            raise HTTPException(
                status_code=409,
                detail=(
                    "Smallest.ai still reports a pending publish "
                    f"(state: {pending_state}); use Check status instead of abandoning it"
                ),
            )
        agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
        if (
            agent.sync_status != "publish_unknown"
            or _publish_operation(agent).get("id") != operation_id
        ):
            raise HTTPException(status_code=409, detail="Provider operation was already resolved")
        agent.sync_status = "dirty"
        _set_provider_operation(
            agent,
            "publish",
            phase="manually_confirmed_absent",
            lease_expires_at=None,
            resolved_by=str(current_user.id),
            resolved_at=datetime.now(UTC).isoformat(),
            last_error=None,
        )
        audit_action = "agent.provider_publish_confirmed_absent"
        audit_details = {"baseline_revision_id": baseline_revision_id}

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action=audit_action,
        resource_type="agent",
        resource_id=str(agent.id),
        details=audit_details,
    )
    await db.commit()
    return AgentResponse.model_validate(agent)


@router.post(
    "/{agent_id}/smallest/session",
    response_model=SmallestSessionResponse,
)
async def create_smallest_browser_session(
    agent_id: UUID,
    data: SmallestSessionRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Mint a short-lived, single-use web-call token without exposing the API key."""
    # Treat deactivation as a safety boundary. Keep the authoritative Agent row
    # locked through the external token mint so a committed deactivation wins
    # before any usable browser credential can be returned. An edit that waits
    # behind this lock linearizes after the already-authorized session instead.
    agent = await _tenant_agent(
        db,
        agent_id,
        current_user.tenant_id,
        for_update=True,
    )
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="Agent is inactive")
    if not agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Provision this agent on Smallest.ai first")
    if not agent.provider_revision_id or not agent.last_synced_at:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot take calls until its initial provider revision is published",
        )
    try:
        session = await get_smallest_client().create_browser_session(
            agent_id=agent.provider_agent_id,
            variables=data.variables,
        )
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    # Deliberately release the readiness lock before serializing the bearer
    # credential into the response.
    await db.commit()
    return SmallestSessionResponse(
        access_token=session.access_token,
        expires_in=session.expires_in,
        sample_rate=session.sample_rate,
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    data: AgentUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot be edited while its provider operation is unresolved",
        )

    changes = data.model_dump(exclude_unset=True)
    language = changes.get("language", agent.language)
    supported_languages = changes.get("supported_languages", agent.supported_languages)
    if language not in supported_languages:
        raise HTTPException(
            status_code=422,
            detail="Primary language must be included in supported languages",
        )
    if "ta" in supported_languages and len(supported_languages) > 1:
        raise HTTPException(
            status_code=422,
            detail="Tamil cannot be combined with other supported languages",
        )

    for key, value in changes.items():
        setattr(agent, key, value)

    if agent.provider_agent_id and SMALLEST_SYNC_FIELDS.intersection(changes):
        agent.sync_status = "dirty"

    await db.flush()
    return AgentResponse.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot be deleted while its provider operation is unresolved",
        )
    if agent.provider_agent_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "Provisioned agents cannot be deleted until provider deprovisioning "
                "and archival are supported"
            ),
        )
    nonterminal_call_id = await db.scalar(
        select(Call.id)
        .where(
            Call.tenant_id == current_user.tenant_id,
            Call.agent_id == agent.id,
            Call.status.notin_(TERMINAL_CALL_STATUSES),
        )
        .limit(1)
    )
    if nonterminal_call_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot be deleted while a call is still nonterminal",
        )
    nonterminal_attempt_id = await db.scalar(
        select(CampaignContactAttempt.id)
        .join(Campaign, Campaign.id == CampaignContactAttempt.campaign_id)
        .outerjoin(Call, Call.id == CampaignContactAttempt.call_id)
        .where(
            Campaign.tenant_id == current_user.tenant_id,
            CampaignContactAttempt.tenant_id == current_user.tenant_id,
            CampaignContactAttempt.state.notin_(TERMINAL_CAMPAIGN_ATTEMPT_STATES),
            or_(Campaign.agent_id == agent.id, Call.agent_id == agent.id),
        )
        .limit(1)
    )
    if nonterminal_attempt_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot be deleted while a campaign attempt is unresolved",
        )
    await db.delete(agent)


# Knowledge Base endpoints
@router.get("/{agent_id}/knowledge", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    agent_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.agent_id == agent_id,
            KnowledgeBase.tenant_id == current_user.tenant_id,
        )
    )
    return [KnowledgeBaseResponse.model_validate(kb) for kb in result.scalars().all()]


@router.post(
    "/{agent_id}/knowledge",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    agent_id: UUID,
    data: KnowledgeBaseCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    # Verify agent belongs to tenant
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Agent not found")

    kb = KnowledgeBase(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        **data.model_dump(),
    )
    db.add(kb)
    await db.flush()
    return KnowledgeBaseResponse.model_validate(kb)


@router.delete("/{agent_id}/knowledge/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    agent_id: UUID,
    kb_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.agent_id == agent_id,
            KnowledgeBase.tenant_id == current_user.tenant_id,
        )
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base entry not found")
    await db.delete(kb)
