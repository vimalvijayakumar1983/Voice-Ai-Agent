"""Operator controls for VAV-hosted realtime agents and SIP connectivity."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent, AgentRuntimeProfile
from app.schemas.runtime import (
    ApiKeyCredentialRequest,
    RuntimeProfileResponse,
    RuntimeProfileUpdate,
    RuntimeReadinessResponse,
    SipCredentialRequest,
    SipCredentialStatus,
    TwilioCredentialRequest,
    WorkspaceCredentialStatus,
    WorkspaceCredentialStatuses,
)
from app.services.audit import record_audit_event
from app.services.provider_credentials import (
    ProviderCredentialError,
    get_provider_credential,
    load_provider_config,
    store_provider_config,
)

router = APIRouter(prefix="/runtime", tags=["Realtime Runtime"])


async def _agent(db: AsyncSession, tenant_id: UUID, agent_id: UUID) -> Agent:
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id))
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _profile(
    db: AsyncSession,
    tenant_id: UUID,
    agent_id: UUID,
    *,
    create: bool = False,
) -> AgentRuntimeProfile | None:
    profile = await db.scalar(
        select(AgentRuntimeProfile).where(
            AgentRuntimeProfile.agent_id == agent_id,
            AgentRuntimeProfile.tenant_id == tenant_id,
        )
    )
    if profile is None and create:
        profile = AgentRuntimeProfile(tenant_id=tenant_id, agent_id=agent_id)
        db.add(profile)
        await db.flush()
    return profile


async def runtime_readiness(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile | None,
) -> tuple[list[str], dict[str, bool]]:
    try:
        sarvam_config = await load_provider_config(db, agent.tenant_id, "sarvam")
    except ProviderCredentialError:
        sarvam_config = None
    sarvam_ready = bool(
        (sarvam_config and sarvam_config.get("api_key")) or settings.sarvam_api_key.strip()
    )
    try:
        openai_config = await load_provider_config(db, agent.tenant_id, "openai")
    except ProviderCredentialError:
        openai_config = None
    try:
        twilio_config = await load_provider_config(db, agent.tenant_id, "twilio")
    except ProviderCredentialError:
        twilio_config = None
    checks = {
        "agent_active": bool(agent.is_active),
        "sarvam_agent": agent.voice_provider == "sarvam",
        "sarvam_credential": sarvam_ready,
        "sarvam_voice": agent.voice_id.startswith("sarvam:") if agent.voice_id else False,
        "openai_credential": bool(
            (openai_config and openai_config.get("api_key")) or settings.openai_api_key.strip()
        ),
        "public_runtime_url": settings.base_url.startswith("https://"),
        "telephony_credential": False,
        "number_assigned": bool(profile and profile.assigned_numbers),
    }
    if profile and profile.telephony_provider == "twilio":
        checks["telephony_credential"] = bool(
            (twilio_config and twilio_config.get("account_sid") and twilio_config.get("auth_token"))
            or (settings.twilio_account_sid and settings.twilio_auth_token)
        )
    elif profile and profile.telephony_provider == "livekit_sip":
        try:
            sip = await load_provider_config(db, agent.tenant_id, "livekit_sip")
        except ProviderCredentialError:
            sip = None
        checks["telephony_credential"] = bool(
            sip
            and sip.get("sip_uri")
            and sip.get("livekit_url")
            and sip.get("livekit_api_key")
            and sip.get("livekit_api_secret")
        )
        # Credentials alone don't create a public SIP/RTP edge. This flag is
        # set only by the infrastructure provisioner after trunk and dispatch
        # rules are verified outside the Railway web service.
        checks["sip_gateway_provisioned"] = bool(sip and sip.get("gateway_provisioned"))
    labels = {
        "agent_active": "Activate the agent configuration.",
        "sarvam_agent": "Select Sarvam as this agent's voice provider.",
        "sarvam_credential": "Add a valid Sarvam API key in Settings.",
        "sarvam_voice": "Select a Sarvam Bulbul v3 voice.",
        "openai_credential": "Add an OpenAI API key in Settings.",
        "public_runtime_url": "Configure BASE_URL as the public HTTPS API origin.",
        "telephony_credential": "Add credentials for the selected telephony provider in Settings.",
        "number_assigned": "Assign at least one E.164 phone number to the runtime.",
        "sip_gateway_provisioned": (
            "Provision and verify the external LiveKit SIP/RTP gateway for the Etisalat trunk."
        ),
    }
    return [labels[name] for name, passed in checks.items() if not passed], checks


def _response(
    agent_id: UUID,
    profile: AgentRuntimeProfile | None,
    blockers: list[str],
) -> RuntimeProfileResponse:
    values = {
        "telephony_provider": "twilio",
        "primary_speech_provider": "sarvam",
        "fallback_speech_provider": None,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "stt_language": "auto",
        "max_concurrent_calls": 1,
        "daily_call_limit": 100,
        "monthly_budget_cents": 5000,
        "assigned_numbers": [],
    }
    if profile is not None:
        values.update({key: getattr(profile, key) for key in values})
    return RuntimeProfileResponse(
        id=profile.id if profile else None,
        agent_id=agent_id,
        enabled=bool(profile and profile.enabled),
        status=profile.status if profile else "draft",
        ready=not blockers,
        blockers=blockers,
        last_tested_at=profile.last_tested_at if profile else None,
        created_at=profile.created_at if profile else None,
        updated_at=profile.updated_at if profile else None,
        **values,
    )


@router.get("/agents", response_model=list[RuntimeProfileResponse])
async def list_runtime_profiles(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agents = (
        await db.scalars(select(Agent).where(Agent.tenant_id == current_user.tenant_id))
    ).all()
    result = []
    for agent in agents:
        profile = await _profile(db, current_user.tenant_id, agent.id)
        blockers, _checks = await runtime_readiness(db, agent, profile)
        result.append(_response(agent.id, profile, blockers))
    return result


@router.get("/agents/{agent_id}", response_model=RuntimeProfileResponse)
async def get_runtime_profile(
    agent_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id)
    blockers, _checks = await runtime_readiness(db, agent, profile)
    return _response(agent.id, profile, blockers)


@router.put("/agents/{agent_id}", response_model=RuntimeProfileResponse)
async def update_runtime_profile(
    agent_id: UUID,
    data: RuntimeProfileUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id, create=True)
    assert profile is not None
    for key, value in data.model_dump().items():
        setattr(profile, key, value)
    profile.enabled = False
    profile.status = "draft"
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.runtime_configured",
        resource_type="agent",
        resource_id=str(agent.id),
        details={
            "telephony_provider": profile.telephony_provider,
            "primary_speech_provider": profile.primary_speech_provider,
            "assigned_number_count": len(profile.assigned_numbers),
        },
    )
    blockers, _checks = await runtime_readiness(db, agent, profile)
    return _response(agent.id, profile, blockers)


@router.post("/agents/{agent_id}/test", response_model=RuntimeReadinessResponse)
async def test_runtime_profile(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id, create=True)
    assert profile is not None
    blockers, checks = await runtime_readiness(db, agent, profile)
    tested_at = datetime.now(UTC)
    profile.last_tested_at = tested_at
    if blockers:
        # A failed production readiness check must fail closed. Keeping enabled
        # while changing only the status leaves the control plane inconsistent.
        profile.enabled = False
        profile.status = "blocked"
    else:
        # Testing an already-active runtime is observational; it must not demote
        # the profile to "ready" and silently remove it from inbound routing.
        profile.status = "active" if profile.enabled else "ready"
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.runtime_tested",
        resource_type="agent",
        resource_id=str(agent.id),
        details={"ready": not blockers, "checks": checks},
    )
    return RuntimeReadinessResponse(
        agent_id=agent.id,
        ready=not blockers,
        status="ready" if not blockers else "blocked",
        blockers=blockers,
        checks=checks,
        tested_at=tested_at,
    )


@router.post("/agents/{agent_id}/activate", response_model=RuntimeProfileResponse)
async def activate_runtime_profile(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id, create=True)
    assert profile is not None
    blockers, _checks = await runtime_readiness(db, agent, profile)
    if blockers:
        profile.enabled = False
        profile.status = "blocked"
        raise HTTPException(
            status_code=409,
            detail={"message": "Runtime is not ready", "blockers": blockers},
        )
    profile.enabled = True
    profile.status = "active"
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.runtime_activated",
        resource_type="agent",
        resource_id=str(agent.id),
        details={"telephony_provider": profile.telephony_provider},
    )
    return _response(agent.id, profile, blockers)


@router.post("/agents/{agent_id}/deactivate", response_model=RuntimeProfileResponse)
async def deactivate_runtime_profile(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id, create=True)
    assert profile is not None
    profile.enabled = False
    profile.status = "inactive"
    blockers, _checks = await runtime_readiness(db, agent, profile)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.runtime_deactivated",
        resource_type="agent",
        resource_id=str(agent.id),
    )
    return _response(agent.id, profile, blockers)


def _platform_credential_config(provider: str) -> dict[str, str]:
    if provider == "smallest":
        return {"api_key": settings.smallest_api_key}
    if provider == "sarvam":
        return {"api_key": settings.sarvam_api_key}
    if provider == "openai":
        return {"api_key": settings.openai_api_key}
    if provider == "twilio":
        return {
            "account_sid": settings.twilio_account_sid,
            "auth_token": settings.twilio_auth_token,
            "default_from_number": settings.twilio_default_from_number,
        }
    return {}


def _credential_configured(provider: str, config: dict | None) -> bool:
    if provider == "twilio":
        return bool(config and config.get("account_sid") and config.get("auth_token"))
    return bool(config and config.get("api_key"))


async def _workspace_credential_status(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
) -> WorkspaceCredentialStatus:
    credential = await get_provider_credential(db, tenant_id, provider)
    workspace_config: dict | None = None
    if credential and credential.is_active:
        try:
            workspace_config = await load_provider_config(db, tenant_id, provider)
        except ProviderCredentialError:
            workspace_config = None
    platform_config = _platform_credential_config(provider)
    if _credential_configured(provider, workspace_config):
        config = workspace_config or {}
        source = "workspace"
        updated_at = credential.updated_at if credential else None
    elif _credential_configured(provider, platform_config):
        config = platform_config
        source = "platform"
        updated_at = None
    else:
        config = {}
        source = "none"
        updated_at = None
    account_sid = str(config.get("account_sid") or "")
    return WorkspaceCredentialStatus(
        provider=provider,
        configured=source != "none",
        source=source,
        updated_at=updated_at,
        account_sid_hint=(f"AC••••{account_sid[-4:]}" if account_sid else None),
        default_from_number=str(config.get("default_from_number") or "") or None,
    )


@router.get("/credentials", response_model=WorkspaceCredentialStatuses)
async def list_workspace_credentials(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    providers = {}
    for provider in ("smallest", "sarvam", "openai", "twilio"):
        providers[provider] = await _workspace_credential_status(
            db, current_user.tenant_id, provider
        )
    return WorkspaceCredentialStatuses(providers=providers)


@router.put("/credentials/{provider}", response_model=WorkspaceCredentialStatus)
async def save_api_key_credential(
    provider: str,
    data: ApiKeyCredentialRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    if provider not in {"smallest", "sarvam", "openai"}:
        raise HTTPException(status_code=404, detail="Unsupported API-key provider")
    existing = await get_provider_credential(db, current_user.tenant_id, provider)
    try:
        credential = await store_provider_config(
            db, current_user.tenant_id, provider, data.model_dump()
        )
    except ProviderCredentialError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action=("provider_credential.rotated" if existing else "provider_credential.created"),
        resource_type="provider_credential",
        resource_id=str(credential.id),
        details={"provider": provider},
    )
    return await _workspace_credential_status(db, current_user.tenant_id, provider)


@router.put("/credentials/twilio/account", response_model=WorkspaceCredentialStatus)
async def save_twilio_credential(
    data: TwilioCredentialRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    existing = await get_provider_credential(db, current_user.tenant_id, "twilio")
    try:
        credential = await store_provider_config(
            db,
            current_user.tenant_id,
            "twilio",
            data.model_dump(),
        )
    except ProviderCredentialError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action=("provider_credential.rotated" if existing else "provider_credential.created"),
        resource_type="provider_credential",
        resource_id=str(credential.id),
        details={"provider": "twilio"},
    )
    return await _workspace_credential_status(db, current_user.tenant_id, "twilio")


@router.delete("/credentials/{provider}", response_model=WorkspaceCredentialStatus)
async def delete_workspace_credential(
    provider: str,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    if provider not in {"smallest", "sarvam", "openai", "twilio"}:
        raise HTTPException(status_code=404, detail="Unsupported credential provider")
    credential = await get_provider_credential(
        db, current_user.tenant_id, provider, for_update=True
    )
    if credential:
        credential_id = str(credential.id)
        await db.delete(credential)
        await db.flush()
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="provider_credential.deleted",
            resource_type="provider_credential",
            resource_id=credential_id,
            details={"provider": provider},
        )
    return await _workspace_credential_status(db, current_user.tenant_id, provider)


@router.get("/sip/credential", response_model=SipCredentialStatus)
async def get_sip_credential_status(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    credential = await get_provider_credential(db, current_user.tenant_id, "livekit_sip")
    return SipCredentialStatus(
        configured=bool(credential and credential.is_active),
        updated_at=credential.updated_at if credential else None,
    )


@router.put("/sip/credential", response_model=SipCredentialStatus)
async def save_sip_credential(
    data: SipCredentialRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        credential = await store_provider_config(
            db, current_user.tenant_id, "livekit_sip", data.model_dump()
        )
    except ProviderCredentialError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="provider_credential.rotated",
        resource_type="provider_credential",
        resource_id=str(credential.id),
        details={"provider": "livekit_sip"},
    )
    return SipCredentialStatus(configured=True, updated_at=credential.updated_at)


@router.delete("/sip/credential", response_model=SipCredentialStatus)
async def delete_sip_credential(
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    credential = await get_provider_credential(
        db, current_user.tenant_id, "livekit_sip", for_update=True
    )
    if credential:
        credential_id = str(credential.id)
        await db.delete(credential)
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="provider_credential.deleted",
            resource_type="provider_credential",
            resource_id=credential_id,
            details={"provider": "livekit_sip"},
        )
    return SipCredentialStatus(configured=False)

