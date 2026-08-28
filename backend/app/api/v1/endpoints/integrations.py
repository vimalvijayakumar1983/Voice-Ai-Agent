"""Integration management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.integration import Integration
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationEncryptionBackfillResponse,
    IntegrationResponse,
    IntegrationUpdate,
)
from app.services.integration_security import (
    INTEGRATION_CONFIG_STORAGE_VERSION,
    SUPPORTED_WEBHOOK_EVENTS,
    IntegrationConfigError,
    IntegrationConfigUnavailableError,
    backfill_legacy_integration_configs,
    clear_integration_secrets,
    load_integration_config,
    merge_integration_config,
    prepare_integration_config_storage,
    validate_integration_config_urls,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])


def _locked_tenant_integration_statement(integration_id: UUID, tenant_id: UUID):
    """Build the mutation lookup that serializes one tenant-owned integration.

    ``populate_existing`` is intentional: even if a request-scoped session has
    already observed the row, the post-lock database state remains authoritative
    when merging write-only credentials.
    """
    return (
        select(Integration)
        .where(
            Integration.id == integration_id,
            Integration.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _load_config_or_fail(integration: Integration) -> dict:
    try:
        return load_integration_config(integration.config, integration.encrypted_config)
    except IntegrationConfigUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Integration configuration is unavailable",
        ) from exc


def _store_config_or_fail(integration: Integration, config: dict) -> None:
    try:
        public_config, encrypted_config = prepare_integration_config_storage(
            config,
            integration.integration_type,
        )
    except IntegrationConfigUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Integration configuration is unavailable",
        ) from exc
    integration.config = public_config
    integration.encrypted_config = encrypted_config
    integration.config_encryption_version = INTEGRATION_CONFIG_STORAGE_VERSION


def _integration_response(integration: Integration, config: dict) -> IntegrationResponse:
    # Build from the hydrated config so the response can report which write-only
    # fields exist, then let the schema enforce redaction before serialization.
    return IntegrationResponse(
        id=integration.id,
        tenant_id=integration.tenant_id,
        name=integration.name,
        integration_type=integration.integration_type,
        config=config,
        is_active=integration.is_active,
    )


def _validate_config(config: dict, integration_type: str) -> None:
    try:
        validate_integration_config_urls(config)
    except IntegrationConfigError as exc:
        # Do not include the submitted config in validation responses; it can
        # contain credentials that are intentionally write-only.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if integration_type == "webhook":
        if not isinstance(config.get("url"), str):
            raise HTTPException(status_code=422, detail="Webhook integrations require a URL")
        signing_secret = config.get("signing_secret")
        if not isinstance(signing_secret, str) or len(signing_secret) < 16:
            raise HTTPException(
                status_code=422,
                detail="Webhook integrations require a signing secret of at least 16 characters",
            )
        events = config.get("events", [])
        if not isinstance(events, list) or any(
            not isinstance(event, str) or event not in SUPPORTED_WEBHOOK_EVENTS for event in events
        ):
            raise HTTPException(
                status_code=422,
                detail="Webhook integrations contain an unsupported event type",
            )


@router.post(
    "/encryption/backfill",
    response_model=IntegrationEncryptionBackfillResponse,
)
async def backfill_integration_encryption(
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Encrypt one tenant-scoped batch; safe to repeat until remaining is zero."""
    try:
        migrated, remaining = await backfill_legacy_integration_configs(
            db,
            tenant_id=current_user.tenant_id,
        )
    except IntegrationConfigUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Integration configuration is unavailable",
        ) from exc
    return IntegrationEncryptionBackfillResponse(migrated=migrated, remaining=remaining)


@router.get("", response_model=list[IntegrationResponse])
async def list_integrations(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Integration).where(Integration.tenant_id == current_user.tenant_id)
    )
    integrations = result.scalars().all()
    return [
        _integration_response(integration, _load_config_or_fail(integration))
        for integration in integrations
    ]


@router.post("", response_model=IntegrationResponse, status_code=status.HTTP_201_CREATED)
async def create_integration(
    data: IntegrationCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    config = merge_integration_config({}, data.config)
    integration_type = data.integration_type.strip().lower()
    _validate_config(config, integration_type)
    integration = Integration(
        tenant_id=current_user.tenant_id,
        name=data.name.strip(),
        integration_type=integration_type,
        config={},
    )
    _store_config_or_fail(integration, config)
    db.add(integration)
    await db.flush()
    return _integration_response(integration, config)


@router.patch("/{integration_id}", response_model=IntegrationResponse)
async def update_integration(
    integration_id: UUID,
    data: IntegrationUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        _locked_tenant_integration_statement(integration_id, current_user.tenant_id)
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    config = _load_config_or_fail(integration)

    updates = data.model_dump(exclude_unset=True, exclude={"config", "clear_secrets"})
    if "name" in updates:
        updates["name"] = updates["name"].strip()
    for key, value in updates.items():
        setattr(integration, key, value)

    if data.config is not None or data.clear_secrets:
        config = merge_integration_config(config, data.config)
        try:
            config = clear_integration_secrets(config, data.clear_secrets)
        except IntegrationConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _validate_config(config, integration.integration_type)

    # Legacy plaintext rows are sanitized and envelope-encrypted on their first
    # mutation, including name/status-only changes.
    if (
        data.config is not None
        or data.clear_secrets
        or integration.encrypted_config is None
        or integration.config_encryption_version != INTEGRATION_CONFIG_STORAGE_VERSION
    ):
        _validate_config(config, integration.integration_type)
        _store_config_or_fail(integration, config)

    await db.flush()
    return _integration_response(integration, config)


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    integration_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        _locked_tenant_integration_statement(integration_id, current_user.tenant_id)
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")
    await db.delete(integration)
