"""Operator controls for VAV-hosted realtime agents and SIP connectivity."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.livekit_runtime.inworld_single_pass import (
    InworldTurnMode,
    decide_single_pass_runtime,
)
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    AgentRuntimeProfile,
    KnowledgeBase,
    KnowledgeProviderCleanup,
    KnowledgeServingRevisionSource,
    KnowledgeSource,
)
from app.models.provider_credential import ProviderCredential
from app.providers.elevenlabs import ElevenLabsClient, ElevenLabsError
from app.providers.inworld import INWORLD_TTS_MODEL, InworldClient, InworldError
from app.providers.openai import OpenAIProviderClient, OpenAIProviderError
from app.providers.sarvam import SarvamAIClient, SarvamAIError
from app.realtime.sarvam_stream import SarvamStreamError, SarvamSTTStream
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
from app.services.knowledge_serving import (
    KnowledgeServingError,
    knowledge_call_reservation_metadata,
    load_durably_admitted_serving_revision,
    validate_call_speech_lexicon_reservation,
)
from app.services.provider_credentials import (
    ProviderCredentialError,
    get_provider_credential,
    invalidate_active_runtimes_for_credential,
    load_provider_config,
    lock_provider_cleanup_boundary,
    lock_provider_runtime_boundaries,
    store_provider_config,
)
from app.services.rate_limit import enforce_rate_limit
from app.services.realtime_speech_config import (
    inworld_stt_wire_language,
    resolve_inworld_stt_model,
    sarvam_stt_wire_language,
)
from app.services.recording_policy import (
    DIAGNOSTIC_RECORDING_OFF,
    diagnostic_recording_mode,
)
from app.services.twilio_route_security import (
    TwilioRouteVerificationError,
    active_twilio_route_conflicts,
    load_workspace_twilio_route_credential,
    lock_twilio_route_claims,
    mark_twilio_route_verified,
    twilio_route_verification_fingerprint,
    twilio_route_verification_is_current,
    verify_twilio_route_ownership,
)
from app.telephony.livekit_provider import LiveKitSIPError, LiveKitSIPProvider

router = APIRouter(prefix="/runtime", tags=["Realtime Runtime"])

_SMALLEST_CLEANUP_PROVIDER = "smallest"
_TWILIO_REVERIFICATION_BLOCKER = (
    "Verify this workspace's Twilio credentials and assigned numbers before activation."
)
_NATIVE_LIVE_PROBE_TIMEOUT_SECONDS = 12.0


def _twilio_inbound_voice_url() -> str:
    return f"{settings.base_url.rstrip('/')}/api/v1/webhooks/twilio/voice/inbound"


async def _has_pending_smallest_cleanup(db: AsyncSession, tenant_id: UUID) -> bool:
    cleanup_id = await db.scalar(
        select(KnowledgeProviderCleanup.id)
        .where(
            KnowledgeProviderCleanup.tenant_id == tenant_id,
            KnowledgeProviderCleanup.provider == _SMALLEST_CLEANUP_PROVIDER,
            KnowledgeProviderCleanup.status != "completed",
        )
        .limit(1)
    )
    return cleanup_id is not None


def _smallest_cleanup_credential_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "Smallest credential cannot be changed while remote knowledge cleanup "
            "is pending. Wait for Knowledge Studio cleanup to finish, then retry."
        ),
    )


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


async def _live_api_key(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
    platform_key: str,
) -> tuple[str, bool]:
    """Resolve a live-probe key without bypassing an unreadable workspace secret."""

    try:
        config = await load_provider_config(db, tenant_id, provider)
    except ProviderCredentialError:
        return "", True
    if config is not None:
        return str(config.get("api_key") or "").strip(), False
    return str(platform_key or "").strip(), False


async def _sarvam_stt_readiness_probe(*, api_key: str, language_code: str) -> None:
    """Prove the same bounded Saaras WebSocket handshake used by paid calls."""

    async with SarvamSTTStream(
        api_key=api_key,
        base_url=settings.sarvam_base_url,
        language_code=language_code,
    ):
        return


async def _bounded_native_live_probe(operation: Any) -> Exception | None:
    """Return a probe failure while preserving request cancellation semantics."""

    try:
        await asyncio.wait_for(operation, timeout=_NATIVE_LIVE_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return exc
    return None


def _native_live_probe_failure(
    *,
    label: str,
    error: Exception,
    provider_errors: tuple[type[Exception], ...],
) -> str:
    if isinstance(error, TimeoutError):
        return f"{label} timed out after {_NATIVE_LIVE_PROBE_TIMEOUT_SECONDS:g} seconds."
    if isinstance(error, provider_errors):
        return f"{label} failed: {error}"
    return f"{label} failed unexpectedly."


async def _native_speech_live_readiness(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile | None,
    blockers: list[str],
    checks: dict[str, bool],
) -> tuple[list[str], dict[str, bool]]:
    """Prove every paid provider boundary in Sarvam and ElevenLabs calls."""

    provider = agent.voice_provider
    checks["tts_provider_live"] = False
    checks["stt_provider_live"] = False
    checks["llm_provider_live"] = False
    if provider == "elevenlabs":
        checks["fallback_tts_provider_live"] = False

    if profile is None or not checks.get("provider_compatibility"):
        blockers.append(
            "Native speech providers cannot be tested until the runtime provider route matches."
        )
        return blockers, checks

    sarvam_key, sarvam_unreadable = await _live_api_key(
        db,
        agent.tenant_id,
        "sarvam",
        settings.sarvam_api_key,
    )
    elevenlabs_key = ""
    elevenlabs_unreadable = False
    if provider == "elevenlabs":
        elevenlabs_key, elevenlabs_unreadable = await _live_api_key(
            db,
            agent.tenant_id,
            "elevenlabs",
            settings.elevenlabs_api_key,
        )
    openai_key, openai_unreadable = await _live_api_key(
        db,
        agent.tenant_id,
        "openai",
        settings.openai_api_key,
    )

    probes: dict[str, tuple[Any, str, tuple[type[Exception], ...]]] = {}
    if provider == "sarvam":
        if sarvam_unreadable:
            blockers.append(
                "Sarvam live TTS synthesis cannot run because the workspace credential "
                "is unreadable."
            )
        elif not sarvam_key:
            blockers.append(
                "Sarvam live TTS synthesis cannot be tested until its API key is configured."
            )
        elif checks.get("voice_selection"):
            probes["tts_provider_live"] = (
                SarvamAIClient(api_key=sarvam_key).synthesize_voice_preview(
                    speaker=agent.voice_id.removeprefix("sarvam:"),
                    language=agent.language,
                    pace=agent.speech_rate,
                ),
                "Sarvam live TTS synthesis",
                (SarvamAIError,),
            )
        else:
            blockers.append(
                "Sarvam live TTS synthesis cannot be tested until a Sarvam voice is selected."
            )
    else:
        if elevenlabs_unreadable:
            blockers.append(
                "ElevenLabs live TTS synthesis cannot run because the workspace credential "
                "is unreadable."
            )
        elif not elevenlabs_key:
            blockers.append(
                "ElevenLabs live TTS synthesis cannot be tested until its API key is configured."
            )
        elif checks.get("voice_selection"):
            probes["tts_provider_live"] = (
                ElevenLabsClient(api_key=elevenlabs_key).synthesize_voice_preview(
                    voice_id=agent.voice_id.removeprefix("elevenlabs:"),
                    language=agent.language,
                    speed=agent.speech_rate,
                ),
                "ElevenLabs live TTS synthesis",
                (ElevenLabsError,),
            )
        else:
            blockers.append(
                "ElevenLabs live TTS synthesis cannot be tested until an ElevenLabs voice "
                "is selected."
            )

        if sarvam_unreadable:
            blockers.append(
                "Sarvam emergency TTS synthesis cannot run because the workspace credential "
                "is unreadable."
            )
        elif not sarvam_key:
            blockers.append(
                "Sarvam emergency TTS synthesis cannot be tested until its API key is configured."
            )
        else:
            probes["fallback_tts_provider_live"] = (
                SarvamAIClient(api_key=sarvam_key).synthesize_voice_preview(
                    speaker="ishita",
                    language=agent.language,
                    pace=agent.speech_rate,
                ),
                "Sarvam emergency TTS synthesis",
                (SarvamAIError,),
            )

    if sarvam_unreadable:
        blockers.append(
            "Sarvam Saaras realtime STT validation cannot run because the workspace "
            "credential is unreadable."
        )
    elif not sarvam_key:
        blockers.append(
            "Sarvam Saaras realtime STT validation cannot be tested until its API key is "
            "configured."
        )
    else:
        try:
            stt_language = sarvam_stt_wire_language(model=agent, profile=profile)
        except SarvamAIError as exc:
            blockers.append(f"Sarvam Saaras realtime STT configuration is invalid: {exc}")
        else:
            probes["stt_provider_live"] = (
                _sarvam_stt_readiness_probe(
                    api_key=sarvam_key,
                    language_code=stt_language,
                ),
                "Sarvam Saaras realtime STT validation",
                (SarvamStreamError,),
            )

    if openai_unreadable:
        blockers.append(
            "OpenAI live tool-calling validation cannot run because the workspace credential "
            "is unreadable."
        )
    elif not openai_key:
        blockers.append(
            "OpenAI live tool-calling validation cannot be tested until its API key is configured."
        )
    elif profile.llm_provider != "openai":
        blockers.append(
            "OpenAI live tool-calling validation requires the OpenAI LLM runtime route."
        )
    else:
        probes["llm_provider_live"] = (
            OpenAIProviderClient(api_key=openai_key).tool_readiness_probe(
                model_id=profile.llm_model
            ),
            "OpenAI live tool-calling check",
            (OpenAIProviderError,),
        )

    if not probes:
        return blockers, checks
    names = list(probes)
    results = await asyncio.gather(*(_bounded_native_live_probe(probes[name][0]) for name in names))
    for name, error in zip(names, results, strict=True):
        if error is None:
            checks[name] = True
            continue
        _operation, label, provider_errors = probes[name]
        blockers.append(
            _native_live_probe_failure(
                label=label,
                error=error,
                provider_errors=provider_errors,
            )
        )
    return blockers, checks


def _diagnostic_recording_mode(profile: AgentRuntimeProfile | None) -> str:
    """Return the governed mode without trusting arbitrary legacy JSON."""
    return diagnostic_recording_mode(profile)


def _knowledge_turn_mode(profile: AgentRuntimeProfile | None) -> str:
    """Map the public mode onto the strict persisted experiment boolean."""

    runtime_config = (
        profile.runtime_config
        if profile is not None and isinstance(profile.runtime_config, dict)
        else {}
    )
    return (
        InworldTurnMode.SINGLE_PASS.value
        if runtime_config.get("inworld_single_pass") is True
        else InworldTurnMode.TOOL_LOOP.value
    )


def _knowledge_turn_audit_details(
    profile: AgentRuntimeProfile | None,
) -> dict[str, Any]:
    runtime_config = (
        profile.runtime_config
        if profile is not None and isinstance(profile.runtime_config, dict)
        else {}
    )
    voice_runtime = str(runtime_config.get("voice_runtime") or "pipeline")
    decision = decide_single_pass_runtime(runtime_config, voice_runtime=voice_runtime)
    details: dict[str, Any] = {
        "knowledge_turn_mode": _knowledge_turn_mode(profile),
        "inworld_single_pass": runtime_config.get("inworld_single_pass") is True,
        "knowledge_turn_mode_supported": decision.mode != InworldTurnMode.BLOCKED,
    }
    if decision.blocker:
        details["knowledge_turn_mode_blocker"] = decision.blocker
    return details


def _diagnostic_recording_readiness(
    profile: AgentRuntimeProfile | None,
) -> tuple[dict[str, bool], dict[str, str]]:
    """Fail closed until capture, consent, storage, and deletion are real.

    This control plane currently stores only an operator's policy intent.  It
    does not start LiveKit Egress, write audio, or make VAV playback available.
    Keep each missing prerequisite explicit so a future implementation cannot
    accidentally treat a saved opt-in as permission or operational readiness.
    """
    if _diagnostic_recording_mode(profile) == DIAGNOSTIC_RECORDING_OFF:
        return {}, {}

    checks = {
        "diagnostic_recording_livekit_transport": bool(
            profile and profile.telephony_provider == "livekit_sip"
        ),
        # No current call boundary requires and persists an explicit grant
        # before capture. Absence of a ConsentRecord must never become consent.
        "diagnostic_recording_explicit_consent_enforced": False,
        # The installed LiveKit SDK is used for rooms/SIP only. There is no
        # governed Egress start/stop implementation in the deployed runtime.
        "diagnostic_recording_egress_configured": False,
        # Provider-hosted playback URLs are not a verified VAV-owned output
        # destination for LiveKit Egress.
        "diagnostic_recording_storage_configured": False,
        "diagnostic_recording_retention_enforced": False,
    }
    labels = {
        "diagnostic_recording_livekit_transport": (
            "Diagnostic audio requires the LiveKit SIP telephony route."
        ),
        "diagnostic_recording_explicit_consent_enforced": (
            "Implement and verify per-call explicit recording consent before capture; "
            "the absence of consent is not permission."
        ),
        "diagnostic_recording_egress_configured": (
            "Provision and verify a governed LiveKit Egress start/stop lifecycle."
        ),
        "diagnostic_recording_storage_configured": (
            "Configure and verify an encrypted, tenant-approved regional recording "
            "destination; this policy does not provide VAV playback."
        ),
        "diagnostic_recording_retention_enforced": (
            "Configure and verify recording retention, deletion, and access-audit controls."
        ),
    }
    return checks, labels


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
        single_pass = decide_single_pass_runtime(
            runtime_config,
            voice_runtime=voice_runtime,
        )
        if single_pass.blocker:
            return single_pass.blocker
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


async def _lock_runtime_mutation(
    db: AsyncSession,
    tenant_id: UUID,
    agent_id: UUID,
    *,
    create_profile: bool = False,
) -> tuple[Agent, AgentRuntimeProfile | None]:
    """Serialize every runtime configuration state transition for one agent."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:route_key, 0))"),
            {"route_key": f"agent-runtime:{tenant_id}:{agent_id}"},
        )
    agent = await db.scalar(
        select(Agent)
        .where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    profile = await db.scalar(
        select(AgentRuntimeProfile)
        .where(
            AgentRuntimeProfile.agent_id == agent_id,
            AgentRuntimeProfile.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if profile is None and create_profile:
        profile = AgentRuntimeProfile(tenant_id=tenant_id, agent_id=agent_id)
        db.add(profile)
        await db.flush()
    return agent, profile


def _activation_configuration_fingerprint(
    agent: Agent,
    profile: AgentRuntimeProfile,
    *,
    dependency_fingerprints: dict[str, str],
) -> str:
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    runtime_config = {
        key: value for key, value in runtime_config.items() if key != "twilio_route_verification"
    }
    payload = {
        "agent": {
            "is_active": agent.is_active,
            "voice_provider": agent.voice_provider,
            "voice_id": agent.voice_id,
            "language": agent.language,
            "supported_languages": agent.supported_languages,
        },
        "profile": {
            "enabled": profile.enabled,
            "status": profile.status,
            "telephony_provider": profile.telephony_provider,
            "primary_speech_provider": profile.primary_speech_provider,
            "fallback_speech_provider": profile.fallback_speech_provider,
            "llm_provider": profile.llm_provider,
            "llm_model": profile.llm_model,
            "stt_language": profile.stt_language,
            "max_concurrent_calls": profile.max_concurrent_calls,
            "daily_call_limit": profile.daily_call_limit,
            "monthly_budget_cents": profile.monthly_budget_cents,
            "assigned_numbers": profile.assigned_numbers,
            "runtime_config": runtime_config,
        },
        "dependency_fingerprints": dependency_fingerprints,
    }
    return hashlib.sha256(
        json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _runtime_dependency_providers(profile: AgentRuntimeProfile) -> set[str]:
    providers = {
        profile.primary_speech_provider,
        profile.llm_provider,
        profile.telephony_provider,
    }
    if profile.fallback_speech_provider:
        providers.add(profile.fallback_speech_provider)
    if profile.primary_speech_provider == "elevenlabs":
        providers.add("sarvam")
    return providers.intersection(
        {"sarvam", "elevenlabs", "inworld", "openai", "twilio", "livekit_sip"}
    )


def _config_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, default=str, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


async def _activation_dependency_fingerprints(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile,
    *,
    for_update: bool = False,
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for provider in sorted(_runtime_dependency_providers(profile)):
        try:
            config = await load_provider_config(
                db,
                agent.tenant_id,
                provider,
                for_update=for_update,
            )
        except ProviderCredentialError:
            effective_config: dict[str, Any] = {"state": "unreadable"}
        else:
            if config is not None:
                effective_config = {"source": "workspace", "config": config}
            elif provider in {"twilio", "livekit_sip"}:
                effective_config = {"source": "none", "config": {}}
            else:
                effective_config = {
                    "source": "platform",
                    "config": _platform_credential_config(provider),
                }
        fingerprints[provider] = _config_fingerprint(effective_config)
    if profile.telephony_provider == "livekit_sip":
        fingerprints["livekit_environment"] = _config_fingerprint(
            {
                "url": settings.livekit_url,
                "api_key": settings.livekit_api_key,
                "api_secret": settings.livekit_api_secret,
                "agent_name": settings.livekit_agent_name,
                "worker_health_url": settings.livekit_worker_health_url,
            }
        )
    return fingerprints


async def _activation_snapshot_fingerprint(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile,
    *,
    lock_credential: bool = False,
) -> str:
    dependency_fingerprints = await _activation_dependency_fingerprints(
        db,
        agent,
        profile,
        for_update=lock_credential,
    )
    if profile.telephony_provider == "twilio":
        try:
            credential = await load_workspace_twilio_route_credential(
                db,
                agent.tenant_id,
                for_update=lock_credential,
            )
        except ProviderCredentialError:
            credential = None
        if credential is not None:
            dependency_fingerprints["twilio_route"] = twilio_route_verification_fingerprint(
                credential,
                list(profile.assigned_numbers or []),
                expected_voice_url=_twilio_inbound_voice_url(),
            )
    return _activation_configuration_fingerprint(
        agent,
        profile,
        dependency_fingerprints=dependency_fingerprints,
    )


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
    *,
    verify_legacy_twilio_claims: bool = False,
) -> list[Agent]:
    """Return active agents that already own one of this profile's phone routes."""
    if profile is None or not profile.assigned_numbers:
        return []

    if profile.telephony_provider == "twilio":
        try:
            credential = await load_workspace_twilio_route_credential(db, agent.tenant_id)
        except ProviderCredentialError:
            return []
        if credential is None:
            return []
        return await active_twilio_route_conflicts(
            db,
            agent_id=agent.id,
            account_sid=credential.account_sid,
            assigned_numbers=list(profile.assigned_numbers),
            expected_voice_url=_twilio_inbound_voice_url(),
            verify_legacy_claims=verify_legacy_twilio_claims,
        )

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
    """Serialize activation for the route's real ownership boundary."""
    if profile.telephony_provider == "twilio":
        try:
            credential = await load_workspace_twilio_route_credential(
                db,
                agent.tenant_id,
                for_update=True,
            )
        except ProviderCredentialError:
            return
        if credential is None:
            return
        await lock_twilio_route_claims(
            db,
            credential=credential,
            assigned_numbers=list(profile.assigned_numbers or []),
        )
        return

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
    *,
    verify_legacy_twilio_claims: bool = False,
) -> tuple[list[str], dict[str, bool]]:
    try:
        sarvam_config = await load_provider_config(db, agent.tenant_id, "sarvam")
    except ProviderCredentialError:
        sarvam_config = None
        sarvam_config_unreadable = True
    else:
        sarvam_config_unreadable = False
    sarvam_ready = not sarvam_config_unreadable and _api_key_configured(
        sarvam_config, settings.sarvam_api_key
    )
    try:
        elevenlabs_config = await load_provider_config(db, agent.tenant_id, "elevenlabs")
    except ProviderCredentialError:
        elevenlabs_config = None
        elevenlabs_config_unreadable = True
    else:
        elevenlabs_config_unreadable = False
    elevenlabs_ready = not elevenlabs_config_unreadable and _api_key_configured(
        elevenlabs_config, settings.elevenlabs_api_key
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
    # Every VAV-owned realtime lane retrieves from the local knowledge engine,
    # regardless of which speech provider renders the call. New Sarvam,
    # ElevenLabs, and Inworld sessions all require the same immutable release;
    # mutable approval-only rows remain a bounded compatibility concern only
    # for already-persisted legacy jobs.
    immutable_knowledge_required = bool(
        profile and profile.primary_speech_provider in {"sarvam", "elevenlabs", "inworld"}
    )
    knowledge_ready = not immutable_knowledge_required
    if knowledge_binding is not None:
        approval_clause = (
            KnowledgeBase.serving_revision_id.is_not(None)
            if immutable_knowledge_required
            else or_(
                KnowledgeBase.serving_revision_id.is_not(None),
                KnowledgeBase.approval_status == "approved",
            )
        )
        bound_knowledge = (
            await db.execute(
                select(
                    KnowledgeBase.id,
                    KnowledgeBase.serving_revision_id,
                    KnowledgeBase.serving_revocation_generation,
                ).where(
                    KnowledgeBase.id == knowledge_binding.knowledge_base_id,
                    KnowledgeBase.tenant_id == agent.tenant_id,
                    KnowledgeBase.is_active.is_(True),
                    approval_clause,
                )
            )
        ).one_or_none()
        if bound_knowledge is None:
            knowledge_ready = False
        else:
            knowledge_base_id, serving_revision_id, revocation_generation = bound_knowledge
            if serving_revision_id is not None:
                serving_revision = await load_durably_admitted_serving_revision(
                    db,
                    tenant_id=agent.tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    serving_revision_id=serving_revision_id,
                    include_sources=False,
                )
                try:
                    if serving_revision is None:
                        raise KnowledgeServingError("Knowledge serving revision is unavailable")
                    reservation = knowledge_call_reservation_metadata(
                        serving_revision,
                        revocation_generation,
                    )
                    await validate_call_speech_lexicon_reservation(
                        db,
                        tenant_id=agent.tenant_id,
                        knowledge_base_id=knowledge_base_id,
                        revision=serving_revision,
                        metadata={"runtime": reservation},
                    )
                except KnowledgeServingError:
                    knowledge_ready = False
                else:
                    source_groups = (
                        await db.execute(
                            select(
                                KnowledgeServingRevisionSource.knowledge_base_id,
                                func.count(KnowledgeServingRevisionSource.id),
                            )
                            .where(
                                KnowledgeServingRevisionSource.serving_revision_id
                                == serving_revision_id,
                                KnowledgeServingRevisionSource.tenant_id == agent.tenant_id,
                            )
                            .group_by(KnowledgeServingRevisionSource.knowledge_base_id)
                        )
                    ).all()
                    # Published source snapshots are immutable and carry
                    # non-null content hashes. Requiring one ownership group
                    # with the exact published count proves the complete
                    # release is retained and prevents a mis-owned row from
                    # entering retrieval, without de-TOASTing every potentially
                    # multi-megabyte body on readiness.
                    knowledge_ready = (
                        serving_revision.source_count > 0
                        and len(source_groups) == 1
                        and source_groups[0].knowledge_base_id == knowledge_base_id
                        and int(source_groups[0][1] or 0) == serving_revision.source_count
                    )
            else:
                # Preserve Python's Unicode-aware ``strip`` semantics for the
                # bounded legacy compatibility path. Current VAV providers use
                # immutable releases above and never load their source bodies.
                source_states = (
                    await db.execute(
                        select(KnowledgeSource.status, KnowledgeSource.content).where(
                            KnowledgeSource.knowledge_base_id == knowledge_base_id,
                            KnowledgeSource.tenant_id == agent.tenant_id,
                        )
                    )
                ).all()
                knowledge_ready = bool(source_states) and all(
                    status in {"processing", "indexed", "local_only"}
                    and bool(str(content or "").strip())
                    for status, content in source_states
                )

    number_route_conflicts = await _number_route_conflicts(
        db,
        agent,
        profile,
        verify_legacy_twilio_claims=verify_legacy_twilio_claims,
    )

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
    runtime_config = (
        profile.runtime_config
        if profile is not None and isinstance(profile.runtime_config, dict)
        else {}
    )
    knowledge_turn_decision = decide_single_pass_runtime(
        runtime_config,
        voice_runtime=str(runtime_config.get("voice_runtime") or "pipeline"),
    )
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
        "knowledge_turn_mode_supported": knowledge_turn_decision.mode != InworldTurnMode.BLOCKED,
    }
    recording_checks, recording_labels = _diagnostic_recording_readiness(profile)
    checks.update(recording_checks)
    if profile and profile.telephony_provider == "twilio":
        # A self-service DID route must prove ownership with this workspace's
        # own account credential. Shared platform credentials are never a
        # tenant-routing authority.
        checks["telephony_credential"] = bool(
            twilio_config and twilio_config.get("account_sid") and twilio_config.get("auth_token")
        )
        try:
            route_credential = await load_workspace_twilio_route_credential(
                db,
                agent.tenant_id,
            )
        except ProviderCredentialError:
            route_credential = None
        checks["twilio_route_verification_current"] = bool(
            route_credential
            and twilio_route_verification_is_current(
                profile,
                route_credential,
                expected_voice_url=_twilio_inbound_voice_url(),
            )
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
        "telephony_credential": (
            "Add this workspace's own Twilio account SID and auth token in Settings."
            if profile and profile.telephony_provider == "twilio"
            else "Add credentials for the selected telephony provider in Settings."
        ),
        "number_assigned": "Assign at least one E.164 phone number to the runtime.",
        "number_route_unique": (
            "Move the assigned phone number from its other active agent before activation."
        ),
        "twilio_route_verification_current": _TWILIO_REVERIFICATION_BLOCKER,
        "knowledge_retrieval": (
            (
                "Approve and publish the bound knowledge base so it has an immutable "
                "serving revision; backfill legacy approved knowledge before activation."
            )
            if immutable_knowledge_required
            else "Repair the bound knowledge base so every source has searchable text."
        ),
        "knowledge_turn_mode_supported": knowledge_turn_decision.blocker
        or "Select a supported knowledge turn mode.",
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
        **recording_labels,
    }
    return [labels[name] for name, passed in checks.items() if not passed], checks


async def live_runtime_readiness(
    db: AsyncSession,
    agent: Agent,
    profile: AgentRuntimeProfile | None,
    *,
    persist_twilio_verification: bool = True,
) -> tuple[list[str], dict[str, bool]]:
    """Run normal gates plus explicit, tightly bounded live provider probes."""
    blockers, checks = await runtime_readiness(db, agent, profile)
    if profile and profile.telephony_provider == "twilio":
        live_conflicts = await _number_route_conflicts(
            db,
            agent,
            profile,
            verify_legacy_twilio_claims=True,
        )
        if live_conflicts and checks.get("number_route_unique", True):
            checks["number_route_unique"] = False
            blockers.append(
                "Move the assigned phone number from its other active agent before activation."
            )
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
    if profile and profile.telephony_provider == "twilio" and "telephony_credential" in checks:
        checks["twilio_route_live"] = False
        if all(
            checks.get(name)
            for name in (
                "telephony_credential",
                "number_assigned",
                "number_route_unique",
            )
        ):
            try:
                credential = await load_workspace_twilio_route_credential(
                    db,
                    agent.tenant_id,
                )
                if credential is None:
                    raise TwilioRouteVerificationError(
                        "Twilio workspace credentials are unavailable"
                    )
                await verify_twilio_route_ownership(
                    credential=credential,
                    assigned_numbers=list(profile.assigned_numbers or []),
                    expected_voice_url=_twilio_inbound_voice_url(),
                )
            except (ProviderCredentialError, TwilioRouteVerificationError) as exc:
                blockers.append(f"Twilio inbound route validation failed: {exc}")
            else:
                if persist_twilio_verification:
                    mark_twilio_route_verified(
                        profile,
                        credential,
                        expected_voice_url=_twilio_inbound_voice_url(),
                    )
                checks["twilio_route_live"] = True
                checks["twilio_route_verification_current"] = True
                blockers = [
                    blocker for blocker in blockers if blocker != _TWILIO_REVERIFICATION_BLOCKER
                ]
        else:
            blockers.append(
                "Twilio inbound route cannot be tested until its credential, number, and "
                "uniqueness gates pass."
            )
    if agent.voice_provider in {"sarvam", "elevenlabs"}:
        return await _native_speech_live_readiness(
            db,
            agent,
            profile,
            blockers,
            checks,
        )
    if agent.voice_provider != "inworld":
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
    inworld = InworldClient(api_key=api_key)
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    voice_runtime = str(runtime_config.get("voice_runtime") or "pipeline")
    if voice_runtime == "inworld_realtime":
        checks["realtime_provider_live"] = False
        single_pass_enabled = _knowledge_turn_mode(profile) == InworldTurnMode.SINGLE_PASS.value
        if single_pass_enabled:
            checks["knowledge_single_pass_provider_live"] = False
        configured_stt = resolve_inworld_stt_model(model=agent, profile=profile)
        configured_language = inworld_stt_wire_language(model=agent, profile=profile)
        try:
            probe_options: dict[str, Any] = {
                "model_id": profile.llm_model,
                "voice_id": agent.voice_id.removeprefix("inworld:"),
                "stt_model_id": configured_stt,
                "stt_language": configured_language,
            }
            if single_pass_enabled:
                # Preserve the deployed control probe byte-for-byte while making
                # the canary prove the distinct manual, tool-free response path.
                probe_options["single_pass"] = True
            await inworld.realtime_readiness_probe(
                **probe_options,
            )
        except InworldError as exc:
            blockers.append(f"Inworld native Realtime validation failed: {exc}")
        else:
            checks["realtime_provider_live"] = True
            checks["tts_provider_live"] = True
            checks["llm_provider_live"] = True
            if single_pass_enabled:
                checks["knowledge_single_pass_provider_live"] = True
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
        "knowledge_turn_mode": InworldTurnMode.TOOL_LOOP.value,
        "stt_language": "auto",
        "stt_model": "auto",
        "tts_delivery_mode": "balanced",
        "diagnostic_recording_mode": DIAGNOSTIC_RECORDING_OFF,
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
                if key
                not in {
                    "voice_runtime",
                    "knowledge_turn_mode",
                    "stt_model",
                    "tts_delivery_mode",
                    "diagnostic_recording_mode",
                }
            }
        )
        runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
        voice_runtime = str(runtime_config.get("voice_runtime") or "pipeline")
        values["voice_runtime"] = (
            voice_runtime if voice_runtime in {"pipeline", "inworld_realtime"} else "pipeline"
        )
        values["knowledge_turn_mode"] = _knowledge_turn_mode(profile)
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
        values["diagnostic_recording_mode"] = _diagnostic_recording_mode(profile)
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
    agent, profile = await _lock_runtime_mutation(
        db,
        current_user.tenant_id,
        agent_id,
        create_profile=True,
    )
    assert profile is not None
    payload = data.model_dump(
        exclude={
            "voice_runtime",
            "knowledge_turn_mode",
            "stt_model",
            "tts_delivery_mode",
            "diagnostic_recording_mode",
        }
    )
    for key, value in payload.items():
        setattr(profile, key, value)
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    if "voice_runtime" in data.model_fields_set:
        runtime_config = {
            **runtime_config,
            "voice_runtime": data.voice_runtime,
        }
    runtime_config = {
        **runtime_config,
        "inworld_single_pass": data.knowledge_turn_mode == InworldTurnMode.SINGLE_PASS.value,
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
    if "diagnostic_recording_mode" in data.model_fields_set:
        runtime_config = {
            **runtime_config,
            "diagnostic_recording_mode": data.diagnostic_recording_mode,
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
            "diagnostic_recording_mode": _diagnostic_recording_mode(profile),
            "diagnostic_recording_opted_in": (
                _diagnostic_recording_mode(profile) != DIAGNOSTIC_RECORDING_OFF
            ),
            **_knowledge_turn_audit_details(profile),
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
    probe_profile_fingerprint = _activation_configuration_fingerprint(
        agent,
        profile,
        dependency_fingerprints={},
    )
    probe_fingerprint = await _activation_snapshot_fingerprint(db, agent, profile)
    await db.commit()
    blockers, checks = await live_runtime_readiness(
        db,
        agent,
        profile,
        persist_twilio_verification=False,
    )
    await db.commit()
    # Runtime admission and credential mutation share the global lock order:
    # provider boundary -> agent runtime -> credential rows.
    await lock_provider_runtime_boundaries(
        db,
        current_user.tenant_id,
        _runtime_dependency_providers(profile),
    )
    agent, profile = await _lock_runtime_mutation(
        db,
        current_user.tenant_id,
        agent_id,
        create_profile=True,
    )
    assert profile is not None
    if (
        _activation_configuration_fingerprint(
            agent,
            profile,
            dependency_fingerprints={},
        )
        != probe_profile_fingerprint
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Runtime configuration changed during readiness verification. "
                "Run the readiness test again."
            ),
        )
    current_fingerprint = await _activation_snapshot_fingerprint(
        db,
        agent,
        profile,
        lock_credential=True,
    )
    if current_fingerprint != probe_fingerprint:
        raise HTTPException(
            status_code=409,
            detail=(
                "Runtime configuration changed during readiness verification. "
                "Run the readiness test again."
            ),
        )
    if profile.telephony_provider == "twilio" and checks.get("twilio_route_live"):
        credential = await load_workspace_twilio_route_credential(
            db,
            agent.tenant_id,
            for_update=True,
        )
        if credential is None:
            raise HTTPException(status_code=409, detail="Twilio workspace credential changed")
        mark_twilio_route_verified(
            profile,
            credential,
            expected_voice_url=_twilio_inbound_voice_url(),
        )
    tested_at = datetime.now(UTC)
    profile.last_tested_at = tested_at
    if blockers:
        # A readiness test is observational for a route that is already live.
        # Transient provider/API failures must be visible, but an ordinary test
        # must never silently remove an active inbound route. Activation still
        # fails closed, and administrators can deactivate explicitly.
        if profile.enabled:
            profile.status = "active"
        else:
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
        details={
            "ready": not blockers,
            "checks": checks,
            "diagnostic_recording_mode": _diagnostic_recording_mode(profile),
            **_knowledge_turn_audit_details(profile),
        },
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
    probe_profile_fingerprint = _activation_configuration_fingerprint(
        agent,
        profile,
        dependency_fingerprints={},
    )
    probe_fingerprint = await _activation_snapshot_fingerprint(db, agent, profile)
    # Release ordinary ORM read state before bounded provider I/O. The final
    # transition reacquires a shared mutation lock and rejects any change made
    # while the probe was in flight.
    await db.commit()
    blockers, _checks = await live_runtime_readiness(
        db,
        agent,
        profile,
        persist_twilio_verification=False,
    )
    await db.commit()
    await lock_provider_runtime_boundaries(
        db,
        current_user.tenant_id,
        _runtime_dependency_providers(profile),
    )
    agent, profile = await _lock_runtime_mutation(
        db,
        current_user.tenant_id,
        agent_id,
        create_profile=True,
    )
    assert profile is not None
    if (
        _activation_configuration_fingerprint(
            agent,
            profile,
            dependency_fingerprints={},
        )
        != probe_profile_fingerprint
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Runtime configuration changed during readiness verification. "
                "Review the latest configuration and activate again."
            ),
        )
    current_fingerprint = await _activation_snapshot_fingerprint(
        db,
        agent,
        profile,
        lock_credential=True,
    )
    if current_fingerprint != probe_fingerprint:
        raise HTTPException(
            status_code=409,
            detail=(
                "Runtime configuration changed during readiness verification. "
                "Review the latest configuration and activate again."
            ),
        )
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
            details={
                "blockers": blockers,
                "diagnostic_recording_mode": _diagnostic_recording_mode(profile),
                **_knowledge_turn_audit_details(profile),
            },
        )
        # FastAPI's session dependency rolls back on HTTPException. Commit the
        # fail-closed state before returning 409 so a formerly active route
        # cannot remain enabled after a failed re-activation check.
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail={"message": "Runtime is not ready", "blockers": blockers},
        )
    if profile.telephony_provider == "twilio":
        credential = await load_workspace_twilio_route_credential(
            db,
            agent.tenant_id,
            for_update=True,
        )
        if credential is None:
            raise HTTPException(status_code=409, detail="Twilio workspace credential changed")
        mark_twilio_route_verified(
            profile,
            credential,
            expected_voice_url=_twilio_inbound_voice_url(),
        )
    await _lock_number_routes(db, agent, profile)
    final_blockers, _final_checks = await runtime_readiness(db, agent, profile)
    if final_blockers:
        profile.enabled = False
        profile.status = "blocked"
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail={"message": "Runtime is not ready", "blockers": final_blockers},
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
        details={
            "telephony_provider": profile.telephony_provider,
            "diagnostic_recording_mode": _diagnostic_recording_mode(profile),
            **_knowledge_turn_audit_details(profile),
        },
    )
    return _response(agent, profile, blockers)


@router.post("/agents/{agent_id}/deactivate", response_model=RuntimeProfileResponse)
async def deactivate_runtime_profile(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    agent, profile = await _lock_runtime_mutation(
        db,
        current_user.tenant_id,
        agent_id,
        create_profile=True,
    )
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
        details={
            "diagnostic_recording_mode": _diagnostic_recording_mode(profile),
            **_knowledge_turn_audit_details(profile),
        },
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
    await lock_provider_runtime_boundaries(db, current_user.tenant_id, provider)
    if provider == _SMALLEST_CLEANUP_PROVIDER:
        await lock_provider_cleanup_boundary(
            db,
            current_user.tenant_id,
            _SMALLEST_CLEANUP_PROVIDER,
        )
    existing = await get_provider_credential(db, current_user.tenant_id, provider)
    if (
        provider == _SMALLEST_CLEANUP_PROVIDER
        and existing is not None
        and existing.is_active
        and await _has_pending_smallest_cleanup(db, current_user.tenant_id)
    ):
        raise _smallest_cleanup_credential_conflict()
    invalidated_agent_ids = await invalidate_active_runtimes_for_credential(
        db,
        current_user.tenant_id,
        provider,
    )
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
        details={
            "provider": provider,
            "reverification_required_agent_ids": invalidated_agent_ids,
        },
    )
    return await _workspace_credential_status(db, current_user.tenant_id, provider)


@router.put("/credentials/twilio/account", response_model=WorkspaceCredentialStatus)
async def save_twilio_credential(
    data: TwilioCredentialRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    await lock_provider_runtime_boundaries(db, current_user.tenant_id, "twilio")
    invalidated_agent_ids = await invalidate_active_runtimes_for_credential(
        db,
        current_user.tenant_id,
        "twilio",
    )
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
        details={
            "provider": "twilio",
            "reverification_required_agent_ids": invalidated_agent_ids,
        },
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
    await lock_provider_runtime_boundaries(db, current_user.tenant_id, provider)
    if provider == _SMALLEST_CLEANUP_PROVIDER:
        await lock_provider_cleanup_boundary(
            db,
            current_user.tenant_id,
            _SMALLEST_CLEANUP_PROVIDER,
        )
        if await _has_pending_smallest_cleanup(db, current_user.tenant_id):
            raise _smallest_cleanup_credential_conflict()
    credential = await get_provider_credential(db, current_user.tenant_id, provider)
    if credential:
        credential_id = str(credential.id)
        invalidated_agent_ids = await invalidate_active_runtimes_for_credential(
            db,
            current_user.tenant_id,
            provider,
        )
        credential = await get_provider_credential(
            db,
            current_user.tenant_id,
            provider,
            for_update=True,
        )
        if credential is None:
            raise HTTPException(status_code=409, detail="Provider credential changed")
        await db.delete(credential)
        await db.flush()
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="provider_credential.deleted",
            resource_type="provider_credential",
            resource_id=credential_id,
            details={
                "provider": provider,
                "reverification_required_agent_ids": invalidated_agent_ids,
            },
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
    await lock_provider_runtime_boundaries(db, current_user.tenant_id, "livekit_sip")
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
    invalidated_agent_ids = await invalidate_active_runtimes_for_credential(
        db,
        current_user.tenant_id,
        "livekit_sip",
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
        details={
            "provider": "livekit_sip",
            "reverification_required_agent_ids": invalidated_agent_ids,
        },
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
    await lock_provider_runtime_boundaries(db, current_user.tenant_id, "livekit_sip")
    credential = await get_provider_credential(db, current_user.tenant_id, "livekit_sip")
    if credential:
        credential_id = str(credential.id)
        invalidated_agent_ids = await invalidate_active_runtimes_for_credential(
            db,
            current_user.tenant_id,
            "livekit_sip",
        )
        credential = await get_provider_credential(
            db,
            current_user.tenant_id,
            "livekit_sip",
            for_update=True,
        )
        if credential is None:
            raise HTTPException(status_code=409, detail="LiveKit SIP credential changed")
        await db.delete(credential)
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="provider_credential.deleted",
            resource_type="provider_credential",
            resource_id=credential_id,
            details={
                "provider": "livekit_sip",
                "reverification_required_agent_ids": invalidated_agent_ids,
            },
        )
    return SipCredentialStatus(configured=False)
