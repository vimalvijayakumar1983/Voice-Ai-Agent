"""Agent builder endpoints - CRUD for AI voice agents."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase
from app.models.call import Call
from app.models.campaign import Campaign, CampaignContactAttempt
from app.providers.smallest import (
    SmallestAIError,
    get_smallest_client,
    resolve_active_knowledge_base_id,
)
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
    VoicePreviewRequest,
    validate_language_configuration,
)
from app.services.agent_catalog import (
    AGENT_TEMPLATES,
    LanguageCompatibilityStatus,
    VoiceModelPool,
    language_catalog,
    normalize_voices,
    voice_language_compatibility,
)
from app.services.agent_catalog_cache import (
    PUBLIC_CATALOG_CACHE_KEY,
    public_agent_catalog_cache,
)
from app.services.audit import record_audit_event
from app.services.rate_limit import enforce_rate_limit

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
    {"completed", "failed", "busy", "no_answer", "canceled", "cancelled"}
)
TERMINAL_CAMPAIGN_ATTEMPT_STATES = frozenset({"completed", "failed", "rejected", "cancelled"})


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


async def _require_public_voice(
    client,
    voice_id: str,
    selected_languages: list[str],
) -> VoiceResolution:
    """Resolve one public voice proven to support every selected language.

    A blank local voice is an app-managed platform default. It is resolved to
    an explicit compatible provider voice so clearing a previous custom voice
    cannot leave stale synthesizer configuration in Smallest's merged draft.
    """
    try:
        normalized_voices = await _public_voice_catalog(client)
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    by_id = {str(voice["id"]): voice for voice in normalized_voices}
    selected_voice = by_id.get(voice_id) if voice_id else None
    if voice_id and selected_voice is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Voice is not available in the shared public catalog. "
                "Private voice clones require a tenant-owned entitlement."
            ),
        )

    if not voice_id:
        candidates = [
            voice
            for voice in normalized_voices
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


def _operator_verified_knowledge_tool_ref_delta(
    original_provider_agent: dict,
    published_provider_agent: dict,
    active_provider_agent: dict,
    *,
    expected_knowledge_base_id: str | None,
) -> bool:
    """Accept one operator-created KB tool-ref delta when Smallest omits expansion.

    Smallest's current agent response can expose the live Knowledge Base system tool
    only through _resolvedConfig.toolRefs while omitting both the expanded
    knowledge_base_search tool and the documented top-level KB field. This fallback
    is intentionally limited to configuration-recovery verification: published and
    active refs must agree, every other field is checked separately, and exactly one
    ref must have been added or removed from the failed revision.
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
            (
                configuration_mismatches,
                published_configuration_mismatches,
                active_configuration_mismatches,
            ) = _provider_configuration_mismatch_sets(
                published_provider_agent,
                active_provider_agent,
                expected_configuration,
            )

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
                if _operator_verified_knowledge_tool_ref_delta(
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

    remote_knowledge_id = None
    knowledge_binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.agent_id == agent.id,
            AgentKnowledgeBinding.tenant_id == tenant_id,
        )
    )
    if knowledge_binding:
        remote_knowledge_id = await db.scalar(
            select(KnowledgeBase.provider_knowledge_base_id).where(
                KnowledgeBase.id == knowledge_binding.knowledge_base_id,
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.approval_status == "approved",
            )
        )
        if not remote_knowledge_id:
            raise HTTPException(
                status_code=409,
                detail="Bound knowledge must be approved and provisioned before publishing",
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
        existing_remote_knowledge_id = await client.get_agent_knowledge_base_id(
            provider_agent_id
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
        await _require_public_voice(
            get_smallest_client(),
            data.voice_id,
            list(data.supported_languages),
        )
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
        voices = await _public_voice_catalog(client)
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Could not load the Smallest.ai voice catalog"
        ) from exc

    # The provider account is shared across tenants. Private clones must not be
    # listed or selectable until a tenant-owned entitlement mapping exists.
    return AgentProviderCatalog(
        voices=voices,
        languages=language_catalog(voices),
        templates=AGENT_TEMPLATES,
        field_capabilities=PROVIDER_FIELD_CAPABILITIES,
    )


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
    client = get_smallest_client()
    try:
        voices = await _public_voice_catalog(client)
    except SmallestAIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    voice = next((item for item in voices if item.get("id") == data.voice_id), None)
    if voice is None:
        raise HTTPException(status_code=422, detail="Voice is not in the public catalog")
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
    voice_selection = _voice_configuration_snapshot(agent)
    # Never retain a row lock while waiting on the provider catalog. A second
    # provisioner can race here, so re-lock and revalidate before recording the
    # durable create lease.
    await db.commit()
    voice = await _require_public_voice(
        client,
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
    voice = await _require_public_voice(
        client,
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
    if agent.sync_status != "synced" or not agent.provider_revision_id or not agent.last_synced_at:
        raise HTTPException(
            status_code=409,
            detail="Agent cannot take calls until its current provider revision is fully synced",
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
    resulting_voice_id = effective_changes.get("voice_id", agent.voice_id)
    voice_inputs_changed = bool(VOICE_PREFLIGHT_FIELDS.intersection(effective_changes))
    if resulting_voice_id and voice_inputs_changed:
        baseline_fields = set(effective_changes) | VOICE_CONFIGURATION_FIELDS
        baseline = _agent_fields_snapshot(agent, baseline_fields)
        await db.rollback()
        await _require_public_voice(
            get_smallest_client(),
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

    for key, value in effective_changes.items():
        setattr(agent, key, value)

    if agent.provider_agent_id and SMALLEST_SYNC_FIELDS.intersection(effective_changes):
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
