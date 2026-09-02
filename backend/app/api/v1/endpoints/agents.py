"""Agent builder endpoints - CRUD for AI voice agents."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Annotated
from uuid import UUID, uuid4, uuid5

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.livekit_runtime.browser_session import (
    BROWSER_TOKEN_TTL_SECONDS,
    LiveKitBrowserSessionError,
    LiveKitBrowserSessionProvider,
    delete_browser_room,
)
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    AgentRuntimeProfile,
    KnowledgeBase,
    KnowledgeSource,
)
from app.models.call import Call
from app.models.campaign import Campaign, CampaignContactAttempt
from app.models.provider_credential import ProviderCredential
from app.models.voice import VoiceClone
from app.providers.elevenlabs import (
    ELEVENLABS_MODEL,
    ElevenLabsClient,
    ElevenLabsError,
    get_elevenlabs_client,
)
from app.providers.inworld import (
    INWORLD_TTS_MODEL,
    InworldClient,
    InworldError,
)
from app.providers.sarvam import (
    SarvamAIClient,
    SarvamAIError,
    get_sarvam_client,
    sarvam_voice_catalog,
)
from app.providers.smallest import (
    SmallestAIClient,
    SmallestAIError,
    get_smallest_client,
    resolve_active_knowledge_base_id,
)
from app.schemas.agent import (
    AgentAIDraftRequest,
    AgentAIDraftResponse,
    AgentCreate,
    AgentProviderCatalog,
    AgentResponse,
    AgentUpdate,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    LiveKitSessionRequest,
    LiveKitSessionResponse,
    ProviderCredentialStatus,
    SarvamCredentialRequest,
    SmallestProviderResolution,
    SmallestSessionRequest,
    SmallestSessionResponse,
    VoiceCloneResponse,
    VoicePreviewRequest,
    validate_language_configuration,
)
from app.services.agent_ai_wizard import (
    AgentAIWizardError,
    KnowledgeBaseSummary,
    generate_agent_ai_draft,
)
from app.services.agent_catalog import (
    AGENT_TEMPLATES,
    LanguageCompatibilityStatus,
    VoiceModelPool,
    language_catalog,
    language_code,
    normalize_voices,
    voice_language_compatibility,
)
from app.services.agent_catalog_cache import (
    PUBLIC_CATALOG_CACHE_KEY,
    public_agent_catalog_cache,
)
from app.services.audit import record_audit_event
from app.services.call_metadata import agent_configuration_snapshot
from app.services.integration_security import (
    IntegrationConfigUnavailableError,
    decrypt_integration_config,
    encrypt_integration_config,
)
from app.services.provider_credentials import ProviderCredentialError, load_provider_config
from app.services.rate_limit import enforce_rate_limit
from app.services.runtime_capacity import RuntimeCapacityError, enforce_runtime_capacity
from app.services.usage_ledger import lock_agent_runtime_limits
from app.telephony.livekit_provider import LiveKitSIPError, LiveKitSIPProvider

LIVEKIT_BROWSER_IDEMPOTENCY_NAMESPACE = UUID("6a46e850-91a5-4ca6-acf4-e51b3a504069")


def _livekit_browser_request_fingerprint(
    *,
    tenant_id: UUID,
    agent_id: UUID,
    user_id: UUID,
    idempotency_key: str,
    variables: dict[str, str | int | float | bool],
) -> str:
    """Bind an idempotency key to one typed request without storing the raw key."""
    payload = {
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "user_id": str(user_id),
        "idempotency_key": idempotency_key,
        "variables": variables,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    key_material = settings.integration_encryption_key.strip() or settings.secret_key.strip()
    derived_key = hmac.new(
        key_material.encode(),
        b"vav-livekit-browser-idempotency-v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(derived_key, canonical, hashlib.sha256).hexdigest()


router = APIRouter(prefix="/agents", tags=["Agents"])

SMALLEST_SYNC_FIELDS = {
    "system_prompt",
    "greeting_message",
    "model_name",
    "voice_id",
    "language",
    "supported_languages",
    "language_switching_enabled",
    "language_switching_mode",
    "speech_rate",
    "timezone",
    "max_call_duration_seconds",
}

VOICE_PREFLIGHT_FIELDS = {
    "voice_provider",
    "voice_id",
    "language",
    "supported_languages",
}

VOICE_CONFIGURATION_FIELDS = VOICE_PREFLIGHT_FIELDS | {
    "language_switching_enabled",
    "language_switching_mode",
}

PROVIDER_FIELD_CAPABILITIES = {
    "is_active": {
        "status": "local_only",
        "reason": "Activation in this app gates new calls but does not archive the provider agent.",
    },
    "name": {
        "status": "create_only",
        "provider_field": "name",
        "reason": "Smallest agent metadata is set during initial provisioning.",
    },
    "description": {
        "status": "create_only",
        "provider_field": "description",
        "reason": "Smallest agent metadata is set during initial provisioning.",
    },
    "system_prompt": {
        "status": "synced",
        "provider_field": "singlePromptConfig.prompt",
    },
    "greeting_message": {"status": "synced", "provider_field": "firstMessage"},
    "model_name": {"status": "synced", "provider_field": "slmModel"},
    "model_provider": {
        "status": "local_only",
        "reason": "This deployment currently supports only the Smallest Electron runtime.",
    },
    "voice_id": {"status": "synced", "provider_field": "synthesizer.voiceConfig.voiceId"},
    "voice_provider": {
        "status": "local_only",
        "reason": "This deployment currently supports only Smallest speech synthesis.",
    },
    "language": {"status": "synced", "provider_field": "language.default"},
    "supported_languages": {"status": "synced", "provider_field": "language.supported"},
    "language_switching_enabled": {
        "status": "synced",
        "provider_field": "language.switching.isEnabled",
    },
    "language_switching_mode": {
        "status": "synced",
        "provider_field": "language.switching.isEnabled",
    },
    "speech_rate": {"status": "synced", "provider_field": "synthesizer.speed"},
    "timezone": {
        "status": "synced",
        "provider_field": "timezone",
        "reason": (
            "The draft API accepts the IANA identifier, but the provider read API returns only "
            "an offset object, so identity is verified by write acknowledgement rather than "
            "strict round trip."
        ),
    },
    "max_call_duration_seconds": {
        "status": "synced",
        "provider_field": "sessionTimeoutConfig.timeoutTimeInSecs",
    },
    "temperature": {
        "status": "local_only",
        "reason": "The current Smallest Electron draft contract has no temperature field.",
    },
    "max_tokens": {
        "status": "local_only",
        "reason": "The current Smallest draft contract has no output-token limit field.",
    },
    "fallback_message": {
        "status": "local_only",
        "reason": "Fallback behavior requires a provider workflow/tool runtime.",
    },
    "transfer_number": {
        "status": "local_only",
        "reason": "Transfers require a configured Smallest transfer tool.",
    },
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
    {
        "completed",
        "failed",
        "busy",
        "no_answer",
        "canceled",
        "cancelled",
        "terminal_unknown",
    }
)
TERMINAL_CAMPAIGN_ATTEMPT_STATES = frozenset({"completed", "failed", "rejected", "cancelled"})
MAX_VOICE_CLONE_BYTES = 5 * 1024 * 1024
VOICE_CLONE_MEDIA_TYPES = {
    "audio/mpeg": {".mp3"},
    "audio/mpeg-3": {".mp3"},
    "audio/wav": {".wav"},
    "audio/wave": {".wav"},
    "audio/x-wav": {".wav"},
    "audio/webm": {".webm"},
    "video/webm": {".webm"},
    "audio/mp4": {".mp4"},
    "video/mp4": {".mp4"},
}
VOICE_CLONE_MODELS = {"lightning-v3.1", "lightning-v3.1-pro"}


@dataclass(frozen=True)
class VoiceResolution:
    requested_voice_id: str
    resolved_voice_id: str
    synthesizer_model: str
    source: str


async def _public_voice_catalog(client) -> list[dict]:
    """Load a normalized public catalog with a short transient-failure fallback."""
    try:
        provider_voices = await client.list_voices()
    except SmallestAIError as exc:
        provider_auth_failure = exc.upstream_status_code in {401, 403}
        transient = exc.status_code == 429 or exc.status_code >= 500
        if client.is_configured and transient and not provider_auth_failure:
            cached = public_agent_catalog_cache.get(PUBLIC_CATALOG_CACHE_KEY)
            if cached is not None:
                return cached
        raise

    normalized = normalize_voices(provider_voices, [])
    if public_agent_catalog_cache.remember(PUBLIC_CATALOG_CACHE_KEY, normalized):
        return normalized
    cached = public_agent_catalog_cache.get(PUBLIC_CATALOG_CACHE_KEY)
    return cached if cached is not None else normalized


def _owned_clone_provider_shape(clone: VoiceClone) -> dict:
    tags = [tag for tag in (clone.gender, "private") if tag]
    return {
        "voiceId": clone.provider_voice_id,
        "displayName": clone.display_name,
        "language": clone.language,
        "accent": clone.accent,
        "tags": tags,
        "status": clone.status,
        "modelIds": list(clone.model_ids),
    }


async def _tenant_voice_catalog(
    client,
    db: AsyncSession,
    tenant_id: UUID,
) -> list[dict]:
    public_voices = await _public_voice_catalog(client)
    result = await db.execute(
        select(VoiceClone).where(
            VoiceClone.tenant_id == tenant_id,
            VoiceClone.provider_voice_id.is_not(None),
            VoiceClone.status == "completed",
        )
    )
    owned_clones = [_owned_clone_provider_shape(clone) for clone in result.scalars().all()]
    normalized_clones = normalize_voices([], owned_clones)
    return sorted(
        [*normalized_clones, *public_voices],
        key=lambda voice: (voice.get("source") != "cloned", str(voice.get("name") or "")),
    )


def _valid_voice_sample_signature(content: bytes, suffix: str) -> bool:
    if suffix == ".wav":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"
    if suffix == ".mp3":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    if suffix == ".webm":
        return content.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".mp4":
        return len(content) >= 12 and content[4:8] == b"ftyp"
    return False


async def _tenant_voice_clone(
    db: AsyncSession,
    tenant_id: UUID,
    clone_id: UUID,
    *,
    for_update: bool = False,
) -> VoiceClone:
    query = select(VoiceClone).where(
        VoiceClone.id == clone_id,
        VoiceClone.tenant_id == tenant_id,
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    clone = await db.scalar(query)
    if clone is None:
        raise HTTPException(status_code=404, detail="Custom voice not found")
    return clone


def _clone_operation_tag(clone_id: UUID) -> str:
    return f"vav-clone-{clone_id}"


def _remote_clone_tags(remote: dict) -> set[str]:
    tags = remote.get("tags")
    if isinstance(tags, dict):
        values = [*tags.keys(), *tags.values()]
    elif isinstance(tags, list):
        values = tags
    elif isinstance(tags, str):
        values = tags.split(",")
    else:
        values = []
    return {str(value).strip() for value in values if str(value).strip()}


def _apply_remote_clone(clone: VoiceClone, remote: dict) -> None:
    voice_id = remote.get("voiceId") or remote.get("id") or remote.get("_id")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise HTTPException(status_code=502, detail="Smallest.ai returned a clone without an ID")
    model_ids = remote.get("modelIds") or remote.get("model_ids") or []
    if isinstance(model_ids, str):
        model_ids = [model_ids]
    clone.provider_voice_id = voice_id.strip()
    clone.model_ids = [str(value).strip() for value in model_ids if str(value).strip()] or [
        clone.model
    ]
    remote_status = str(remote.get("status") or "completed").strip().lower()
    clone.status = (
        remote_status if remote_status in {"pending", "processing", "completed"} else "processing"
    )
    clone.last_error = None
    clone.last_synced_at = datetime.now(UTC)


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


def _provider_config_mismatch_pending(agent: Agent) -> bool:
    """Return whether Sync must verify a provider-side correction without publishing."""
    return (
        agent.sync_status == "error"
        and _publish_operation(agent).get("phase") == "provider_config_mismatch"
    )


def _provider_config_recovery_is_current(agent: Agent, operation_id: str) -> bool:
    return (
        _provider_config_mismatch_pending(agent)
        and _publish_operation(agent).get("id") == operation_id
    )


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


def _provider_publish_label(label: str, operation_id: str) -> str:
    """Build a provider-safe, bounded label that still identifies this operation."""
    punctuation = {" ", ".", ",", "-", "(", ")"}
    normalized = "".join(
        character
        if (character.isascii() and character.isalnum()) or character in punctuation
        else "-"
        for character in label
    )
    normalized = " ".join(normalized.split()).strip(" .,-()") or "VAV"
    suffix = f"-{operation_id[:8]}"
    base = normalized[: 40 - len(suffix)].rstrip(" .,-()") or "VAV"
    return f"{base}{suffix}"


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


def _publish_kwargs(agent: Agent, voice: VoiceResolution) -> dict:
    return {
        "global_prompt": agent.system_prompt,
        "first_message": agent.greeting_message,
        "slm_model": agent.model_name,
        "language": agent.language,
        "supported_languages": list(agent.supported_languages),
        "language_switching_enabled": agent.language_switching_enabled,
        "language_switching_mode": agent.language_switching_mode,
        "timezone": agent.timezone,
        "voice_id": voice.resolved_voice_id,
        "speech_rate": agent.speech_rate,
        "synthesizer_model": voice.synthesizer_model,
        "max_call_duration_seconds": agent.max_call_duration_seconds,
    }


def _apply_knowledge_binding_delta(
    snapshot: dict,
    *,
    desired_knowledge_base_id: str | None,
    existing_knowledge_base_id: str | None,
) -> str:
    """Write the KB field only when the provider binding actually changes."""
    if desired_knowledge_base_id == existing_knowledge_base_id:
        return "unchanged"
    snapshot["global_knowledge_base_id"] = desired_knowledge_base_id
    return "set" if desired_knowledge_base_id is not None else "clear"


async def _approved_bound_provider_knowledge_base_id(
    db: AsyncSession,
    *,
    agent_id: UUID,
    tenant_id: UUID,
) -> str | None:
    """Resolve one approved, provisioned KB bound to an agent."""
    knowledge_binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.agent_id == agent_id,
            AgentKnowledgeBinding.tenant_id == tenant_id,
        )
    )
    if not knowledge_binding:
        return None
    bound_knowledge = await db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_binding.knowledge_base_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.approval_status == "approved",
        )
    )
    if not bound_knowledge or not bound_knowledge.provider_knowledge_base_id:
        raise HTTPException(
            status_code=409,
            detail="Bound knowledge must be approved and provisioned before publishing",
        )
    if bound_knowledge.sync_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=(
                "Bound knowledge is not retrieval-ready. Repair or finish indexing every "
                "source before publishing."
            ),
        )
    return bound_knowledge.provider_knowledge_base_id


def _voice_configuration_snapshot(agent: Agent) -> tuple:
    return (
        agent.voice_id,
        agent.language,
        tuple(agent.supported_languages),
        agent.language_switching_enabled,
        agent.language_switching_mode,
        agent.speech_rate,
        agent.max_call_duration_seconds,
    )


def _comparable_agent_field_value(field: str, value):
    """Return an immutable representation for an authoritative update diff."""
    if field == "supported_languages":
        return tuple(value)
    return value


def _effective_agent_changes(agent: Agent, changes: dict) -> dict:
    """Drop normalized PATCH values that already match the locked database row."""
    return {
        field: value
        for field, value in changes.items()
        if _comparable_agent_field_value(field, getattr(agent, field))
        != _comparable_agent_field_value(field, value)
    }


def _agent_fields_snapshot(agent: Agent, fields: set[str]) -> dict:
    return {field: _comparable_agent_field_value(field, getattr(agent, field)) for field in fields}


def _stored_voice_resolution(agent: Agent) -> VoiceResolution | None:
    config = _provider_config(agent)
    resolved_voice_id = config.get("resolved_voice_id")
    model = config.get("voice_model")
    if not isinstance(resolved_voice_id, str) or not resolved_voice_id:
        return None
    if not isinstance(model, str) or not model:
        return None
    return VoiceResolution(
        requested_voice_id=agent.voice_id,
        resolved_voice_id=resolved_voice_id,
        synthesizer_model=model,
        source=str(config.get("voice_resolution_source") or "stored"),
    )


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


async def _require_agent_idle_for_provider_removal(
    db: AsyncSession,
    *,
    agent: Agent,
    tenant_id: UUID,
    action: str,
) -> None:
    """Block destructive provider removal while live work still references the agent."""
    nonterminal_call_id = await db.scalar(
        select(Call.id)
        .where(
            Call.tenant_id == tenant_id,
            Call.agent_id == agent.id,
            Call.status.notin_(TERMINAL_CALL_STATUSES),
        )
        .limit(1)
    )
    if nonterminal_call_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Agent cannot be {action} while a call is still nonterminal",
        )
    nonterminal_attempt_id = await db.scalar(
        select(CampaignContactAttempt.id)
        .join(Campaign, Campaign.id == CampaignContactAttempt.campaign_id)
        .outerjoin(Call, Call.id == CampaignContactAttempt.call_id)
        .where(
            Campaign.tenant_id == tenant_id,
            CampaignContactAttempt.tenant_id == tenant_id,
            CampaignContactAttempt.state.notin_(TERMINAL_CAMPAIGN_ATTEMPT_STATES),
            or_(Campaign.agent_id == agent.id, Call.agent_id == agent.id),
        )
        .limit(1)
    )
    if nonterminal_attempt_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Agent cannot be {action} while a campaign attempt is unresolved",
        )


async def _tenant_sarvam_credential(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    for_update: bool = False,
) -> ProviderCredential | None:
    query = select(ProviderCredential).where(
        ProviderCredential.tenant_id == tenant_id,
        ProviderCredential.provider == "sarvam",
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def _tenant_smallest_client(
    db: AsyncSession,
    tenant_id: UUID,
) -> tuple[SmallestAIClient, str, datetime | None]:
    credential = await db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.tenant_id == tenant_id,
            ProviderCredential.provider == "smallest",
            ProviderCredential.is_active.is_(True),
        )
    )
    if credential is not None:
        try:
            config = decrypt_integration_config(credential.encrypted_config)
        except IntegrationConfigUnavailableError as exc:
            raise HTTPException(
                status_code=500,
                detail="Smallest.ai credential is unavailable; ask an administrator to rotate it",
            ) from exc
        api_key = config.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise HTTPException(status_code=500, detail="Smallest.ai credential is invalid")
        return SmallestAIClient(api_key=api_key), "workspace", credential.updated_at
    client = get_smallest_client()
    configured = bool(getattr(client, "is_configured", True))
    return client, "platform" if configured else "none", None


async def _tenant_sarvam_client(
    db: AsyncSession,
    tenant_id: UUID,
) -> tuple[SarvamAIClient, str, datetime | None]:
    credential = await _tenant_sarvam_credential(db, tenant_id)
    if credential is not None and credential.is_active:
        try:
            config = decrypt_integration_config(credential.encrypted_config)
        except IntegrationConfigUnavailableError as exc:
            raise HTTPException(
                status_code=500,
                detail="Sarvam credential is unavailable; ask an administrator to rotate it",
            ) from exc
        api_key = config.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise HTTPException(status_code=500, detail="Sarvam credential is invalid")
        return SarvamAIClient(api_key=api_key), "workspace", credential.updated_at
    client = get_sarvam_client()
    return client, "platform" if client.is_configured else "none", None


async def _tenant_elevenlabs_client(
    db: AsyncSession,
    tenant_id: UUID,
) -> tuple[ElevenLabsClient, str, datetime | None]:
    credential = await db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.tenant_id == tenant_id,
            ProviderCredential.provider == "elevenlabs",
            ProviderCredential.is_active.is_(True),
        )
    )
    if credential is not None:
        try:
            config = decrypt_integration_config(credential.encrypted_config)
        except IntegrationConfigUnavailableError as exc:
            raise HTTPException(
                status_code=500,
                detail="ElevenLabs credential is unavailable; ask an administrator to rotate it",
            ) from exc
        api_key = config.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise HTTPException(status_code=500, detail="ElevenLabs credential is invalid")
        return ElevenLabsClient(api_key=api_key), "workspace", credential.updated_at
    client = get_elevenlabs_client()
    return client, "platform" if client.is_configured else "none", None


async def _tenant_inworld_client(
    db: AsyncSession,
    tenant_id: UUID,
) -> tuple[InworldClient, str, datetime | None]:
    credential = await db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.tenant_id == tenant_id,
            ProviderCredential.provider == "inworld",
            ProviderCredential.is_active.is_(True),
        )
    )
    if credential is not None:
        try:
            config = decrypt_integration_config(credential.encrypted_config)
        except IntegrationConfigUnavailableError as exc:
            raise HTTPException(
                status_code=500,
                detail="Inworld credential is unavailable; ask an administrator to rotate it",
            ) from exc
        api_key = config.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise HTTPException(status_code=500, detail="Inworld credential is invalid")
        return InworldClient(api_key=api_key), "workspace", credential.updated_at
    client = InworldClient()
    return client, "platform" if client.is_configured else "none", None


async def _livekit_browser_runtime(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    for_update: bool = False,
) -> tuple[Agent, AgentRuntimeProfile]:
    """Load the exact active Inworld runtime and its governed searchable KB."""
    agent_query = (
        select(Agent)
        .where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    profile_query = (
        select(AgentRuntimeProfile)
        .where(
            AgentRuntimeProfile.agent_id == agent_id,
            AgentRuntimeProfile.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        agent_query = agent_query.with_for_update()
        profile_query = profile_query.with_for_update()
    agent = await db.scalar(agent_query)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    profile = await db.scalar(profile_query)
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="Agent is inactive")
    if agent.voice_provider != "inworld" or not agent.voice_id.startswith("inworld:"):
        raise HTTPException(
            status_code=409,
            detail="LiveKit browser testing currently requires an Inworld voice agent",
        )
    if (
        profile is None
        or profile.status == "inactive"
        or profile.telephony_provider != "livekit_sip"
        or profile.primary_speech_provider != "inworld"
        or profile.llm_provider not in {"inworld", "openai"}
    ):
        raise HTTPException(
            status_code=409,
            detail=("Save a compatible LiveKit + Inworld runtime profile before browser testing"),
        )
    if not (
        settings.livekit_url.strip()
        and settings.livekit_api_key.strip()
        and settings.livekit_api_secret.strip()
        and settings.livekit_agent_name.strip()
        and settings.livekit_worker_health_url.strip()
    ):
        raise HTTPException(
            status_code=503,
            detail="LiveKit browser runtime credentials or worker health routing are unavailable",
        )
    inworld, _source, _updated_at = await _tenant_inworld_client(db, tenant_id)
    if not inworld.is_configured:
        raise HTTPException(status_code=409, detail="Add a valid Inworld API key first")
    if profile.llm_provider == "openai":
        try:
            openai_config = await load_provider_config(db, tenant_id, "openai")
        except ProviderCredentialError as exc:
            raise HTTPException(
                status_code=409,
                detail="OpenAI credential is unavailable; ask an administrator to rotate it",
            ) from exc
        openai_key = str(
            ((openai_config or {}).get("api_key") or "")
            if openai_config is not None
            else settings.openai_api_key
        ).strip()
        if not openai_key:
            raise HTTPException(status_code=409, detail="Add a valid OpenAI API key first")

    binding_query = (
        select(AgentKnowledgeBinding)
        .where(
            AgentKnowledgeBinding.agent_id == agent.id,
            AgentKnowledgeBinding.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        binding_query = binding_query.with_for_update()
    binding = await db.scalar(binding_query)
    if binding is None:
        raise HTTPException(
            status_code=409,
            detail="Bind an approved searchable knowledge base before browser testing",
        )
    knowledge_query = (
        select(KnowledgeBase)
        .where(
            KnowledgeBase.id == binding.knowledge_base_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_active.is_(True),
            KnowledgeBase.approval_status == "approved",
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        knowledge_query = knowledge_query.with_for_update()
    knowledge = await db.scalar(knowledge_query)
    if knowledge is None:
        raise HTTPException(
            status_code=409,
            detail="Approve the bound knowledge base before browser testing",
        )
    source_query = (
        select(KnowledgeSource)
        .where(
            KnowledgeSource.knowledge_base_id == knowledge.id,
            KnowledgeSource.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        source_query = source_query.with_for_update()
    sources = (await db.scalars(source_query)).all()
    if not sources or not all(
        source.status in {"processing", "indexed", "local_only"}
        and bool(str(source.content or "").strip())
        for source in sources
    ):
        raise HTTPException(
            status_code=409,
            detail="Repair the bound knowledge base so every source has searchable text",
        )
    return agent, profile


async def _mark_livekit_browser_issuance_failed(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    call_id: UUID,
    ambiguous: bool,
    failure_type: str,
) -> None:
    call = await db.scalar(
        select(Call)
        .where(Call.id == call_id, Call.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if call is None or call.status in TERMINAL_CALL_STATUSES:
        return
    now = datetime.now(UTC)
    call.status = "terminal_unknown" if ambiguous else "failed"
    call.ended_at = now
    call.call_metadata = {
        **(call.call_metadata or {}),
        "lifecycle_error": "livekit_browser_session_issuance_failed",
        "runtime_failure_type": failure_type,
        "operator_review_required": ambiguous,
        "automatic_redial_disabled": True,
    }
    await db.commit()


async def _delete_browser_room_despite_cancellation(*, room_name: str) -> bool:
    cleanup = asyncio.create_task(
        delete_browser_room(
            url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
            room_name=room_name,
        )
    )
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    try:
        return bool(cleanup.result())
    except Exception:
        return False


async def _mark_livekit_browser_issuance_failed_despite_cancellation(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    call_id: UUID,
    ambiguous: bool,
    failure_type: str,
) -> bool:
    """Release a committed reservation even while its request is being cancelled."""
    cleanup = asyncio.create_task(
        _mark_livekit_browser_issuance_failed(
            db,
            tenant_id=tenant_id,
            call_id=call_id,
            ambiguous=ambiguous,
            failure_type=failure_type,
        )
    )
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    try:
        cleanup.result()
    except BaseException:
        return False
    return True


async def _require_tenant_voice(
    client,
    db: AsyncSession,
    tenant_id: UUID,
    voice_id: str,
    selected_languages: list[str],
) -> VoiceResolution:
    """Resolve one public or tenant-owned voice for every selected language.

    A blank local voice is an app-managed platform default. It is resolved to
    an explicit compatible provider voice so clearing a previous custom voice
    cannot leave stale synthesizer configuration in Smallest's merged draft.
    """
    try:
        normalized_voices = await _tenant_voice_catalog(client, db, tenant_id)
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    by_id = {str(voice["id"]): voice for voice in normalized_voices}
    selected_voice = by_id.get(voice_id) if voice_id else None
    if voice_id and selected_voice is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Voice is not available in the shared public catalog. "
                "Private voice clones must belong to this workspace."
            ),
        )

    if not voice_id:
        candidates = [
            voice
            for voice in normalized_voices
            if voice.get("source") == "catalog"
            if voice_language_compatibility(
                normalized_voices,
                str(voice.get("id") or ""),
                selected_languages,
            )[0]
            == LanguageCompatibilityStatus.COMPATIBLE
            and voice.get("synthesizer_model")
        ]
        if not candidates:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No public Smallest.ai voice has verified support for every selected "
                    "language. Choose a compatible voice or split this into language-specific "
                    "agents."
                ),
            )
        selected_voice = min(
            candidates,
            key=lambda voice: (
                str(voice.get("id")) != "nyah",
                str(voice.get("name") or voice.get("id")).casefold(),
                str(voice.get("id")),
            ),
        )

    assert selected_voice is not None
    compatibility, unsupported = voice_language_compatibility(
        normalized_voices,
        str(selected_voice["id"]),
        selected_languages,
    )
    if compatibility == LanguageCompatibilityStatus.UNKNOWN:
        raise HTTPException(
            status_code=422,
            detail=(
                "Smallest.ai does not declare language coverage for the selected voice. "
                "Choose a voice with verified language metadata."
            ),
        )
    if compatibility != LanguageCompatibilityStatus.COMPATIBLE:
        raise HTTPException(
            status_code=422,
            detail=(
                "Selected voice does not support these agent languages: " + ", ".join(unsupported)
            ),
        )
    voice_model = selected_voice.get("synthesizer_model")
    if not isinstance(voice_model, str) or not voice_model:
        raise HTTPException(
            status_code=422,
            detail="Selected voice has no verified Smallest.ai Atoms synthesizer model",
        )
    return VoiceResolution(
        requested_voice_id=voice_id,
        resolved_voice_id=str(selected_voice["id"]),
        synthesizer_model=voice_model,
        source="operator" if voice_id else "platform_default",
    )


async def _require_sarvam_voice(
    voice_id: str,
    selected_languages: list[str],
) -> VoiceResolution:
    voices = sarvam_voice_catalog()
    selected_voice = next((voice for voice in voices if voice["id"] == voice_id), None)
    if selected_voice is None:
        raise HTTPException(
            status_code=422,
            detail="Voice is not available in the Sarvam Bulbul v3 catalog",
        )
    compatibility, unsupported = voice_language_compatibility(
        voices,
        voice_id,
        selected_languages,
    )
    if compatibility != LanguageCompatibilityStatus.COMPATIBLE:
        detail = "Selected Sarvam voice does not support every agent language"
        if unsupported:
            detail += ": " + ", ".join(unsupported)
        raise HTTPException(status_code=422, detail=detail)
    return VoiceResolution(
        requested_voice_id=voice_id,
        resolved_voice_id=voice_id,
        synthesizer_model="bulbul:v3",
        source="operator",
    )


async def _require_elevenlabs_voice(
    client: ElevenLabsClient,
    voice_id: str,
    selected_languages: list[str],
) -> VoiceResolution:
    try:
        voices = await client.list_voices()
    except ElevenLabsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    selected_voice = next((voice for voice in voices if voice["id"] == voice_id), None)
    if selected_voice is None:
        raise HTTPException(
            status_code=422,
            detail="Voice is not available to this ElevenLabs workspace",
        )
    compatibility, unsupported = voice_language_compatibility(
        voices,
        voice_id,
        selected_languages,
    )
    if compatibility != LanguageCompatibilityStatus.COMPATIBLE:
        detail = "Selected ElevenLabs voice does not support every agent language"
        if unsupported:
            detail += ": " + ", ".join(unsupported)
        raise HTTPException(status_code=422, detail=detail)
    return VoiceResolution(
        requested_voice_id=voice_id,
        resolved_voice_id=voice_id,
        synthesizer_model=ELEVENLABS_MODEL,
        source="operator",
    )


async def _require_inworld_voice(
    client: InworldClient,
    voice_id: str,
    selected_languages: list[str],
) -> VoiceResolution:
    try:
        voices = await client.list_voices()
    except InworldError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    selected_voice = next((voice for voice in voices if voice["id"] == voice_id), None)
    if selected_voice is None:
        raise HTTPException(
            status_code=422, detail="Voice is not available to this Inworld workspace"
        )
    compatibility, unsupported = voice_language_compatibility(voices, voice_id, selected_languages)
    if compatibility != LanguageCompatibilityStatus.COMPATIBLE:
        detail = "Selected Inworld voice does not support every agent language"
        if unsupported:
            detail += ": " + ", ".join(unsupported)
        raise HTTPException(status_code=422, detail=detail)
    return VoiceResolution(
        requested_voice_id=voice_id,
        resolved_voice_id=voice_id,
        synthesizer_model=INWORLD_TTS_MODEL,
        source="operator",
    )


async def _require_provider_voice(
    provider: str,
    db: AsyncSession,
    tenant_id: UUID,
    voice_id: str,
    selected_languages: list[str],
) -> VoiceResolution:
    if provider == "smallest":
        client, _, _ = await _tenant_smallest_client(db, tenant_id)
        return await _require_tenant_voice(
            client,
            db,
            tenant_id,
            voice_id,
            selected_languages,
        )
    if provider == "sarvam":
        return await _require_sarvam_voice(voice_id, selected_languages)
    if provider == "elevenlabs":
        client, _, _ = await _tenant_elevenlabs_client(db, tenant_id)
        return await _require_elevenlabs_voice(client, voice_id, selected_languages)
    if provider == "inworld":
        client, _, _ = await _tenant_inworld_client(db, tenant_id)
        return await _require_inworld_voice(client, voice_id, selected_languages)
    raise HTTPException(status_code=422, detail="Unsupported voice provider")


_MISSING = object()


def _first_present(mapping: dict, *keys: str):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return _MISSING


def _resolved_system_prompt(provider_agent: dict, resolved: dict):
    """Select the prompt that is authoritative for the provider workflow type."""
    raw_workflow_type = _first_present(provider_agent, "workflowType", "workflow_type")
    if raw_workflow_type is _MISSING:
        raw_workflow_type = _first_present(resolved, "workflowType", "workflow_type")
    workflow_type = (
        raw_workflow_type.strip().lower().replace("-", "_")
        if isinstance(raw_workflow_type, str)
        else None
    )

    prompt = _first_present(resolved, "prompt")
    global_prompt = _first_present(resolved, "globalPrompt")
    nested_prompt = _MISSING
    for configuration in (resolved, provider_agent):
        single_prompt = configuration.get("singlePromptConfig")
        if isinstance(single_prompt, dict) and "prompt" in single_prompt:
            nested_prompt = single_prompt["prompt"]
            break

    if workflow_type == "single_prompt":
        return next(
            (value for value in (prompt, nested_prompt, global_prompt) if value is not _MISSING),
            _MISSING,
        )
    if workflow_type == "workflow_graph":
        return global_prompt

    candidates = [
        value for value in (prompt, nested_prompt, global_prompt) if value is not _MISSING
    ]
    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        return _MISSING
    return candidates[0]


def _provider_configuration_mismatches(
    provider_agent: dict,
    expected: dict,
) -> list[str]:
    """Compare documented resolved fields without storing prompt contents."""
    resolved = provider_agent.get("_resolvedConfig")
    if not isinstance(resolved, dict):
        return ["_resolvedConfig"]

    mismatches: list[str] = []

    def compare(field: str, actual, expected_value) -> None:
        if actual is _MISSING or actual != expected_value:
            mismatches.append(field)

    compare(
        "system_prompt",
        _resolved_system_prompt(provider_agent, resolved),
        expected["global_prompt"],
    )
    compare(
        "greeting_message",
        _first_present(resolved, "firstMessage"),
        expected["first_message"] or "",
    )
    compare(
        "model_name",
        _first_present(resolved, "modelName", "slmModel"),
        expected["slm_model"],
    )
    language = provider_agent.get("language")
    top_language = language if isinstance(language, dict) else {}
    compare(
        "language",
        _first_present(resolved, "defaultLanguage")
        if "defaultLanguage" in resolved
        else _first_present(top_language, "default"),
        expected["language"],
    )
    actual_languages = (
        _first_present(resolved, "supportedLanguages")
        if "supportedLanguages" in resolved
        else _first_present(top_language, "supported")
    )
    if actual_languages is _MISSING or not isinstance(actual_languages, list):
        mismatches.append("supported_languages")
    elif list(dict.fromkeys(actual_languages)) != expected["supported_languages"]:
        mismatches.append("supported_languages")

    switching = _first_present(resolved, "languageSwitching")
    if switching is _MISSING:
        switching = top_language.get("switching", _MISSING)
    actual_switching = (
        switching.get("isEnabled", _MISSING) if isinstance(switching, dict) else _MISSING
    )
    compare(
        "language_switching_enabled",
        actual_switching,
        expected["language_switching_enabled"],
    )

    synthesizer = resolved.get("synthesizer")
    if not isinstance(synthesizer, dict):
        synthesizer = provider_agent.get("synthesizer")
    voice_config = synthesizer.get("voiceConfig") if isinstance(synthesizer, dict) else None
    compare(
        "voice_id",
        voice_config.get("voiceId", _MISSING) if isinstance(voice_config, dict) else _MISSING,
        expected["voice_id"],
    )
    compare(
        "synthesizer_model",
        voice_config.get("model", _MISSING) if isinstance(voice_config, dict) else _MISSING,
        expected["synthesizer_model"],
    )
    actual_speed = synthesizer.get("speed", _MISSING) if isinstance(synthesizer, dict) else _MISSING
    if actual_speed is _MISSING:
        mismatches.append("speech_rate")
    else:
        try:
            if abs(float(actual_speed) - float(expected["speech_rate"])) > 0.001:
                mismatches.append("speech_rate")
        except (TypeError, ValueError):
            mismatches.append("speech_rate")

    session_timeout = resolved.get("sessionTimeoutConfig")
    if not isinstance(session_timeout, dict):
        session_timeout = provider_agent.get("sessionTimeoutConfig")
    compare(
        "max_call_duration_seconds",
        session_timeout.get("timeoutTimeInSecs", _MISSING)
        if isinstance(session_timeout, dict)
        else _MISSING,
        expected["max_call_duration_seconds"],
    )
    if "global_knowledge_base_id" in expected:
        try:
            actual_knowledge_base_id = resolve_active_knowledge_base_id(provider_agent)
        except SmallestAIError:
            mismatches.append("global_knowledge_base_id")
        else:
            compare(
                "global_knowledge_base_id",
                actual_knowledge_base_id,
                expected["global_knowledge_base_id"],
            )
    return mismatches


def _provider_configuration_mismatch_sets(
    published_provider_agent: dict,
    active_provider_agent: dict,
    expected_configuration: dict,
) -> tuple[list[str], list[str], list[str]]:
    published_mismatches = _provider_configuration_mismatches(
        published_provider_agent,
        expected_configuration,
    )
    if published_provider_agent.get("_configSource") != "version":
        published_mismatches.append("configuration_source")
    active_mismatches = _provider_configuration_mismatches(
        active_provider_agent,
        expected_configuration,
    )
    if active_provider_agent.get("_configSource") != "active":
        active_mismatches.append("configuration_source")
    return (
        sorted({*published_mismatches, *active_mismatches}),
        published_mismatches,
        active_mismatches,
    )


def _provider_tool_refs(provider_agent: dict) -> tuple[str, ...] | None:
    """Return published tool references, preserving invalid state as None."""
    resolved = provider_agent.get("_resolvedConfig")
    configurations = [resolved, provider_agent] if isinstance(resolved, dict) else [provider_agent]
    for configuration in configurations:
        if "toolRefs" not in configuration:
            continue
        raw_refs = configuration["toolRefs"]
        if raw_refs is None:
            return ()
        if not isinstance(raw_refs, list) or any(
            not isinstance(ref, str) or not ref.strip() for ref in raw_refs
        ):
            return None
        return tuple(dict.fromkeys(raw_refs))
    return ()


def _created_agent_knowledge_tool_ref_matches(
    original_provider_agent: dict,
    published_provider_agent: dict,
    active_provider_agent: dict,
    *,
    expected_knowledge_base_id: str | None,
) -> bool:
    """Verify the one KB tool created by an initial agent create request."""
    original_refs = _provider_tool_refs(original_provider_agent)
    published_refs = _provider_tool_refs(published_provider_agent)
    active_refs = _provider_tool_refs(active_provider_agent)
    if None in (original_refs, published_refs, active_refs):
        return False
    if original_refs != published_refs or published_refs != active_refs:
        return False
    if expected_knowledge_base_id is None:
        return original_refs == ()
    return len(original_refs) == 1


def _verified_knowledge_tool_ref_delta(
    original_provider_agent: dict,
    published_provider_agent: dict,
    active_provider_agent: dict,
    *,
    expected_knowledge_base_id: str | None,
) -> bool:
    """Accept one provider-created KB tool-ref delta when Smallest omits expansion.

    Smallest's current agent response can expose the live Knowledge Base system tool
    only through _resolvedConfig.toolRefs while omitting both the expanded
    knowledge_base_search tool and the documented top-level KB field. This fallback
    is limited to a publish operation with a known baseline: published and active
    refs must agree, every other field is checked separately, and exactly one ref
    must have been added or removed from the baseline revision.
    """
    original_refs = _provider_tool_refs(original_provider_agent)
    published_refs = _provider_tool_refs(published_provider_agent)
    active_refs = _provider_tool_refs(active_provider_agent)
    if None in (original_refs, published_refs, active_refs):
        return False
    if published_refs != active_refs:
        return False

    original_set = set(original_refs)
    current_set = set(published_refs)
    if expected_knowledge_base_id is None:
        return current_set < original_set and len(original_set - current_set) == 1
    return original_set < current_set and len(current_set - original_set) == 1


async def _mark_publish_failure(
    db: AsyncSession,
    *,
    agent_id: UUID,
    tenant_id: UUID,
    operation_id: str,
    sync_status: str,
    error: SmallestAIError,
    phase: str | None = None,
) -> Agent:
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    operation = _publish_operation(agent)
    if operation.get("id") == operation_id and agent.sync_status == "publishing":
        agent.sync_status = sync_status
        _set_provider_operation(
            agent,
            "publish",
            phase=f"{phase}_failed" if phase else sync_status,
            lease_expires_at=None,
            last_error=str(error),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
    return agent


def _publish_error_detail(phase: str, error: SmallestAIError) -> str:
    action = {
        "webhook_subscriptions": "update webhook subscriptions",
        "branch_lookup": "read the default branch",
        "knowledge_binding_lookup": "read the active knowledge-base binding",
        "draft_update": "update the draft configuration",
        "baseline_revision": "read the current revision",
        "publish_request": "publish the draft",
    }.get(phase, "complete the publish operation")
    return f"Could not {action} on Smallest.ai: {error}"


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
    provision_operation = _provision_operation(agent)
    provisioned_knowledge_base_id = (
        provision_operation.get("global_knowledge_base_id")
        if "global_knowledge_base_id" in provision_operation
        else _MISSING
    )
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
    configuration_mismatches: list[str] = []
    published_configuration_mismatches: list[str] = []
    active_configuration_mismatches: list[str] = []
    knowledge_binding_verification: str | None = None
    if revision_status == "published" and security_status == "passed":
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if not _publish_reconciliation_is_current(agent, operation_id):
            return agent
        voice = _stored_voice_resolution(agent)
        if voice is None:
            configuration_mismatches = ["voice_resolution"]
        else:
            expected_configuration = _publish_kwargs(agent, voice)
            expected_configuration["global_knowledge_base_id"] = operation.get(
                "global_knowledge_base_id"
            )
            await db.commit()
            try:
                published_provider_agent = await client.get_agent(
                    provider_agent_id,
                    version_id=latest_revision_id,
                )
                active_provider_agent = await client.get_agent(provider_agent_id)
                (
                    configuration_mismatches,
                    published_configuration_mismatches,
                    active_configuration_mismatches,
                ) = _provider_configuration_mismatch_sets(
                    published_provider_agent,
                    active_provider_agent,
                    expected_configuration,
                )
                knowledge_only = {"global_knowledge_base_id"}
                if (
                    isinstance(baseline_revision_id, str)
                    and baseline_revision_id
                    and set(configuration_mismatches) == knowledge_only
                    and set(published_configuration_mismatches) == knowledge_only
                    and set(active_configuration_mismatches) == knowledge_only
                ):
                    baseline_provider_agent = await client.get_agent(
                        provider_agent_id,
                        version_id=baseline_revision_id,
                    )
                    expected_knowledge_base_id = expected_configuration["global_knowledge_base_id"]
                    if _verified_knowledge_tool_ref_delta(
                        baseline_provider_agent,
                        published_provider_agent,
                        active_provider_agent,
                        expected_knowledge_base_id=expected_knowledge_base_id,
                    ):
                        configuration_mismatches = []
                        published_configuration_mismatches = []
                        active_configuration_mismatches = []
                        knowledge_binding_verification = "publish_tool_ref_delta"
                    elif (
                        provisioned_knowledge_base_id is not _MISSING
                        and provisioned_knowledge_base_id == expected_knowledge_base_id
                        and _created_agent_knowledge_tool_ref_matches(
                            baseline_provider_agent,
                            published_provider_agent,
                            active_provider_agent,
                            expected_knowledge_base_id=expected_knowledge_base_id,
                        )
                    ):
                        configuration_mismatches = []
                        published_configuration_mismatches = []
                        active_configuration_mismatches = []
                        knowledge_binding_verification = "create_tool_ref_binding"
            except SmallestAIError as exc:
                agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
                if not _publish_reconciliation_is_current(agent, operation_id):
                    return agent
                agent.provider_revision_id = latest_revision_id
                agent.sync_status = "provider_scanning"
                _set_provider_operation(
                    agent,
                    "publish",
                    phase="provider_config_verification",
                    lease_expires_at=None,
                    revision_id=latest_revision_id,
                    revision_status=revision_status,
                    security_status=security_status,
                    last_error=str(exc),
                    last_checked_at=datetime.now(UTC).isoformat(),
                )
                await db.commit()
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

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
    elif configuration_mismatches:
        agent.sync_status = "error"
        phase = "provider_config_mismatch"
        last_error = (
            "Smallest.ai published a revision whose resolved configuration differs from "
            "the requested draft. Mismatched fields: " + ", ".join(configuration_mismatches)
        )
    elif revision_status == "published" and security_status == "passed":
        agent.sync_status = "synced"
        agent.last_synced_at = datetime.now(UTC)
        binding = await db.scalar(
            select(AgentKnowledgeBinding).where(
                AgentKnowledgeBinding.agent_id == agent.id,
                AgentKnowledgeBinding.tenant_id == tenant_id,
            )
        )
        if binding:
            binding.sync_status = "synced"
            binding.last_synced_at = agent.last_synced_at
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
        configuration_mismatches=configuration_mismatches,
        published_configuration_mismatches=published_configuration_mismatches,
        active_configuration_mismatches=active_configuration_mismatches,
        knowledge_binding_verification=knowledge_binding_verification,
        last_error=last_error,
        last_checked_at=datetime.now(UTC).isoformat(),
    )
    await db.commit()
    return agent


async def _reconcile_smallest_config_mismatch(
    db: AsyncSession,
    *,
    agent_id: UUID,
    tenant_id: UUID,
    actor_user_id: UUID,
    client,
) -> Agent:
    """Verify a provider-side correction using reads only; never publish here."""
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    if not _provider_config_mismatch_pending(agent):
        return agent
    if not agent.provider_agent_id or not agent.provider_branch_id:
        raise HTTPException(status_code=409, detail="Provider mapping is incomplete")

    operation = _publish_operation(agent)
    operation_id = operation.get("id")
    original_revision_id = operation.get("revision_id")
    original_label = operation.get("label")
    baseline_known = "baseline_revision_id" in operation
    baseline_revision_id = operation.get("baseline_revision_id")
    if not all(
        isinstance(value, str) and value
        for value in (operation_id, original_revision_id, original_label)
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider verification history is incomplete; support review is required",
        )
    if not baseline_known or (
        baseline_revision_id is not None
        and (not isinstance(baseline_revision_id, str) or not baseline_revision_id)
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider baseline history is incomplete; support review is required",
        )
    if "global_knowledge_base_id" not in operation:
        raise HTTPException(
            status_code=409,
            detail="Provider knowledge-binding history is incomplete",
        )
    expected_knowledge_base_id = operation["global_knowledge_base_id"]
    if expected_knowledge_base_id is not None and (
        not isinstance(expected_knowledge_base_id, str) or not expected_knowledge_base_id.strip()
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider knowledge-binding history is invalid",
        )
    if agent.provider_revision_id != original_revision_id:
        raise HTTPException(
            status_code=409,
            detail="Provider revision history changed before correction verification",
        )
    voice = _stored_voice_resolution(agent)
    if voice is None:
        raise HTTPException(
            status_code=409, detail="Stored provider voice resolution is incomplete"
        )

    provider_agent_id = agent.provider_agent_id
    branch_id = agent.provider_branch_id
    expected_configuration = _publish_kwargs(agent, voice)
    expected_configuration["global_knowledge_base_id"] = expected_knowledge_base_id
    await db.commit()

    observed_revision_id: str | None = None
    observed_revision_label: str | None = None
    try:
        latest = await client.get_latest_branch_revision(
            agent_id=provider_agent_id,
            branch_id=branch_id,
        )
        observed_revision_id = _revision_id(latest)
        observed_revision_label = _revision_label(latest)
        if not observed_revision_id or not observed_revision_label:
            raise SmallestAIError("Smallest.ai returned an incomplete correction revision.")
        if observed_revision_id == baseline_revision_id:
            raise SmallestAIError(
                "Smallest.ai returned the pre-publish baseline revision.",
                status_code=409,
            )
        if (
            observed_revision_id == original_revision_id
            and observed_revision_label != original_label
        ):
            raise SmallestAIError(
                "Smallest.ai changed the original revision label during correction verification.",
                status_code=409,
            )
        revision = await client.get_branch_revision(
            agent_id=provider_agent_id,
            branch_id=branch_id,
            revision_id=observed_revision_id,
        )
        if _revision_id(revision) != observed_revision_id:
            raise SmallestAIError(
                "Smallest.ai returned conflicting correction revision details.",
                status_code=409,
            )
        detailed_revision_label = _revision_label(revision)
        if detailed_revision_label and detailed_revision_label != observed_revision_label:
            raise SmallestAIError(
                "Smallest.ai returned conflicting correction revision labels.",
                status_code=409,
            )
        revision_status, security_status = _revision_lifecycle(revision)
        configuration_mismatches: list[str] = []
        published_configuration_mismatches: list[str] = []
        active_configuration_mismatches: list[str] = []
        knowledge_binding_verification: str | None = None
        if revision_status == "published" and security_status == "passed":
            published_provider_agent = await client.get_agent(
                provider_agent_id,
                version_id=observed_revision_id,
            )
            active_provider_agent = await client.get_agent(provider_agent_id)
            (
                configuration_mismatches,
                published_configuration_mismatches,
                active_configuration_mismatches,
            ) = _provider_configuration_mismatch_sets(
                published_provider_agent,
                active_provider_agent,
                expected_configuration,
            )
            knowledge_only = {"global_knowledge_base_id"}
            if (
                set(configuration_mismatches) == knowledge_only
                and set(published_configuration_mismatches) == knowledge_only
                and set(active_configuration_mismatches) == knowledge_only
            ):
                original_provider_agent = await client.get_agent(
                    provider_agent_id,
                    version_id=original_revision_id,
                )
                if _verified_knowledge_tool_ref_delta(
                    original_provider_agent,
                    published_provider_agent,
                    active_provider_agent,
                    expected_knowledge_base_id=expected_knowledge_base_id,
                ):
                    configuration_mismatches = []
                    published_configuration_mismatches = []
                    active_configuration_mismatches = []
                    knowledge_binding_verification = "operator_tool_ref_delta"
    except SmallestAIError as exc:
        agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
        if _provider_config_recovery_is_current(agent, operation_id):
            _set_provider_operation(
                agent,
                "publish",
                phase="provider_config_mismatch",
                reconciliation_mode="configuration_recovery",
                observed_revision_id=observed_revision_id,
                observed_revision_label=observed_revision_label,
                recovery_last_error=str(exc),
                last_checked_at=datetime.now(UTC).isoformat(),
            )
            await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    if not _provider_config_recovery_is_current(agent, operation_id):
        return agent

    now = datetime.now(UTC)
    success = (
        revision_status == "published"
        and security_status == "passed"
        and not configuration_mismatches
    )
    if success:
        agent.provider_revision_id = observed_revision_id
        agent.sync_status = "synced"
        agent.last_synced_at = now
        binding = await db.scalar(
            select(AgentKnowledgeBinding).where(
                AgentKnowledgeBinding.agent_id == agent.id,
                AgentKnowledgeBinding.tenant_id == tenant_id,
            )
        )
        if binding:
            binding.sync_status = "synced"
            binding.last_synced_at = now
        await record_audit_event(
            db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="agent.provider_configuration_reconciled",
            resource_type="agent",
            resource_id=str(agent.id),
            details={
                "previous_revision_id": original_revision_id,
                "previous_revision_label": original_label,
                "reconciled_revision_id": observed_revision_id,
                "reconciled_revision_label": observed_revision_label,
                "mode": "read_only_configuration_recovery",
            },
        )
        phase = "complete"
        last_error = None
    else:
        phase = "provider_config_mismatch"
        if revision_status in PROVIDER_FAILED_STATES or security_status in PROVIDER_FAILED_STATES:
            last_error = (
                "Smallest.ai rejected the provider-side correction "
                f"(revision state: {revision_status}; security state: {security_status})."
            )
        elif revision_status != "published" or security_status != "passed":
            last_error = (
                "Smallest.ai has not finished publishing and approving the corrected revision."
            )
        else:
            last_error = (
                "Smallest.ai's corrected revision still differs from the requested configuration. "
                "Mismatched fields: " + ", ".join(configuration_mismatches)
            )

    _set_provider_operation(
        agent,
        "publish",
        phase=phase,
        reconciliation_mode="configuration_recovery",
        previous_revision_id=original_revision_id,
        previous_revision_label=original_label,
        revision_id=observed_revision_id if success else original_revision_id,
        revision_status=revision_status,
        security_status=security_status,
        observed_revision_id=observed_revision_id,
        observed_revision_label=observed_revision_label,
        reconciled_revision_id=observed_revision_id if success else None,
        reconciled_revision_label=observed_revision_label if success else None,
        configuration_mismatches=configuration_mismatches,
        published_configuration_mismatches=published_configuration_mismatches,
        active_configuration_mismatches=active_configuration_mismatches,
        knowledge_binding_verification=knowledge_binding_verification,
        recovery_last_error=None,
        last_error=last_error,
        last_checked_at=now.isoformat(),
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
    voice: VoiceResolution,
) -> Agent:
    """Update a draft, publish once, then reconcile the resulting revision."""
    operation_id = str(uuid4())
    operation_label = _provider_publish_label(label, operation_id)
    now = datetime.now(UTC)
    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    provider_agent_id = agent.provider_agent_id
    branch_id = agent.provider_branch_id
    if not provider_agent_id:
        raise HTTPException(status_code=409, detail="Provider mapping is incomplete")

    remote_knowledge_id = await _approved_bound_provider_knowledge_base_id(
        db,
        agent_id=agent.id,
        tenant_id=tenant_id,
    )

    agent.sync_status = "publishing"
    config = _provider_config(agent)
    config["requested_voice_id"] = voice.requested_voice_id
    config["resolved_voice_id"] = voice.resolved_voice_id
    config["voice_model"] = voice.synthesizer_model
    config["voice_resolution_source"] = voice.source
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
    await db.commit()

    provider_phase = "webhook_subscriptions"
    existing_remote_knowledge_id = None
    try:
        await client.set_agent_webhook_subscriptions(
            agent_id=provider_agent_id,
            webhook_id=settings.smallest_webhook_id,
        )
        if not branch_id:
            provider_phase = "branch_lookup"
            branch_id = await client.get_default_branch_id(provider_agent_id)
        provider_phase = "knowledge_binding_lookup"
        existing_remote_knowledge_id = await client.get_agent_knowledge_base_id(provider_agent_id)
    except SmallestAIError as exc:
        await _mark_publish_failure(
            db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            sync_status="dirty",
            error=exc,
            phase=provider_phase,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail=_publish_error_detail(provider_phase, exc),
        ) from exc

    agent = await _tenant_agent(db, agent_id, tenant_id, for_update=True)
    operation = _publish_operation(agent)
    if operation.get("id") != operation_id or agent.sync_status != "publishing":
        raise HTTPException(status_code=409, detail="Provider operation was superseded")
    agent.provider_branch_id = branch_id
    snapshot = _publish_kwargs(agent, voice)
    # Smallest merges this partial draft payload. Replaying an unchanged
    # globalKnowledgeBaseId can rematerialize the provider's KB tool, so only
    # write the field when VAV's desired binding differs from the active one.
    knowledge_binding_action = _apply_knowledge_binding_delta(
        snapshot,
        desired_knowledge_base_id=remote_knowledge_id,
        existing_knowledge_base_id=existing_remote_knowledge_id,
    )
    _set_provider_operation(
        agent,
        "publish",
        phase="draft_update",
        global_knowledge_base_id=remote_knowledge_id,
        existing_global_knowledge_base_id=existing_remote_knowledge_id,
        knowledge_binding_action=knowledge_binding_action,
        lease_expires_at=(datetime.now(UTC) + PROVIDER_OPERATION_LEASE).isoformat(),
    )
    await db.commit()

    provider_phase = "draft_update"
    try:
        await client.update_agent_draft(
            agent_id=provider_agent_id,
            branch_id=branch_id,
            **snapshot,
        )
        provider_phase = "baseline_revision"
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
            phase=provider_phase,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail=_publish_error_detail(provider_phase, exc),
        ) from exc

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
            phase="publish_request",
        )
        detail = (
            "Smallest.ai publish outcome is unknown; use Check status before any retry"
            if exc.ambiguous
            else _publish_error_detail("publish_request", exc)
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
    if data.voice_id:
        await _require_provider_voice(
            data.voice_provider,
            db,
            current_user.tenant_id,
            data.voice_id,
            list(data.supported_languages),
        )
    agent = Agent(tenant_id=current_user.tenant_id, **data.model_dump())
    db.add(agent)
    await db.flush()
    return AgentResponse.model_validate(agent)


@router.post("/ai-draft", response_model=AgentAIDraftResponse)
async def create_agent_ai_draft(
    data: AgentAIDraftRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Generate a reviewable local draft without creating or publishing an agent."""
    await enforce_rate_limit(
        request,
        scope="agent-ai-draft",
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

    catalog = await get_provider_catalog(current_user=current_user, db=db)
    knowledge_result = await db.execute(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id == current_user.tenant_id,
            KnowledgeBase.approval_status == "approved",
        )
        .order_by(KnowledgeBase.name.asc())
    )
    knowledge_bases = [
        KnowledgeBaseSummary(
            id=knowledge.id,
            name=knowledge.name,
            description=knowledge.description or "",
        )
        for knowledge in knowledge_result.scalars().all()
    ]
    try:
        generated = await generate_agent_ai_draft(
            api_key=api_key,
            brief=data.brief,
            provider_preference=data.provider_preference,
            primary_language=data.primary_language,
            timezone=data.timezone,
            voices=[voice.model_dump() for voice in catalog.voices],
            available_languages=[language.code for language in catalog.languages],
            knowledge_bases=knowledge_bases,
        )
    except AgentAIWizardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="OpenAI could not generate an agent draft right now. Please retry.",
        ) from exc

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.ai_draft_generated",
        resource_type="agent_draft",
        resource_id=None,
        details={
            "model": generated.model,
            "voice_provider": generated.draft.voice_provider,
            "primary_language": generated.draft.language,
            "knowledge_base_id": (
                str(generated.recommended_knowledge_base_id)
                if generated.recommended_knowledge_base_id
                else None
            ),
        },
    )
    return generated


@router.get("/provider/status")
async def get_provider_status(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return non-sensitive provider readiness for the operator console."""
    client, smallest_source, smallest_updated_at = await _tenant_smallest_client(
        db, current_user.tenant_id
    )
    sarvam, sarvam_source, sarvam_updated_at = await _tenant_sarvam_client(
        db,
        current_user.tenant_id,
    )
    elevenlabs, elevenlabs_source, elevenlabs_updated_at = await _tenant_elevenlabs_client(
        db, current_user.tenant_id
    )
    inworld, inworld_source, inworld_updated_at = await _tenant_inworld_client(
        db, current_user.tenant_id
    )
    return {
        "provider": "smallest",
        "configured": client.is_configured,
        "webhook_configured": bool(
            settings.smallest_webhook_secret and settings.smallest_webhook_id
        ),
        "base_url": settings.smallest_base_url,
        "providers": {
            "smallest": {
                "configured": client.is_configured,
                "agent_runtime": True,
                "voice_preview": client.is_configured,
                "source": smallest_source,
                "updated_at": (smallest_updated_at.isoformat() if smallest_updated_at else None),
            },
            "sarvam": {
                "configured": sarvam.is_configured,
                "agent_runtime": True,
                "voice_preview": sarvam.is_configured,
                "source": sarvam_source,
                "updated_at": sarvam_updated_at.isoformat() if sarvam_updated_at else None,
            },
            "elevenlabs": {
                "configured": elevenlabs.is_configured,
                "agent_runtime": True,
                "voice_preview": elevenlabs.is_configured,
                "source": elevenlabs_source,
                "updated_at": (
                    elevenlabs_updated_at.isoformat() if elevenlabs_updated_at else None
                ),
            },
            "inworld": {
                "configured": inworld.is_configured,
                "agent_runtime": True,
                "voice_preview": inworld.is_configured,
                "source": inworld_source,
                "updated_at": inworld_updated_at.isoformat() if inworld_updated_at else None,
            },
        },
    }


@router.put(
    "/provider/sarvam/credential",
    response_model=ProviderCredentialStatus,
)
async def save_sarvam_credential(
    data: SarvamCredentialRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Store a write-only tenant Sarvam key in an authenticated envelope."""
    try:
        encrypted = encrypt_integration_config({"api_key": data.api_key})
    except IntegrationConfigUnavailableError as exc:
        raise HTTPException(
            status_code=500,
            detail="Credential encryption is unavailable",
        ) from exc

    credential = await _tenant_sarvam_credential(
        db,
        current_user.tenant_id,
        for_update=True,
    )
    action = "provider_credential.rotated" if credential else "provider_credential.created"
    if credential is None:
        credential = ProviderCredential(
            tenant_id=current_user.tenant_id,
            provider="sarvam",
            encrypted_config=encrypted,
            encryption_version=1,
            is_active=True,
        )
        db.add(credential)
    else:
        credential.encrypted_config = encrypted
        credential.encryption_version = 1
        credential.is_active = True
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action=action,
        resource_type="provider_credential",
        resource_id=str(credential.id),
        details={"provider": "sarvam"},
    )
    return ProviderCredentialStatus(
        configured=True,
        source="workspace",
        updated_at=credential.updated_at,
    )


@router.delete(
    "/provider/sarvam/credential",
    response_model=ProviderCredentialStatus,
)
async def delete_sarvam_credential(
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    credential = await _tenant_sarvam_credential(
        db,
        current_user.tenant_id,
        for_update=True,
    )
    if credential is not None:
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
            details={"provider": "sarvam"},
        )
    platform = get_sarvam_client()
    return ProviderCredentialStatus(
        configured=platform.is_configured,
        source="platform" if platform.is_configured else "none",
    )


@router.get("/provider/catalog", response_model=AgentProviderCatalog)
async def get_provider_catalog(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return public voices plus only this tenant's completed private clones."""
    client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
    smallest_error: HTTPException | None = None
    try:
        voices = await _tenant_voice_catalog(client, db, current_user.tenant_id)
    except SmallestAIError as exc:
        voices = []
        smallest_error = HTTPException(status_code=exc.status_code, detail=str(exc))
    except Exception:
        voices = []
        smallest_error = HTTPException(
            status_code=502, detail="Could not load the Smallest.ai voice catalog"
        )

    combined_voices = [{"provider": "smallest", **voice} for voice in voices]
    sarvam, _, _ = await _tenant_sarvam_client(db, current_user.tenant_id)
    if sarvam.is_configured:
        combined_voices.extend(sarvam_voice_catalog())
    elevenlabs_error: HTTPException | None = None
    elevenlabs, _, _ = await _tenant_elevenlabs_client(db, current_user.tenant_id)
    if elevenlabs.is_configured:
        try:
            combined_voices.extend(await elevenlabs.list_voices())
        except ElevenLabsError as exc:
            elevenlabs_error = HTTPException(status_code=exc.status_code, detail=str(exc))
    inworld_error: HTTPException | None = None
    inworld, _, _ = await _tenant_inworld_client(db, current_user.tenant_id)
    if inworld.is_configured:
        try:
            combined_voices.extend(await inworld.list_voices())
        except InworldError as exc:
            inworld_error = HTTPException(status_code=exc.status_code, detail=str(exc))
    if not combined_voices:
        if inworld_error is not None:
            raise inworld_error
        if elevenlabs_error is not None:
            raise elevenlabs_error
        if smallest_error is not None:
            raise smallest_error

    return AgentProviderCatalog(
        voices=combined_voices,
        languages=language_catalog(combined_voices),
        templates=AGENT_TEMPLATES,
        field_capabilities=PROVIDER_FIELD_CAPABILITIES,
    )


@router.get("/provider/voice-clones", response_model=list[VoiceCloneResponse])
async def list_voice_clones(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VoiceClone)
        .where(VoiceClone.tenant_id == current_user.tenant_id)
        .order_by(VoiceClone.created_at.desc())
    )
    return [VoiceCloneResponse.model_validate(clone) for clone in result.scalars().all()]


@router.post(
    "/provider/voice-clones",
    response_model=VoiceCloneResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_voice_clone(
    display_name: str = Form(...),
    language: str = Form("en"),
    accent: str = Form("indian"),
    gender: str = Form(""),
    description: str = Form(""),
    model: str = Form("lightning-v3.1-pro"),
    consent_confirmed: bool = Form(...),
    media: UploadFile = File(...),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Create a tenant-owned clone without retaining the reference recording."""
    name = " ".join(display_name.split())
    if not 1 <= len(name) <= 255:
        raise HTTPException(status_code=422, detail="Voice name must be 1–255 characters")
    normalized_language = language_code(language)
    if not normalized_language:
        raise HTTPException(status_code=422, detail="Language must be a valid provider code")
    normalized_accent = " ".join(accent.split())[:100] or None
    normalized_gender = gender.strip().lower() or None
    if normalized_gender not in {None, "female", "male"}:
        raise HTTPException(status_code=422, detail="Gender must be female or male")
    normalized_description = " ".join(description.split()) or None
    if normalized_description and len(normalized_description) > 1000:
        raise HTTPException(status_code=422, detail="Description must be 1,000 characters or less")
    if model not in VOICE_CLONE_MODELS:
        raise HTTPException(status_code=422, detail="Unsupported voice cloning model")
    if consent_confirmed is not True:
        raise HTTPException(
            status_code=422,
            detail="Confirm that the speaker consented to creation and business use of this clone",
        )

    filename = PurePath(media.filename or "voice-sample.wav").name
    suffix = PurePath(filename).suffix.lower()
    content_type = (media.content_type or "").split(";", 1)[0].strip().lower()
    if (
        content_type not in VOICE_CLONE_MEDIA_TYPES
        or suffix not in VOICE_CLONE_MEDIA_TYPES[content_type]
    ):
        raise HTTPException(
            status_code=422,
            detail="Use a WAV, MP3, WebM, or MP4 voice sample",
        )
    content = await media.read(MAX_VOICE_CLONE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="Voice sample is empty")
    if len(content) > MAX_VOICE_CLONE_BYTES:
        raise HTTPException(status_code=413, detail="Voice sample must be 5 MB or smaller")
    if not _valid_voice_sample_signature(content, suffix):
        raise HTTPException(
            status_code=422,
            detail="Voice sample content does not match its file type",
        )

    now = datetime.now(UTC)
    clone = VoiceClone(
        tenant_id=current_user.tenant_id,
        display_name=name,
        description=normalized_description,
        language=normalized_language,
        accent=normalized_accent,
        gender=normalized_gender,
        model=model,
        model_ids=[model],
        status="creating",
        consent_confirmed_at=now,
        consent_confirmed_by=current_user.id,
    )
    db.add(clone)
    await db.flush()
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="voice_clone.creation_started",
        resource_type="voice_clone",
        resource_id=str(clone.id),
        details={
            "display_name": name,
            "language": normalized_language,
            "accent": normalized_accent,
            "model": model,
            "reference_audio_retained": False,
            "consent_confirmed": True,
        },
    )
    # Persist the operation marker before the external mutation. A timeout can
    # then be reconciled safely from the provider-side tag without re-uploading.
    await db.commit()

    client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
    try:
        remote = await client.create_voice_clone(
            display_name=name,
            file_name=filename,
            content=content,
            content_type=content_type,
            language=normalized_language,
            accent=normalized_accent,
            description=normalized_description,
            tags=[
                _clone_operation_tag(clone.id),
                "vav",
                *([normalized_gender] if normalized_gender else []),
            ],
            model=model,
        )
    except SmallestAIError as exc:
        clone = await _tenant_voice_clone(db, current_user.tenant_id, clone.id, for_update=True)
        clone.status = "creation_unknown" if exc.ambiguous else "error"
        clone.last_error = str(exc)
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="voice_clone.creation_failed",
            resource_type="voice_clone",
            resource_id=str(clone.id),
            details={"ambiguous": exc.ambiguous, "status": clone.status},
        )
        await db.commit()
        detail = (
            "Voice clone creation outcome is unknown. Use Check status before retrying."
            if exc.ambiguous
            else str(exc)
        )
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    clone = await _tenant_voice_clone(db, current_user.tenant_id, clone.id, for_update=True)
    _apply_remote_clone(clone, remote)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="voice_clone.created",
        resource_type="voice_clone",
        resource_id=str(clone.id),
        details={"provider_voice_id": clone.provider_voice_id, "status": clone.status},
    )
    await db.flush()
    return VoiceCloneResponse.model_validate(clone)


@router.post(
    "/provider/voice-clones/{clone_id}/refresh",
    response_model=VoiceCloneResponse,
)
async def refresh_voice_clone(
    clone_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    clone = await _tenant_voice_clone(db, current_user.tenant_id, clone_id)
    try:
        client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
        remote_clones = await client.list_voice_clones()
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    marker = _clone_operation_tag(clone.id)
    remote = next(
        (
            item
            for item in remote_clones
            if (
                clone.provider_voice_id
                and str(item.get("voiceId") or item.get("id") or item.get("_id") or "")
                == clone.provider_voice_id
            )
            or marker in _remote_clone_tags(item)
        ),
        None,
    )
    clone = await _tenant_voice_clone(db, current_user.tenant_id, clone_id, for_update=True)
    if remote is None:
        clone.status = "creation_unknown" if not clone.provider_voice_id else "missing"
        clone.last_error = "The provider clone could not be found during reconciliation."
        clone.last_synced_at = datetime.now(UTC)
    else:
        _apply_remote_clone(clone, remote)
    await db.flush()
    return VoiceCloneResponse.model_validate(clone)


@router.delete(
    "/provider/voice-clones/{clone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_voice_clone(
    clone_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    clone = await _tenant_voice_clone(db, current_user.tenant_id, clone_id, for_update=True)
    if clone.provider_voice_id:
        agents = (
            (
                await db.execute(
                    select(Agent.name).where(
                        Agent.tenant_id == current_user.tenant_id,
                        Agent.voice_id == clone.provider_voice_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if agents:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Replace this voice on these agents before deleting it: " + ", ".join(agents)
                ),
            )
    elif clone.status == "creation_unknown":
        raise HTTPException(
            status_code=409,
            detail="Check status before deleting a clone whose provider outcome is unknown",
        )

    if clone.provider_voice_id:
        await db.commit()
        try:
            client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
            await client.delete_voice_clone(clone.provider_voice_id)
        except SmallestAIError as exc:
            clone = await _tenant_voice_clone(db, current_user.tenant_id, clone_id, for_update=True)
            clone.status = "deletion_unknown" if exc.ambiguous else "delete_error"
            clone.last_error = str(exc)
            await db.commit()
            detail = (
                "Voice clone deletion outcome is unknown. Check Smallest.ai before retrying."
                if exc.ambiguous
                else str(exc)
            )
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    clone = await _tenant_voice_clone(db, current_user.tenant_id, clone_id, for_update=True)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="voice_clone.deleted",
        resource_type="voice_clone",
        resource_id=str(clone.id),
        details={"provider_voice_id": clone.provider_voice_id},
    )
    await db.delete(clone)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/provider/voice-preview",
    response_class=Response,
    responses={
        200: {
            "description": "Short server-generated voice preview",
            "content": {"audio/wav": {}},
        }
    },
)
async def preview_provider_voice(
    data: VoicePreviewRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Proxy one bounded provider-owned phrase without exposing the server API key."""
    await enforce_rate_limit(
        request,
        scope="voice-preview",
        limit=20,
        window_seconds=60,
        subject=str(current_user.id),
        bind_to_client=False,
    )
    if data.provider == "sarvam":
        sarvam, _, _ = await _tenant_sarvam_client(db, current_user.tenant_id)
        voice = next(
            (item for item in sarvam_voice_catalog() if item["id"] == data.voice_id),
            None,
        )
        if voice is None:
            raise HTTPException(status_code=422, detail="Voice is not available in Sarvam")
        advertised_languages = voice.get("languages") or []
        preview_language = data.language or (
            str(advertised_languages[0]) if advertised_languages else "en"
        )
        try:
            audio = await sarvam.synthesize_voice_preview(
                speaker=data.voice_id.removeprefix("sarvam:"),
                language=preview_language,
            )
        except SarvamAIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    if data.provider == "elevenlabs":
        elevenlabs, _, _ = await _tenant_elevenlabs_client(db, current_user.tenant_id)
        try:
            voices = await elevenlabs.list_voices()
        except ElevenLabsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        voice = next((item for item in voices if item["id"] == data.voice_id), None)
        if voice is None:
            raise HTTPException(
                status_code=422, detail="Voice is not available to this ElevenLabs workspace"
            )
        advertised_languages = voice.get("languages") or []
        preview_language = data.language or (
            str(advertised_languages[0]) if advertised_languages else "en"
        )
        try:
            audio = await elevenlabs.synthesize_voice_preview(
                voice_id=data.voice_id.removeprefix("elevenlabs:"),
                language=preview_language,
            )
        except ElevenLabsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    if data.provider == "inworld":
        inworld, _, _ = await _tenant_inworld_client(db, current_user.tenant_id)
        try:
            voices = await inworld.list_voices()
            if not any(item["id"] == data.voice_id for item in voices):
                raise HTTPException(
                    status_code=422, detail="Voice is not available to this Inworld workspace"
                )
            audio = await inworld.voice_preview(data.voice_id.removeprefix("inworld:"))
        except InworldError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
    try:
        voices = await _tenant_voice_catalog(client, db, current_user.tenant_id)
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    voice = next((item for item in voices if item.get("id") == data.voice_id), None)
    if voice is None:
        raise HTTPException(status_code=422, detail="Voice is not available to this workspace")
    if not voice.get("synthesizer_model"):
        raise HTTPException(status_code=422, detail="Voice is not available for preview")

    advertised_languages = voice.get("languages") or []
    preview_language = data.language or (
        str(advertised_languages[0]) if advertised_languages else None
    )
    if not preview_language:
        raise HTTPException(
            status_code=422,
            detail="Voice preview language could not be verified from provider metadata",
        )
    preview_compatibility, preview_unsupported = voice_language_compatibility(
        voices,
        data.voice_id,
        [preview_language],
    )
    if preview_compatibility != LanguageCompatibilityStatus.COMPATIBLE:
        detail = "Voice does not support the requested preview language"
        if preview_unsupported:
            detail += ": " + ", ".join(preview_unsupported)
        raise HTTPException(status_code=422, detail=detail)

    pool = voice.get("_model_pool")
    direct_model = {
        VoiceModelPool.STANDARD: "lightning_v3.1",
        VoiceModelPool.PRO: "lightning_v3.1_pro",
    }.get(pool)
    if not direct_model:
        # Atoms can route newly-added public voices from the voice ID alone,
        # while direct Waves TTS requires an explicit Standard/Pro model token.
        raise HTTPException(
            status_code=422,
            detail="Voice preview pool could not be verified from provider metadata",
        )

    try:
        audio = await client.synthesize_voice_preview(
            voice_id=data.voice_id,
            model=direct_model,
            language=preview_language,
        )
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{agent_id}/smallest/provision", response_model=AgentResponse)
async def provision_smallest_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create the remote Atoms agent only after an operator explicitly requests it."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.voice_provider != "smallest":
        raise HTTPException(
            status_code=409,
            detail=(
                "This agent uses the VAV realtime voice runtime and cannot be provisioned "
                "into Smallest.ai. "
                "It must be activated on the VAV realtime runtime."
            ),
        )
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

    client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
    remote_knowledge_id = await _approved_bound_provider_knowledge_base_id(
        db,
        agent_id=agent.id,
        tenant_id=current_user.tenant_id,
    )
    voice_selection = _voice_configuration_snapshot(agent)
    # Never retain a row lock while waiting on the provider catalog. A second
    # provisioner can race here, so re-lock and revalidate before recording the
    # durable create lease.
    await db.commit()
    voice = await _require_tenant_voice(
        client,
        db,
        current_user.tenant_id,
        agent.voice_id,
        list(agent.supported_languages),
    )
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Agent is already provisioned on Smallest.ai")
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(
            status_code=409,
            detail="Agent has an unresolved provider operation and cannot be provisioned again",
        )
    if _voice_configuration_snapshot(agent) != voice_selection:
        raise HTTPException(
            status_code=409,
            detail=(
                "Agent voice configuration changed during provider validation; retry provisioning"
            ),
        )
    current_remote_knowledge_id = await _approved_bound_provider_knowledge_base_id(
        db,
        agent_id=agent.id,
        tenant_id=current_user.tenant_id,
    )
    if current_remote_knowledge_id != remote_knowledge_id:
        raise HTTPException(
            status_code=409,
            detail="Agent knowledge binding changed during provider validation; retry provisioning",
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
        global_knowledge_base_id=remote_knowledge_id,
    )
    await db.commit()

    try:
        provider_agent_id = await client.create_agent(
            name=name,
            description=description,
            global_knowledge_base_id=remote_knowledge_id,
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
        voice=voice,
    )
    return AgentResponse.model_validate(agent)


@router.post("/{agent_id}/smallest/sync", response_model=AgentResponse)
async def sync_smallest_agent(
    agent_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Publish a dirty draft, or read-only verify an unresolved provider result."""
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.voice_provider != "smallest":
        raise HTTPException(
            status_code=409,
            detail=("VAV realtime voice agents are synchronized by VAV, not Smallest.ai."),
        )
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

    client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
    if _provider_config_mismatch_pending(agent):
        agent = await _reconcile_smallest_config_mismatch(
            db,
            agent_id=agent_id,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            client=client,
        )
        return AgentResponse.model_validate(agent)
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
    voice_selection = _voice_configuration_snapshot(agent)
    # Release the row lock around the provider catalog read, then re-lock and
    # prove no competing provider operation or voice edit superseded this sync.
    await db.commit()
    voice = await _require_tenant_voice(
        client,
        db,
        current_user.tenant_id,
        agent.voice_id,
        list(agent.supported_languages),
    )
    agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
    if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
        raise HTTPException(status_code=409, detail="Agent provider operation is unresolved")
    if agent.sync_status == "synced":
        raise HTTPException(status_code=409, detail="Agent is already in sync")
    if not agent.provider_agent_id:
        raise HTTPException(status_code=409, detail="Provider mapping is incomplete")
    if _voice_configuration_snapshot(agent) != voice_selection:
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
        voice=voice,
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
    client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)

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
    if agent.sync_status != "synced" or not agent.provider_revision_id or not agent.last_synced_at:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot take calls until its current provider revision is fully synced",
        )
    try:
        client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
        session = await client.create_browser_session(
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


@router.post(
    "/{agent_id}/livekit/session",
    response_model=LiveKitSessionResponse,
)
async def create_livekit_browser_session(
    agent_id: UUID,
    data: LiveKitSessionRequest,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=16,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    """Reserve a governed call and mint one short-lived, room-scoped WebRTC token."""
    await enforce_rate_limit(
        request,
        scope="livekit-browser-session",
        limit=10,
        window_seconds=60,
        subject=f"{current_user.tenant_id}:{current_user.id}",
        bind_to_client=False,
    )
    call_id = uuid5(
        LIVEKIT_BROWSER_IDEMPOTENCY_NAMESPACE,
        (f"{current_user.tenant_id}:{agent_id}:{current_user.id}:{idempotency_key}"),
    )
    request_fingerprint = _livekit_browser_request_fingerprint(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        user_id=current_user.id,
        idempotency_key=idempotency_key,
        variables=dict(data.variables),
    )
    # Perform the first readiness pass without locks, then end its transaction
    # before the external worker probe. The short locked pass below is the
    # authoritative capacity reservation.
    await _livekit_browser_runtime(
        db,
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        for_update=False,
    )
    await db.rollback()
    try:
        await LiveKitSIPProvider(
            url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        ).verify_worker(
            health_url=settings.livekit_worker_health_url,
            agent_name=settings.livekit_agent_name,
        )
    except LiveKitSIPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LiveKit worker is unavailable: {exc}",
        ) from exc

    # Every call path takes the advisory runtime lock before any Agent row
    # lock. This global order avoids advisory/FK row-lock deadlocks with
    # concurrent inbound and outbound reservations on PostgreSQL.
    await lock_agent_runtime_limits(
        db,
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
    )
    agent, profile = await _livekit_browser_runtime(
        db,
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        for_update=True,
    )
    existing_call = await db.scalar(
        select(Call)
        .where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing_call is not None:
        metadata = (
            existing_call.call_metadata if isinstance(existing_call.call_metadata, dict) else {}
        )
        stored_fingerprint = metadata.get("browser_session_request_fingerprint")
        if not isinstance(stored_fingerprint, str) or not hmac.compare_digest(
            stored_fingerprint,
            request_fingerprint,
        ):
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was already used for a different browser session",
            )
        dispatch_id = metadata.get("livekit_dispatch_id")
        raw_deadline = metadata.get("join_expires_at")
        try:
            join_deadline = (
                datetime.fromisoformat(raw_deadline.replace("Z", "+00:00")).astimezone(UTC)
                if isinstance(raw_deadline, str)
                else None
            )
        except ValueError:
            join_deadline = None
        remaining_seconds = (
            int((join_deadline - datetime.now(UTC)).total_seconds())
            if join_deadline is not None
            else 0
        )
        room_name = str(existing_call.provider_call_sid or "")
        participant_identity = str(metadata.get("browser_participant_identity") or "")
        reserved_duration = metadata.get("reserved_max_duration_seconds")
        can_reissue = (
            existing_call.status == "initiated"
            and metadata.get("session_issuance") == "issued"
            and isinstance(dispatch_id, str)
            and bool(dispatch_id.strip())
            and room_name == f"vav-browser-{call_id}"
            and participant_identity == f"browser-{call_id}"
            and isinstance(reserved_duration, int)
            and not isinstance(reserved_duration, bool)
            and 30 <= reserved_duration <= 7200
            and 1 <= remaining_seconds <= BROWSER_TOKEN_TTL_SECONDS
        )
        if not can_reissue:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "This browser-session attempt is still preparing, already used, "
                    "terminal, or expired; start a new attempt"
                ),
            )
        provider = LiveKitBrowserSessionProvider(
            url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        access_token = provider.mint_access_token(
            room_name=room_name,
            participant_identity=participant_identity,
            expires_in=remaining_seconds,
        )
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="agent.browser_session_token_reissued",
            resource_type="call",
            resource_id=str(call_id),
            details={"remaining_ttl_seconds": remaining_seconds},
        )
        await db.commit()
        return LiveKitSessionResponse(
            url=settings.livekit_url,
            access_token=access_token,
            room_name=room_name,
            participant_identity=participant_identity,
            expires_in=remaining_seconds,
            call_id=call_id,
            session_id=str(call_id),
            max_duration_seconds=reserved_duration,
        )
    try:
        await enforce_runtime_capacity(
            db,
            model=agent,
            profile=profile,
            lock_already_held=True,
        )
    except RuntimeCapacityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=402 if exc.kind == "budget" else 429,
            detail=str(exc),
        ) from exc

    reserved_max_duration_seconds = int(agent.max_call_duration_seconds)
    room_name = f"vav-browser-{call_id}"
    participant_identity = f"browser-{call_id}"
    issued_at = datetime.now(UTC)
    join_expires_at = issued_at + timedelta(seconds=BROWSER_TOKEN_TTL_SECONDS)
    call = Call(
        id=call_id,
        tenant_id=current_user.tenant_id,
        agent_id=agent.id,
        direction="inbound",
        status="initiated",
        from_number="browser",
        to_number="voice-agent",
        provider="livekit_webrtc",
        provider_call_sid=room_name,
        call_metadata={
            "agent_configuration": agent_configuration_snapshot(agent),
            "conversation_type": "webcall",
            "channel": "browser",
            # Variables remain server-side. The worker reads them only from
            # this tenant-bound row and applies matching prompt placeholders.
            "browser_variables": dict(data.variables),
            "browser_session_request_fingerprint": request_fingerprint,
            "browser_participant_identity": participant_identity,
            "join_expires_at": join_expires_at.isoformat(),
            "reserved_max_duration_seconds": reserved_max_duration_seconds,
            "speech_provider": "inworld",
            "runtime": {
                "transport": "livekit_webrtc",
                "speech_provider": "inworld",
                "llm_provider": profile.llm_provider,
                "llm_model": profile.llm_model,
                "stt_model": "inworld/inworld-stt-1",
                "stt_language": (
                    agent.language if profile.stt_language == "auto" else profile.stt_language
                ),
                "stt_language_configured": profile.stt_language,
                "tts_model": "inworld-tts-2",
                "tts_delivery_mode": str(
                    (
                        profile.runtime_config.get("tts_delivery_mode")
                        if isinstance(profile.runtime_config, dict)
                        else None
                    )
                    or "balanced"
                ).lower(),
                "recording_enabled": False,
                "max_duration_seconds": reserved_max_duration_seconds,
            },
            "livekit_room": room_name,
            "session_issuance": "reserved",
        },
    )
    db.add(call)
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.browser_session_reserved",
        resource_type="call",
        resource_id=str(call.id),
        details={"agent_id": str(agent.id), "transport": "livekit_webrtc"},
    )
    # The worker must be able to see the durable identity before dispatch can
    # start. This commit also atomically consumes the capacity reservation.
    await db.commit()

    try:
        # Revalidate after the reservation commit. A deactivation,
        # credential removal, or knowledge revocation that won this race must
        # prevent a usable browser credential from being returned.
        current_agent, _current_profile = await _livekit_browser_runtime(
            db,
            tenant_id=current_user.tenant_id,
            agent_id=agent_id,
            for_update=False,
        )
        current_agent_id = current_agent.id
        # Do not retain a read transaction while waiting for LiveKit. The
        # signed durable reservation and worker join-time validation close the
        # remaining readiness race without holding database locks over I/O.
        await db.rollback()
    except HTTPException as exc:
        await db.rollback()
        await _mark_livekit_browser_issuance_failed(
            db,
            tenant_id=current_user.tenant_id,
            call_id=call_id,
            ambiguous=False,
            failure_type="RuntimeReadinessChanged",
        )
        raise exc

    try:
        session = await LiveKitBrowserSessionProvider(
            url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        ).create_session(
            tenant_id=current_user.tenant_id,
            agent_id=current_agent_id,
            call_id=call_id,
            agent_name=settings.livekit_agent_name,
            max_call_duration_seconds=reserved_max_duration_seconds,
        )
    except asyncio.CancelledError:
        await _mark_livekit_browser_issuance_failed_despite_cancellation(
            db,
            tenant_id=current_user.tenant_id,
            call_id=call_id,
            ambiguous=False,
            failure_type="CancelledError",
        )
        raise
    except LiveKitBrowserSessionError as exc:
        await _mark_livekit_browser_issuance_failed(
            db,
            tenant_id=current_user.tenant_id,
            call_id=call_id,
            ambiguous=exc.ambiguous,
            failure_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        stored_call = await db.scalar(
            select(Call)
            .where(Call.id == call_id, Call.tenant_id == current_user.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if stored_call is None or stored_call.status in TERMINAL_CALL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Browser call reservation is no longer active",
            )
        stored_call.call_metadata = {
            **(stored_call.call_metadata or {}),
            "livekit_dispatch_id": session.dispatch_id,
            "session_issuance": "issued",
        }
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="agent.browser_session_issued",
            resource_type="call",
            resource_id=str(call_id),
            details={
                "agent_id": str(current_agent_id),
                "transport": "livekit_webrtc",
                "expires_in": session.expires_in,
            },
        )
        response = LiveKitSessionResponse(
            url=settings.livekit_url,
            access_token=session.access_token,
            room_name=session.room_name,
            participant_identity=session.participant_identity,
            expires_in=session.expires_in,
            call_id=call_id,
            session_id=str(call_id),
            max_duration_seconds=reserved_max_duration_seconds,
        )
        await db.commit()
    except (Exception, asyncio.CancelledError) as exc:
        cleanup_succeeded = await _delete_browser_room_despite_cancellation(
            room_name=session.room_name
        )
        try:
            await db.rollback()
            await _mark_livekit_browser_issuance_failed(
                db,
                tenant_id=current_user.tenant_id,
                call_id=call_id,
                ambiguous=not cleanup_succeeded,
                failure_type=type(exc).__name__,
            )
        except Exception:
            # The committed reservation still has a bounded join deadline and
            # Celery stale-session recovery if the database is unavailable.
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(
            status_code=500,
            detail="VAV could not finalize the LiveKit browser session",
        ) from exc
    return response


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
    deprovision_existing_provider: bool = False,
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
    if "language_switching_enabled" in changes and "language_switching_mode" not in changes:
        changes["language_switching_mode"] = (
            "automatic" if changes["language_switching_enabled"] else "disabled"
        )
    elif "language_switching_mode" in changes and "language_switching_enabled" not in changes:
        changes["language_switching_enabled"] = changes["language_switching_mode"] == "automatic"
    elif (
        "supported_languages" in changes
        and len(supported_languages) == 1
        and agent.language_switching_enabled
        and "language_switching_enabled" not in changes
    ):
        # Shrinking an automatic agent to one language must not leave an
        # impossible switching policy behind.
        changes["language_switching_enabled"] = False
        changes["language_switching_mode"] = "disabled"

    switching_enabled = changes.get(
        "language_switching_enabled",
        agent.language_switching_enabled,
    )
    switching_mode = changes.get("language_switching_mode", agent.language_switching_mode)
    try:
        validate_language_configuration(
            language,
            supported_languages,
            switching_enabled,
            switching_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # AgentUpdate has already normalized language values. The database row is
    # authoritative, so a full editor payload must behave like a semantic PATCH:
    # unchanged values neither trigger provider I/O nor dirty a published agent.
    effective_changes = _effective_agent_changes(agent, changes)
    voice_provider_changed = "voice_provider" in effective_changes
    provider_switch_requested = bool(
        "voice_provider" in effective_changes
        and agent.provider_agent_id
        and effective_changes["voice_provider"] != "smallest"
    )
    if provider_switch_requested and not deprovision_existing_provider:
        raise HTTPException(
            status_code=409,
            detail=(
                "Confirm provider deprovisioning before switching this agent to the VAV "
                "realtime runtime. "
                "The Smallest.ai remote agent must be archived first."
            ),
        )
    resulting_voice_id = effective_changes.get("voice_id", agent.voice_id)
    resulting_voice_provider = effective_changes.get("voice_provider", agent.voice_provider)
    voice_inputs_changed = bool(VOICE_PREFLIGHT_FIELDS.intersection(effective_changes))
    if resulting_voice_id and voice_inputs_changed:
        baseline_fields = set(effective_changes) | VOICE_CONFIGURATION_FIELDS
        if provider_switch_requested:
            baseline_fields.update(
                {
                    "provider_agent_id",
                    "provider_branch_id",
                    "provider_revision_id",
                    "provider_config",
                    "sync_status",
                    "last_synced_at",
                }
            )
        baseline = _agent_fields_snapshot(agent, baseline_fields)
        await db.rollback()
        await _require_provider_voice(
            resulting_voice_provider,
            db,
            current_user.tenant_id,
            resulting_voice_id,
            list(supported_languages),
        )
        agent = await _tenant_agent(db, agent_id, current_user.tenant_id, for_update=True)
        if agent.sync_status in UNRESOLVED_PROVIDER_STATES:
            raise HTTPException(
                status_code=409,
                detail="Agent cannot be edited while its provider operation is unresolved",
            )
        if _agent_fields_snapshot(agent, baseline_fields) != baseline:
            raise HTTPException(
                status_code=409,
                detail="Agent configuration changed during voice validation; retry the edit",
            )

    deprovisioned_provider_agent_id: str | None = None
    if provider_switch_requested:
        await _require_agent_idle_for_provider_removal(
            db,
            agent=agent,
            tenant_id=current_user.tenant_id,
            action="switched to another provider",
        )
        deprovisioned_provider_agent_id = agent.provider_agent_id
        try:
            client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
            await client.delete_agent(deprovisioned_provider_agent_id)
        except SmallestAIError as exc:
            if exc.ambiguous:
                detail = (
                    "Smallest.ai archival outcome is unknown. The VAV agent kept its original "
                    "provider mapping; retry the provider switch to reconcile safely."
                )
            else:
                detail = f"Could not archive the Smallest.ai agent before switching: {exc}"
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc

        agent.provider_agent_id = None
        agent.provider_branch_id = None
        agent.provider_revision_id = None
        agent.provider_config = None
        agent.sync_status = "local_only"
        agent.last_synced_at = None

    for key, value in effective_changes.items():
        setattr(agent, key, value)

    runtime_sensitive_fields = VOICE_CONFIGURATION_FIELDS | {"speech_rate"}
    if runtime_sensitive_fields.intersection(effective_changes):
        runtime_profile = await db.scalar(
            select(AgentRuntimeProfile).where(
                AgentRuntimeProfile.agent_id == agent.id,
                AgentRuntimeProfile.tenant_id == current_user.tenant_id,
            )
        )
        if runtime_profile is not None:
            runtime_profile.enabled = False
            runtime_profile.status = "draft"
            if agent.voice_provider in {"sarvam", "elevenlabs", "inworld"}:
                runtime_profile.primary_speech_provider = agent.voice_provider
                if agent.voice_provider == "inworld" and voice_provider_changed:
                    runtime_profile.telephony_provider = "livekit_sip"
                    runtime_profile.llm_provider = "openai"
                    runtime_profile.llm_model = "gpt-4o-mini"

    if agent.provider_agent_id and SMALLEST_SYNC_FIELDS.intersection(effective_changes):
        agent.sync_status = "dirty"

    if deprovisioned_provider_agent_id:
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="agent.provider_switched",
            resource_type="agent",
            resource_id=str(agent.id),
            details={
                "from_provider": "smallest",
                "to_provider": agent.voice_provider,
                "provider_agent_id": deprovisioned_provider_agent_id,
                "provider_deprovisioned": True,
            },
        )

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
    await _require_agent_idle_for_provider_removal(
        db,
        agent=agent,
        tenant_id=current_user.tenant_id,
        action="deleted",
    )

    provider_agent_id = agent.provider_agent_id
    if provider_agent_id:
        try:
            client, _, _ = await _tenant_smallest_client(db, current_user.tenant_id)
            await client.delete_agent(provider_agent_id)
        except SmallestAIError as exc:
            if exc.ambiguous:
                detail = (
                    "Smallest.ai deletion outcome is unknown. The VAV agent was kept; "
                    "retry Delete to reconcile safely."
                )
            else:
                detail = f"Could not delete the agent from Smallest.ai: {exc}"
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="agent.deleted",
        resource_type="agent",
        resource_id=str(agent.id),
        details={
            "provider": "smallest" if provider_agent_id else None,
            "provider_agent_id": provider_agent_id,
            "provider_deprovisioned": bool(provider_agent_id),
        },
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
