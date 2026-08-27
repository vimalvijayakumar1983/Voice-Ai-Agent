"""Agent builder endpoints - CRUD for AI voice agents."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent, KnowledgeBase
from app.providers.smallest import SmallestAIError, get_smallest_client
from app.schemas.agent import (
    AgentCreate,
    AgentResponse,
    AgentUpdate,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    SmallestSessionRequest,
    SmallestSessionResponse,
)

router = APIRouter(prefix="/agents", tags=["Agents"])

SMALLEST_SYNC_FIELDS = {
    "name",
    "system_prompt",
    "greeting_message",
    "model_name",
    "voice_id",
    "language",
    "speech_rate",
    "timezone",
}


async def _tenant_agent(db: AsyncSession, agent_id: UUID, tenant_id: UUID) -> Agent:
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


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
    current_user: CurrentUser = Depends(get_current_user),
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
        "webhook_configured": bool(settings.smallest_webhook_secret),
        "base_url": settings.smallest_base_url,
    }


@router.post("/{agent_id}/smallest/provision", response_model=AgentResponse)
async def provision_smallest_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create the remote Atoms agent only after an operator explicitly requests it."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id)
    if agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Agent is already provisioned on Smallest.ai")

    client = get_smallest_client()
    try:
        provider_agent_id = await client.create_agent(
            name=agent.name,
            description=agent.description,
        )
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # Persist the remote mapping before later provider calls so a partial failure
    # can be resumed with Sync instead of creating an orphaned duplicate agent.
    agent.voice_provider = "smallest"
    agent.provider_agent_id = provider_agent_id
    agent.sync_status = "dirty"
    await db.commit()

    try:
        provider_branch_id = await client.get_default_branch_id(provider_agent_id)
        await client.update_agent_draft(
            agent_id=provider_agent_id,
            branch_id=provider_branch_id,
            global_prompt=agent.system_prompt,
            first_message=agent.greeting_message,
            slm_model=agent.model_name,
            language=agent.language,
            timezone=agent.timezone,
            voice_id=agent.voice_id,
            speech_rate=agent.speech_rate,
        )
        publish = await client.publish_draft(
            agent_id=provider_agent_id,
            branch_id=provider_branch_id,
            label=f"VAV Voice AI initial release {datetime.now(UTC).date().isoformat()}",
        )
    except SmallestAIError as exc:
        agent.sync_status = "error"
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    agent.provider_branch_id = provider_branch_id
    revision = publish.get("revision")
    if isinstance(revision, dict):
        agent.provider_revision_id = revision.get("_id") or revision.get("id")
    agent.sync_status = "publishing" if publish.get("state") == "scanning" else "synced"
    agent.last_synced_at = datetime.now(UTC)
    await db.flush()
    return AgentResponse.model_validate(agent)


@router.post("/{agent_id}/smallest/sync", response_model=AgentResponse)
async def sync_smallest_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Publish the local prompt and greeting through Atoms branch versioning."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id)
    if not agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Provision this agent on Smallest.ai first")

    client = get_smallest_client()
    try:
        branch_id = agent.provider_branch_id or await client.get_default_branch_id(
            agent.provider_agent_id
        )
        await client.update_agent_draft(
            agent_id=agent.provider_agent_id,
            branch_id=branch_id,
            global_prompt=agent.system_prompt,
            first_message=agent.greeting_message,
            slm_model=agent.model_name,
            language=agent.language,
            timezone=agent.timezone,
            voice_id=agent.voice_id,
            speech_rate=agent.speech_rate,
        )
        publish = await client.publish_draft(
            agent_id=agent.provider_agent_id,
            branch_id=branch_id,
            label=f"VAV Voice AI sync {datetime.now(UTC).date().isoformat()}",
        )
    except SmallestAIError as exc:
        agent.sync_status = "error"
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    agent.provider_branch_id = branch_id
    revision = publish.get("revision")
    if isinstance(revision, dict):
        agent.provider_revision_id = revision.get("_id") or revision.get("id")
    agent.sync_status = "publishing" if publish.get("state") == "scanning" else "synced"
    agent.last_synced_at = datetime.now(UTC)
    await db.flush()
    return AgentResponse.model_validate(agent)


@router.post(
    "/{agent_id}/smallest/session",
    response_model=SmallestSessionResponse,
)
async def create_smallest_browser_session(
    agent_id: UUID,
    data: SmallestSessionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a short-lived, single-use web-call token without exposing the API key."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id)
    if not agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Provision this agent on Smallest.ai first")
    try:
        session = await get_smallest_client().create_browser_session(
            agent_id=agent.provider_agent_id,
            variables=data.variables,
        )
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == current_user.tenant_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(agent, key, value)

    if agent.provider_agent_id and SMALLEST_SYNC_FIELDS.intersection(changes):
        agent.sync_status = "dirty"

    await db.flush()
    return AgentResponse.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
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
    current_user: CurrentUser = Depends(get_current_user),
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
    current_user: CurrentUser = Depends(get_current_user),
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
