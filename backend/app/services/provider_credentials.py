"""Server-only access to tenant-scoped provider credentials."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_credential import ProviderCredential
from app.services.integration_security import (
    IntegrationConfigUnavailableError,
    decrypt_integration_config,
    encrypt_integration_config,
)


class ProviderCredentialError(RuntimeError):
    pass


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
) -> dict[str, Any] | None:
    credential = await get_provider_credential(db, tenant_id, provider)
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
