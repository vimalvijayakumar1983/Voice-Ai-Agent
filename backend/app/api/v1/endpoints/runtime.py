"""Operator controls for VAV-hosted realtime agents and SIP connectivity."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    AgentRuntimeProfile,
    KnowledgeBase,
    KnowledgeSource,
)
from app.models.provider_credential import ProviderCredential
from app.providers.elevenlabs import ElevenLabsClient, ElevenLabsError
from app.providers.inworld import INWORLD_TTS_MODEL, InworldClient, InworldError
from app.providers.openai import OpenAIProviderClient, OpenAIProviderError
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
from app.services.integration_security import (
    IntegrationConfigUnavailableError,
    decrypt_integration_config,
)
from app.services.provider_credentials import (
    ProviderCredentialError,
    get_provider_credential,
    load_provider_config,
    store_provider_config,
)
from app.services.rate_limit import enforce_rate_limit
from app.telephony.livekit_provider import LiveKitSIPError, LiveKitSIPProvider

router = APIRouter(prefix="/runtime", tags=["Realtime Runtime"])


def _speech_provider_name(provider: str) -> str:
    return {
        "elevenlabs": "ElevenLabs",
        "inworld": "Inworld",
        "sarvam": "Sarvam AI",
    }.get(provider, provider.title())


def _api_key_configured(config: dict | None, platform_key: str) -> bool:
    """Use the platform fallback only when no tenant credential exists."""
    if config is not None:
        return bool(str(config.get("api_key") or "").strip())
    return bool(str(platform_key or "").strip())


def _runtime_provider_blocker(
    agent: Agent,
    profile: AgentRuntimeProfile | None,
) -> str | None:
    """Reject route combinations the deployed runtime does not implement."""
    if profile is None:
        return "Save a supported runtime provider route before testing readiness."
    if agent.voice_provider == "inworld":
        runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
        voice_runtime = str(runtime_config.get("voice_runtime") or "pipeline")
        if (
            profile.telephony_provider != "livekit_sip"
            or profile.primary_speech_provider != "inworld"
            or profile.llm_provider not in {"inworld", "openai"}
        ):
            return (
                "Inworld agents require LiveKit SIP telephony, Inworld speech, "
                "and either the OpenAI LLM or Inworld Router."
            )
        if voice_runtime == "inworld_realtime" and profile.llm_provider != "inworld":
            return (
                "Native Inworld Realtime requires the Inworld LLM route so speech, "
                "reasoning, turn-taking, and voice remain in one provider session."
            )
        return None
    if agent.voice_provider in {"sarvam", "elevenlabs"}:
        if (
            profile.telephony_provider != "twilio"
            or profile.primary_speech_provider != agent.voice_provider
            or profile.llm_provider != "openai"
        ):
            return (
                f"{_speech_provider_name(agent.voice_provider)} agents require Twilio telephony, "
                f"{_speech_provider_name(agent.voice_provider)} speech, and the OpenAI LLM."
            )
        return None
    return "Select Inworld, Sarvam, or ElevenLabs before configuring a VAV realtime runtime."


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


async def _number_route_conflicts(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile | None,
) -> list[Agent]:
    """Return active agents that already own one of this profile's phone routes."""
    if profile is None or not profile.assigned_numbers:
        return []

    rows = (
        await db.execute(
            select(AgentRuntimeProfile, Agent)
            .join(
                Agent,
                (Agent.id == AgentRuntimeProfile.agent_id)
                & (Agent.tenant_id == AgentRuntimeProfile.tenant_id),
            )
            .where(
                AgentRuntimeProfile.tenant_id == agent.tenant_id,
                AgentRuntimeProfile.agent_id != agent.id,
                AgentRuntimeProfile.enabled.is_(True),
                AgentRuntimeProfile.status == "active",
                AgentRuntimeProfile.telephony_provider == profile.telephony_provider,
                Agent.is_active.is_(True),
            )
        )
    ).all()
    assigned = set(profile.assigned_numbers)
    return [
        candidate_agent
        for candidate_profile, candidate_agent in rows
        if assigned.intersection(candidate_profile.assigned_numbers or [])
    ]


async def _lock_number_routes(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile,
) -> None:
    """Serialize activation for the same tenant/provider/number on PostgreSQL."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for number in sorted(set(profile.assigned_numbers or [])):
        route_key = f"{agent.tenant_id}:{profile.telephony_provider}:{number}"
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:route_key, 0))"),
            {"route_key": route_key},
        )


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
        elevenlabs_config = await load_provider_config(db, agent.tenant_id, "elevenlabs")
    except ProviderCredentialError:
        elevenlabs_config = None
    elevenlabs_ready = bool(
        (elevenlabs_config and elevenlabs_config.get("api_key"))
        or settings.elevenlabs_api_key.strip()
    )
    try:
        inworld_config = await load_provider_config(db, agent.tenant_id, "inworld")
    except ProviderCredentialError:
        inworld_config = None
        inworld_config_unreadable = True
    else:
        inworld_config_unreadable = False
    inworld_ready = not inworld_config_unreadable and _api_key_configured(
        inworld_config, settings.inworld_api_key
    )
    try:
        openai_config = await load_provider_config(db, agent.tenant_id, "openai")
    except ProviderCredentialError:
        openai_config = None
        openai_config_unreadable = True
    else:
        openai_config_unreadable = False
    try:
        twilio_config = await load_provider_config(db, agent.tenant_id, "twilio")
    except ProviderCredentialError:
        twilio_config = None
    knowledge_binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.agent_id == agent.id,
            AgentKnowledgeBinding.tenant_id == agent.tenant_id,
        )
    )
    # The production Inworld lane is intended for governed customer-facing
    # agents, so it fails closed unless approved searchable knowledge is bound.
    knowledge_ready = not (profile and profile.primary_speech_provider == "inworld")
    if knowledge_binding is not None:
        bound_knowledge = await db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_binding.knowledge_base_id,
                KnowledgeBase.tenant_id == agent.tenant_id,
                KnowledgeBase.is_active.is_(True),
                KnowledgeBase.approval_status == "approved",
            )
        )
        if bound_knowledge is None:
            knowledge_ready = False
        else:
            source_states = (
                await db.execute(
                    select(KnowledgeSource.status, KnowledgeSource.content).where(
                        KnowledgeSource.knowledge_base_id == bound_knowledge.id,
                        KnowledgeSource.tenant_id == agent.tenant_id,
                    )
                )
            ).all()
            knowledge_ready = bool(source_states) and all(
                status in {"processing", "indexed", "local_only"}
                and bool(str(content or "").strip())
                for status, content in source_states
            )

    number_route_conflicts = await _number_route_conflicts(db, agent, profile)

    vav_speech_agent = agent.voice_provider in {"sarvam", "elevenlabs", "inworld"}
    tts_ready = {
        "sarvam": sarvam_ready,
        "elevenlabs": elevenlabs_ready,
        "inworld": inworld_ready,
    }.get(agent.voice_provider, False)
    stt_ready = (
        inworld_ready if profile and profile.primary_speech_provider == "inworld" else sarvam_ready
    )
    llm_ready = (
        inworld_ready
        if profile and profile.llm_provider == "inworld"
        else not openai_config_unreadable
        and _api_key_configured(openai_config, settings.openai_api_key)
    )
    voice_ready = bool(
        agent.voice_id
        and vav_speech_agent
        and agent.voice_id.startswith(f"{agent.voice_provider}:")
    )
    provider_compatibility_blocker = _runtime_provider_blocker(agent, profile)
    checks = {
        "agent_active": bool(agent.is_active),
        "vav_speech_agent": vav_speech_agent,
        "provider_compatibility": provider_compatibility_blocker is None,
        "stt_credential": stt_ready,
        "tts_credential": tts_ready,
        "voice_selection": voice_ready,
        "speech_provider_match": bool(
            profile and vav_speech_agent and profile.primary_speech_provider == agent.voice_provider
        ),
        "llm_credential": llm_ready,
        "public_runtime_url": settings.base_url.startswith("https://"),
        "telephony_credential": False,
        "number_assigned": bool(profile and profile.assigned_numbers),
        "number_route_unique": not number_route_conflicts,
        "knowledge_retrieval": knowledge_ready,
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
            and settings.livekit_url
            and settings.livekit_api_key
            and settings.livekit_api_secret
        )
        # Credentials alone don't create a public SIP/RTP edge. This flag is
        # set only by the infrastructure provisioner after trunk and dispatch
        # rules are verified outside the Railway web service.
        checks["sip_gateway_provisioned"] = bool(
            sip and sip.get("inbound_trunk_id") and sip.get("dispatch_rule_id")
        )
        checks["livekit_agent_registered"] = bool(
            sip and sip.get("agent_name") and sip.get("agent_name") == settings.livekit_agent_name
        )
        checks["livekit_worker_environment"] = bool(
            settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret
        )
        worker_health = urlsplit(settings.livekit_worker_health_url.strip())
        checks["livekit_worker_health_configured"] = bool(
            worker_health.scheme in {"http", "https"} and worker_health.netloc
        )
    labels = {
        "agent_active": "Activate the agent configuration.",
        "vav_speech_agent": "Select Inworld, Sarvam, or ElevenLabs as this agent's voice provider.",
        "provider_compatibility": provider_compatibility_blocker
        or "Save a supported runtime provider route.",
        "stt_credential": "Add a valid API key for the selected transcription provider.",
        "tts_credential": (
            f"Add a valid {_speech_provider_name(agent.voice_provider)} API key in Settings "
            "for speech output."
        ),
        "voice_selection": "Select a voice from the configured speech provider.",
        "speech_provider_match": (
            "Save the runtime so its primary speech provider matches the agent voice provider."
        ),
        "llm_credential": "Add a valid API key for the selected LLM route.",
        "public_runtime_url": "Configure BASE_URL as the public HTTPS API origin.",
        "telephony_credential": "Add credentials for the selected telephony provider in Settings.",
        "number_assigned": "Assign at least one E.164 phone number to the runtime.",
        "number_route_unique": (
            "Move the assigned phone number from its other active agent before activation."
        ),
        "knowledge_retrieval": (
            "Repair the bound knowledge base so every source has searchable text."
        ),
        "sip_gateway_provisioned": (
            "Enter the verified LiveKit inbound trunk and dispatch-rule IDs for the e& SIP trunk."
        ),
        "livekit_agent_registered": (
            f"Set the LiveKit dispatch agent name to {settings.livekit_agent_name}."
        ),
        "livekit_worker_environment": (
            "Configure server-only LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET "
            "on the API and LiveKit worker."
        ),
        "livekit_worker_health_configured": (
            "Configure LIVEKIT_WORKER_HEALTH_URL on the API with the LiveKit worker's "
            "private HTTP origin."
        ),
    }
    return [labels[name] for name, passed in checks.items() if not passed], checks


async def live_runtime_readiness(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile | None,
) -> tuple[list[str], dict[str, bool]]:
    """Run normal gates plus explicit, tightly bounded live provider probes."""
    blockers, checks = await runtime_readiness(db, agent, profile)
    if profile and profile.telephony_provider == "livekit_sip":
        checks["sip_route_live"] = False
        checks["livekit_worker_live"] = False
        try:
            sip = await load_provider_config(db, agent.tenant_id, "livekit_sip")
        except ProviderCredentialError:
            sip = None
        if all(
            checks.get(name)
            for name in (
                "telephony_credential",
                "sip_gateway_provisioned",
                "livekit_agent_registered",
                "livekit_worker_environment",
            )
        ):
            try:
                await LiveKitSIPProvider(
                    url=settings.livekit_url,
                    api_key=settings.livekit_api_key,
                    api_secret=settings.livekit_api_secret,
                ).verify_route(
                    inbound_trunk_id=str((sip or {}).get("inbound_trunk_id")),
                    dispatch_rule_id=str((sip or {}).get("dispatch_rule_id")),
                    outbound_trunk_id=str((sip or {}).get("outbound_trunk_id") or "") or None,
                    sip_uri=str((sip or {}).get("sip_uri") or ""),
                    agent_name=str((sip or {}).get("agent_name")),
                    assigned_numbers=list(profile.assigned_numbers or []),
                )
            except LiveKitSIPError as exc:
                blockers.append(f"LiveKit SIP route validation failed: {exc}")
            else:
                checks["sip_route_live"] = True
        else:
            blockers.append(
                "LiveKit SIP route cannot be tested until its configuration gates pass."
            )
        if all(
            checks.get(name)
            for name in (
                "livekit_agent_registered",
                "livekit_worker_environment",
                "livekit_worker_health_configured",
            )
        ):
            try:
                await LiveKitSIPProvider(
                    url=settings.livekit_url,
                    api_key=settings.livekit_api_key,
                    api_secret=settings.livekit_api_secret,
                ).verify_worker(
                    health_url=settings.livekit_worker_health_url,
                    agent_name=str((sip or {}).get("agent_name")),
                )
            except LiveKitSIPError as exc:
                blockers.append(f"LiveKit worker validation failed: {exc}")
            else:
                checks["livekit_worker_live"] = True
        else:
            blockers.append("LiveKit worker cannot be tested until its configuration gates pass.")
    if agent.voice_provider not in {"elevenlabs", "inworld"}:
        return blockers, checks

    checks["tts_provider_live"] = False
    provider = agent.voice_provider
    if provider == "inworld":
        checks["llm_provider_live"] = False
    if not checks.get("tts_credential") or not checks.get("voice_selection"):
        return blockers, checks
    if provider == "inworld" and not all(
        checks.get(name) for name in ("provider_compatibility", "llm_credential")
    ):
        return blockers, checks
    try:
        config = await load_provider_config(db, agent.tenant_id, provider)
    except ProviderCredentialError:
        if provider == "inworld":
            blockers.append(
                "Inworld workspace credential became unavailable during live readiness."
            )
            return blockers, checks
        config = None
    platform_key = (
        settings.elevenlabs_api_key if provider == "elevenlabs" else settings.inworld_api_key
    )
    api_key = str(
        ((config or {}).get("api_key") or "") if config is not None else platform_key
    ).strip()
    if provider == "elevenlabs":
        try:
            await ElevenLabsClient(api_key=api_key).synthesize_voice_preview(
                voice_id=agent.voice_id.removeprefix("elevenlabs:"),
                language=agent.language,
                speed=agent.speech_rate,
            )
        except ElevenLabsError as exc:
            blockers.append(f"ElevenLabs live synthesis failed: {exc}")
            return blockers, checks
        checks["tts_provider_live"] = True
        return blockers, checks

    inworld = InworldClient(api_key=api_key)
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    voice_runtime = str(runtime_config.get("voice_runtime") or "pipeline")
    if voice_runtime == "inworld_realtime":
        checks["realtime_provider_live"] = False
        configured_stt = str(runtime_config.get("stt_model") or "auto")
        if configured_stt == "auto":
            languages = {
                str(language or "").strip().lower().split("-", 1)[0]
                for language in (
                    list(agent.supported_languages or [])
                    + [agent.language, profile.stt_language]
                )
                if str(language or "").strip()
                and str(language or "").strip().lower() != "auto"
            }
            configured_stt = (
                "assemblyai/u3-rt-pro"
                if languages and languages.issubset({"en", "es", "fr", "de", "it", "pt"})
                else "soniox/stt-rt-v4"
            )
        try:
            await inworld.realtime_readiness_probe(
                model_id=profile.llm_model,
                voice_id=agent.voice_id.removeprefix("inworld:"),
                stt_model_id=configured_stt,
            )
        except InworldError as exc:
            blockers.append(f"Inworld native Realtime validation failed: {exc}")
        else:
            checks["realtime_provider_live"] = True
            checks["tts_provider_live"] = True
            checks["llm_provider_live"] = True
        return blockers, checks

    try:
        await inworld.synthesize_readiness_probe(
            voice_id=agent.voice_id.removeprefix("inworld:"),
            model_id=INWORLD_TTS_MODEL,
        )
    except InworldError as exc:
        blockers.append(f"Inworld live TTS synthesis failed: {exc}")
    else:
        checks["tts_provider_live"] = True

    assert profile is not None
    if profile.llm_provider == "inworld":
        try:
            await inworld.router_readiness_probe(model_id=profile.llm_model)
        except InworldError as exc:
            blockers.append(f"Inworld Router tool-calling check failed: {exc}")
        else:
            checks["llm_provider_live"] = True
        return blockers, checks

    try:
        openai_config = await load_provider_config(db, agent.tenant_id, "openai")
    except ProviderCredentialError:
        blockers.append("OpenAI workspace credential became unavailable during live readiness.")
        return blockers, checks
    openai_api_key = str(
        ((openai_config or {}).get("api_key") or "")
        if openai_config is not None
        else settings.openai_api_key
    ).strip()
    if not openai_api_key:
        return blockers, checks
    try:
        await OpenAIProviderClient(api_key=openai_api_key).tool_readiness_probe(
            model_id=profile.llm_model
        )
    except OpenAIProviderError as exc:
        blockers.append(f"OpenAI live tool-calling check failed: {exc}")
    else:
        checks["llm_provider_live"] = True
    return blockers, checks


def _response(
    agent: Agent,
    profile: AgentRuntimeProfile | None,
    blockers: list[str],
) -> RuntimeProfileResponse:
    values = {
        "telephony_provider": "livekit_sip" if agent.voice_provider == "inworld" else "twilio",
        "primary_speech_provider": (
            agent.voice_provider
            if agent.voice_provider in {"sarvam", "elevenlabs", "inworld"}
            else "sarvam"
        ),
        "fallback_speech_provider": None,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "voice_runtime": "pipeline",
        "stt_language": "auto",
        "stt_model": "auto",
        "tts_delivery_mode": "balanced",
        "max_concurrent_calls": 1,
        "daily_call_limit": 100,
        "monthly_budget_cents": 5000,
        "assigned_numbers": [],
    }
    if profile is not None:
        values.update(
            {
                key: getattr(profile, key)
                for key in values
                if key not in {"voice_runtime", "stt_model", "tts_delivery_mode"}
            }
        )
        runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
        voice_runtime = str(runtime_config.get("voice_runtime") or "pipeline")
        values["voice_runtime"] = (
            voice_runtime
            if voice_runtime in {"pipeline", "inworld_realtime"}
            else "pipeline"
        )
        stt_model = str(runtime_config.get("stt_model") or "auto")
        values["stt_model"] = (
            stt_model
            if stt_model
            in {
                "auto",
                "assemblyai/u3-rt-pro",
                "soniox/stt-rt-v4",
                "inworld/inworld-stt-1",
            }
            else "auto"
        )
        delivery_mode = str(runtime_config.get("tts_delivery_mode") or "balanced").lower()
        values["tts_delivery_mode"] = (
            delivery_mode if delivery_mode in {"stable", "balanced", "creative"} else "balanced"
        )
    return RuntimeProfileResponse(
        id=profile.id if profile else None,
        agent_id=agent.id,
        enabled=bool(profile and profile.enabled),
        status=profile.status if profile else "draft",
        # A live provider/SIP probe can fail even when static configuration is
        # complete. Keep that profile blocked until the operator reruns Test
        # readiness successfully or activation itself passes every live gate.
        ready=not blockers and not bool(profile and profile.status == "blocked"),
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
        result.append(_response(agent, profile, blockers))
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
    return _response(agent, profile, blockers)


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
    payload = data.model_dump(exclude={"voice_runtime", "stt_model", "tts_delivery_mode"})
    for key, value in payload.items():
        setattr(profile, key, value)
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    if "voice_runtime" in data.model_fields_set:
        runtime_config = {
            **runtime_config,
            "voice_runtime": data.voice_runtime,
        }
    if "stt_model" in data.model_fields_set:
        runtime_config = {
            **runtime_config,
            "stt_model": data.stt_model,
        }
    if "tts_delivery_mode" in data.model_fields_set:
        runtime_config = {
            **runtime_config,
            "tts_delivery_mode": data.tts_delivery_mode,
        }
    profile.runtime_config = runtime_config
    delivery_mode = str(runtime_config.get("tts_delivery_mode") or "balanced").lower()
    if delivery_mode not in {"stable", "balanced", "creative"}:
        delivery_mode = "balanced"
    provider_compatibility_blocker = _runtime_provider_blocker(agent, profile)
    if provider_compatibility_blocker:
        raise HTTPException(status_code=422, detail=provider_compatibility_blocker)
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
            "voice_runtime": data.voice_runtime,
            "stt_model": data.stt_model,
            "tts_delivery_mode": delivery_mode,
        },
    )
    blockers, _checks = await runtime_readiness(db, agent, profile)
    return _response(agent, profile, blockers)


@router.post("/agents/{agent_id}/test", response_model=RuntimeReadinessResponse)
async def test_runtime_profile(
    agent_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    await enforce_rate_limit(
        request,
        scope="runtime-live-readiness",
        limit=5,
        window_seconds=60,
        subject=f"{current_user.tenant_id}:{current_user.id}",
        bind_to_client=False,
        limit_detail="Too many live readiness tests. Wait one minute and try again.",
        unavailable_detail="Live readiness testing is temporarily unavailable.",
    )
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id, create=True)
    assert profile is not None
    blockers, checks = await live_runtime_readiness(db, agent, profile)
    tested_at = datetime.now(UTC)
    profile.last_tested_at = tested_at
    if blockers:
        # A readiness test is observational for a route that is already live.
        # Transient provider/API failures must be visible, but an ordinary test
        # must never silently remove an active inbound route. Activation still
        # fails closed, and administrators can deactivate explicitly.
        if not profile.enabled:
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
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    await enforce_rate_limit(
        request,
        scope="runtime-live-readiness",
        limit=5,
        window_seconds=60,
        subject=f"{current_user.tenant_id}:{current_user.id}",
        bind_to_client=False,
        limit_detail="Too many live readiness tests. Wait one minute and try again.",
        unavailable_detail="Live readiness testing is temporarily unavailable.",
    )
    agent = await _agent(db, current_user.tenant_id, agent_id)
    profile = await _profile(db, current_user.tenant_id, agent_id, create=True)
    assert profile is not None
    await _lock_number_routes(db, agent, profile)
    blockers, _checks = await live_runtime_readiness(db, agent, profile)
    if blockers:
        profile.enabled = False
        profile.status = "blocked"
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="agent.runtime_activation_blocked",
            resource_type="agent",
            resource_id=str(agent.id),
            details={"blockers": blockers},
        )
        # FastAPI's session dependency rolls back on HTTPException. Commit the
        # fail-closed state before returning 409 so a formerly active route
        # cannot remain enabled after a failed re-activation check.
        await db.commit()
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
    return _response(agent, profile, blockers)


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
    return _response(agent, profile, blockers)


def _platform_credential_config(provider: str) -> dict[str, str]:
    if provider == "smallest":
        return {"api_key": settings.smallest_api_key}
    if provider == "sarvam":
        return {"api_key": settings.sarvam_api_key}
    if provider == "elevenlabs":
        return {"api_key": settings.elevenlabs_api_key}
    if provider == "inworld":
        return {"api_key": settings.inworld_api_key}
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
    for provider in ("smallest", "sarvam", "elevenlabs", "inworld", "openai", "twilio"):
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
    if provider not in {"smallest", "sarvam", "elevenlabs", "inworld", "openai"}:
        raise HTTPException(status_code=404, detail="Unsupported API-key provider")
    if provider == "elevenlabs":
        try:
            await ElevenLabsClient(api_key=data.api_key).validate_connection()
        except ElevenLabsError as exc:
            # Provider authentication failures must not look like an expired VAV
            # login to the browser. Return a field-level validation failure instead.
            status_code = 422 if exc.status_code in {400, 401, 403, 422} else exc.status_code
            raise HTTPException(
                status_code=status_code,
                detail=f"ElevenLabs API key validation failed: {exc}",
            ) from exc
    if provider == "inworld":
        try:
            await InworldClient(api_key=data.api_key).validate_connection()
        except InworldError as exc:
            status_code = 422 if exc.status_code in {400, 401, 403, 422} else exc.status_code
            raise HTTPException(
                status_code=status_code,
                detail=f"Inworld API key validation failed: {exc}",
            ) from exc
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
    if provider not in {"smallest", "sarvam", "elevenlabs", "inworld", "openai", "twilio"}:
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
    config = None
    if credential and credential.is_active:
        try:
            config = await load_provider_config(db, current_user.tenant_id, "livekit_sip")
        except ProviderCredentialError:
            config = None
    inbound_trunk = str((config or {}).get("inbound_trunk_id") or "")
    dispatch_rule = str((config or {}).get("dispatch_rule_id") or "")
    return SipCredentialStatus(
        configured=bool(credential and credential.is_active),
        route_recorded=bool(inbound_trunk and dispatch_rule),
        gateway_provisioned=False,
        inbound_trunk_hint=f"••••{inbound_trunk[-6:]}" if inbound_trunk else None,
        dispatch_rule_hint=f"••••{dispatch_rule[-6:]}" if dispatch_rule else None,
        agent_name=str((config or {}).get("agent_name") or "") or None,
        updated_at=credential.updated_at if credential else None,
    )


@router.put("/sip/credential", response_model=SipCredentialStatus)
async def save_sip_credential(
    data: SipCredentialRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    if db.get_bind().dialect.name == "postgresql":
        for route_id in sorted((data.inbound_trunk_id, data.dispatch_rule_id)):
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:route_key, 0))"),
                {"route_key": f"livekit-route:{route_id}"},
            )
    other_routes = (
        await db.execute(
            select(ProviderCredential).where(
                ProviderCredential.provider == "livekit_sip",
                ProviderCredential.is_active.is_(True),
                ProviderCredential.tenant_id != current_user.tenant_id,
            )
        )
    ).scalars()
    for other in other_routes:
        try:
            config = decrypt_integration_config(other.encrypted_config)
        except IntegrationConfigUnavailableError:
            # An unreadable active route cannot be proven non-conflicting.
            raise HTTPException(
                status_code=409,
                detail="Another active LiveKit route cannot be safely validated",
            ) from None
        if (
            str(config.get("inbound_trunk_id") or "") == data.inbound_trunk_id
            or str(config.get("dispatch_rule_id") or "") == data.dispatch_rule_id
        ):
            raise HTTPException(
                status_code=409,
                detail="This LiveKit trunk or dispatch rule is already assigned",
            )
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
    return SipCredentialStatus(
        configured=True,
        route_recorded=True,
        gateway_provisioned=False,
        inbound_trunk_hint=f"••••{data.inbound_trunk_id[-6:]}",
        dispatch_rule_hint=f"••••{data.dispatch_rule_id[-6:]}",
        agent_name=data.agent_name,
        updated_at=credential.updated_at,
    )


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
