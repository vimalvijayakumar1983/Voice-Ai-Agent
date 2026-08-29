"""Integration management endpoints."""

from uuid import UUID, uuid4

import json
import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.integration import Integration, WebhookEvent
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationEncryptionBackfillResponse,
    IntegrationResponse,
    IntegrationUpdate,
    WebhookDeliveryResponse,
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
logger = structlog.get_logger()

SUPPORTED_INTEGRATION_TYPES = {"webhook", "his_api", "vav_crm", "google_sheets"}
_API_AUTH_TYPES = {"bearer", "api_key"}
_API_PATH_FIELDS = ("availability_path", "create_path", "reschedule_path", "cancel_path")
_SPREADSHEET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_INVALID_SHEET_NAME_PATTERN = re.compile(r"[\\/*?\\[\\]:]")


def _require_text(config: dict, key: str, label: str, *, min_length: int = 1) -> str:
    value = config.get(key)
    if not isinstance(value, str) or len(value.strip()) < min_length:
        raise HTTPException(status_code=422, detail=f"{label} is required")
    return value.strip()


def _validate_relative_api_path(config: dict, key: str, *, required: bool = False) -> None:
    value = config.get(key)
    if value is None and not required:
        return
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise HTTPException(
            status_code=422,
            detail=f"{key} must be an absolute API path beginning with /",
        )
    if "?" in value or "#" in value:
        raise HTTPException(status_code=422, detail=f"{key} must not contain a query or fragment")
    if ".." in value.split("/"):
        raise HTTPException(status_code=422, detail=f"{key} must not contain parent traversal")
    if len(value) > 500:
        raise HTTPException(status_code=422, detail=f"{key} is too long")


def _validate_api_connector(config: dict, integration_type: str) -> None:
    _require_text(config, "base_url", "A public HTTPS base URL")
    auth_type = _require_text(config, "auth_type", "An authentication type").lower()
    if auth_type not in _API_AUTH_TYPES:
        raise HTTPException(
            status_code=422,
            detail="auth_type must be bearer or api_key",
        )
    _require_text(config, "credential", "An API credential", min_length=16)
    if auth_type == "api_key":
        header = config.get("api_key_header", "X-API-Key")
        if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
            raise HTTPException(status_code=422, detail="api_key_header is invalid")

    for key in _API_PATH_FIELDS:
        _validate_relative_api_path(
            config,
            key,
            required=key == "create_path" or (
                key == "availability_path" and integration_type == "his_api"
            ),
        )


def _validate_google_sheets_connector(config: dict) -> None:
    spreadsheet_id = _require_text(config, "spreadsheet_id", "A Google spreadsheet ID")
    if not _SPREADSHEET_ID_PATTERN.fullmatch(spreadsheet_id):
        raise HTTPException(status_code=422, detail="Google spreadsheet ID is invalid")

    sheet_name = _require_text(config, "sheet_name", "A Google sheet tab name")
    if len(sheet_name) > 100 or _INVALID_SHEET_NAME_PATTERN.search(sheet_name):
        raise HTTPException(status_code=422, detail="Google sheet tab name is invalid")

    credentials_value = config.get("credentials")
    if isinstance(credentials_value, str):
        if len(credentials_value) > 100_000:
            raise HTTPException(status_code=422, detail="Google credentials are too large")
        try:
            credentials = json.loads(credentials_value)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail="Google credentials must be valid service-account JSON",
            ) from exc
    elif isinstance(credentials_value, dict):
        credentials = credentials_value
    else:
        raise HTTPException(
            status_code=422,
            detail="Google service-account credentials are required",
        )

    required_fields = ("type", "client_email", "private_key")
    if (
        credentials.get("type") != "service_account"
        or any(
            not isinstance(credentials.get(key), str) or not credentials[key].strip()
            for key in required_fields
        )
        or "BEGIN PRIVATE KEY" not in credentials["private_key"]
    ):
        raise HTTPException(
            status_code=422,
            detail="Google credentials must be a service-account key",
        )



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
    if integration_type not in SUPPORTED_INTEGRATION_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported integration type")

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
    elif integration_type in {"his_api", "vav_crm"}:
        _validate_api_connector(config, integration_type)
    elif integration_type == "google_sheets":
        _validate_google_sheets_connector(config)


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


async def _load_webhook_integration_or_404(
    db: AsyncSession,
    integration_id: UUID,
    tenant_id: UUID,
) -> Integration:
    result = await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.tenant_id == tenant_id,
            Integration.integration_type == "webhook",
        )
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=404, detail="Webhook integration not found")
    return integration


@router.get(
    "/{integration_id}/deliveries",
    response_model=list[WebhookDeliveryResponse],
)
async def list_webhook_deliveries(
    integration_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Return a safe delivery log without webhook payloads or credentials."""
    await _load_webhook_integration_or_404(db, integration_id, current_user.tenant_id)
    result = await db.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.integration_id == integration_id,
            WebhookEvent.tenant_id == current_user.tenant_id,
        )
        .order_by(WebhookEvent.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return [WebhookDeliveryResponse.model_validate(event) for event in result.scalars().all()]


async def _enqueue_webhook_delivery(
    db: AsyncSession,
    event: WebhookEvent,
    tenant_id: UUID,
) -> None:
    from app.tasks.webhook_tasks import (
        WEBHOOK_QUEUE_PENDING,
        WEBHOOK_QUEUE_UNAVAILABLE,
        deliver_webhook_event,
    )

    # Persist an explicit outbox state before publishing. It remains until the
    # worker records an attempt outcome, so a process crash or lost publish is
    # recoverable without conflating it with Celery's transient HTTP backoff.
    event.last_error = WEBHOOK_QUEUE_PENDING
    await db.commit()
    try:
        deliver_webhook_event.delay(str(event.id), str(tenant_id))
    except Exception:
        # A publish can fail ambiguously after the worker has already consumed
        # it. Update only the untouched enqueue sentinel, never a fast worker's
        # sent/failed/transient result.
        await db.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.id == event.id,
                WebhookEvent.tenant_id == tenant_id,
                WebhookEvent.status == "pending",
                WebhookEvent.last_error == WEBHOOK_QUEUE_PENDING,
            )
            .values(last_error=WEBHOOK_QUEUE_UNAVAILABLE)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        await db.refresh(event)
        logger.warning(
            "webhook_delivery_enqueue_deferred",
            event_id=str(event.id),
            tenant_id=str(tenant_id),
        )


@router.post(
    "/{integration_id}/test",
    response_model=WebhookDeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def test_webhook_integration(
    integration_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Queue a signed, non-production test event for one active webhook."""
    integration = await _load_webhook_integration_or_404(
        db,
        integration_id,
        current_user.tenant_id,
    )
    if not integration.is_active:
        raise HTTPException(status_code=409, detail="Activate the webhook before testing it")

    event = WebhookEvent(
        id=uuid4(),
        tenant_id=current_user.tenant_id,
        integration_id=integration.id,
        event_type="integration.test",
        payload={
            "integration_id": str(integration.id),
            "message": "Voice AI webhook test",
        },
        status="pending",
    )
    db.add(event)
    await db.flush()
    await _enqueue_webhook_delivery(db, event, current_user.tenant_id)
    return WebhookDeliveryResponse.model_validate(event)


@router.post(
    "/{integration_id}/deliveries/{delivery_id}/replay",
    response_model=WebhookDeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def replay_webhook_delivery(
    integration_id: UUID,
    delivery_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Requeue one failed delivery while preserving its stable event ID."""
    integration = await _load_webhook_integration_or_404(
        db,
        integration_id,
        current_user.tenant_id,
    )
    if not integration.is_active:
        raise HTTPException(status_code=409, detail="Activate the webhook before replaying it")
    result = await db.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.id == delivery_id,
            WebhookEvent.integration_id == integration_id,
            WebhookEvent.tenant_id == current_user.tenant_id,
        )
        .with_for_update()
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    if event.status != "failed":
        raise HTTPException(
            status_code=409,
            detail="Only failed webhook deliveries can be replayed",
        )

    event.status = "pending"
    event.delivered_at = None
    await _enqueue_webhook_delivery(db, event, current_user.tenant_id)
    return WebhookDeliveryResponse.model_validate(event)
