"""Server-only access to tenant-scoped provider credentials."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.provider_credential import ProviderCredential
from app.services.integration_security import (
    IntegrationConfigUnavailableError,
    decrypt_integration_config,
    encrypt_integration_config,
)


class ProviderCredentialError(RuntimeError):
    pass


async def lock_provider_runtime_boundaries(
    db: AsyncSession,
    tenant_id: UUID,
    providers: str | Iterable[str],
) -> None:
    """Serialize credential mutation with runtime admission for a tenant.

    Callers must acquire these transaction locks, in sorted provider order,
    before any agent-runtime or credential-row lock.  That single global order
    prevents credential rotation from racing a profile activation and avoids
    the credential-row/runtime-lock inversion that can deadlock PostgreSQL.

    SQLite has no transaction advisory locks.  Production uses PostgreSQL;
    SQLite tests exercise the surrounding snapshot and fail-closed state
    transitions without pretending to provide cross-process serialization.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    provider_names = {providers} if isinstance(providers, str) else set(providers)
    for provider in sorted(str(item).strip() for item in provider_names if str(item).strip()):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"runtime-provider:{tenant_id}:{provider}"},
        )


def runtime_profile_depends_on_credential(
    profile: AgentRuntimeProfile,
    provider: str,
) -> bool:
    """Return whether rotating a credential invalidates an active runtime."""
    if provider == "twilio":
        return profile.telephony_provider == "twilio"
    if provider == "livekit_sip":
        return profile.telephony_provider == "livekit_sip"
    if provider == "sarvam":
        # ElevenLabs is TTS-only in the native Twilio pipeline; Sarvam remains
        # its realtime transcription dependency.
        return profile.primary_speech_provider in {"sarvam", "elevenlabs"} or (
            profile.fallback_speech_provider == "sarvam"
        )
    if provider == "elevenlabs":
        return profile.primary_speech_provider == "elevenlabs" or (
            profile.fallback_speech_provider == "elevenlabs"
        )
    if provider == "inworld":
        return profile.primary_speech_provider == "inworld" or (
            profile.fallback_speech_provider == "inworld" or profile.llm_provider == "inworld"
        )
    if provider == "openai":
        return profile.llm_provider == "openai"
    return False


async def invalidate_active_runtimes_for_credential(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
) -> list[str]:
    """Lock and fail closed every active runtime that consumes ``provider``.

    The provider boundary is always acquired here so legacy credential APIs
    cannot accidentally bypass activation serialization. Credential rows must
    only be locked or written *after* this helper returns.
    """
    await lock_provider_runtime_boundaries(db, tenant_id, provider)
    candidate_ids = sorted(
        str(agent_id)
        for agent_id in (
            await db.scalars(
                select(AgentRuntimeProfile.agent_id).where(
                    AgentRuntimeProfile.tenant_id == tenant_id,
                    AgentRuntimeProfile.enabled.is_(True),
                    AgentRuntimeProfile.status == "active",
                )
            )
        ).all()
    )
    invalidated: list[str] = []
    for candidate_id in candidate_ids:
        candidate_uuid = UUID(candidate_id)
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"agent-runtime:{tenant_id}:{candidate_uuid}"},
            )
        # Lock Agent first to match the runtime mutation boundary used by
        # update, test, activation, and explicit deactivation.
        agent = await db.scalar(
            select(Agent)
            .where(Agent.id == candidate_uuid, Agent.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        profile = await db.scalar(
            select(AgentRuntimeProfile)
            .where(
                AgentRuntimeProfile.agent_id == candidate_uuid,
                AgentRuntimeProfile.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            agent is not None
            and profile is not None
            and profile.enabled
            and profile.status == "active"
            and runtime_profile_depends_on_credential(profile, provider)
        ):
            profile.enabled = False
            profile.status = "draft"
            invalidated.append(candidate_id)
    return invalidated


async def lock_provider_cleanup_boundary(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
) -> None:
    """Serialize provider credentials with durable remote-cleanup intents.

    Credential rotation/deletion and every cleanup reservation use the same
    transaction-scoped lock.  This closes the empty-result race where a
    credential could disappear immediately before a cleanup row is inserted.
    SQLite test databases do not offer advisory locks; their single-process
    transactions still exercise the surrounding state machine.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = f"provider-cleanup:{tenant_id}:{provider}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


async def get_provider_credential(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
    *,
    for_update: bool = False,
) -> ProviderCredential | None:
    query = select(ProviderCredential).where(
        ProviderCredential.tenant_id == tenant_id,
        ProviderCredential.provider == provider,
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def load_provider_config(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    credential = await get_provider_credential(
        db,
        tenant_id,
        provider,
        for_update=for_update,
    )
    if credential is None or not credential.is_active:
        return None
    try:
        return decrypt_integration_config(credential.encrypted_config)
    except IntegrationConfigUnavailableError as exc:
        raise ProviderCredentialError(f"{provider} credential is unavailable") from exc


async def store_provider_config(
    db: AsyncSession,
    tenant_id: UUID,
    provider: str,
    config: dict[str, Any],
) -> ProviderCredential:
    try:
        encrypted = encrypt_integration_config(config)
    except IntegrationConfigUnavailableError as exc:
        raise ProviderCredentialError("Credential encryption is unavailable") from exc
    credential = await get_provider_credential(db, tenant_id, provider, for_update=True)
    if credential is None:
        credential = ProviderCredential(
            tenant_id=tenant_id,
            provider=provider,
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
    return credential
