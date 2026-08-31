"""Reliable, signed delivery for tenant webhook integrations.

Receivers can authenticate a request using these headers:

* ``X-VoiceAI-Event-ID``: the stable UUID for this delivery.
* ``X-VoiceAI-Timestamp``: Unix seconds when the request was created.
* ``X-VoiceAI-Signature``: ``sha256=<hex>`` where the digest is HMAC-SHA256
  over ``<timestamp>.<event-id>.<exact request body>``.

The JSON body is serialized deterministically and sent as bytes so receivers
verify the same payload that was signed. A receiver should also reject stale
timestamps and retain event IDs to make its own processing idempotent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpcore
import httpx
import structlog
from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.integration import Integration, WebhookEvent
from app.services.integration_security import (
    IntegrationConfigError,
    IntegrationConfigUnavailableError,
    load_integration_config,
    validate_public_https_url,
)
from app.tasks.async_runner import run_async as _run_async
from app.tasks.worker import celery_app

logger = structlog.get_logger()

EVENT_ID_HEADER = "X-VoiceAI-Event-ID"
TIMESTAMP_HEADER = "X-VoiceAI-Timestamp"
SIGNATURE_HEADER = "X-VoiceAI-Signature"
_DELIVERY_NAMESPACE = uuid.UUID("d92757e4-8fd7-4da1-aa76-a5bb6de5286f")
_RETRYABLE_HTTP_STATUSES = {408, 425, 429}
_HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
_DNS_TIMEOUT_SECONDS = 3.0
PENDING_DELIVERY_RECOVERY_AFTER_SECONDS = 120
PENDING_DELIVERY_RECOVERY_BATCH_SIZE = 200
WEBHOOK_QUEUE_PENDING = "queue_pending"
WEBHOOK_QUEUE_UNAVAILABLE = "queue_unavailable"
WEBHOOK_RECOVERABLE_QUEUE_STATES = (WEBHOOK_QUEUE_PENDING, WEBHOOK_QUEUE_UNAVAILABLE)


@dataclass(frozen=True)
class DeliveryResult:
    """A safe, bounded result returned by one delivery attempt."""

    outcome: str
    error_code: str | None = None


class WebhookPreparationError(RuntimeError):
    """Safe error raised when matching deliveries cannot be persisted."""


def _deterministic_body(event_type: str, payload: dict) -> bytes:
    return json.dumps(
        {"data": payload, "event": event_type},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _delivery_headers(
    *,
    event_id: uuid.UUID,
    body: bytes,
    signing_secret: str | None,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_value = str(
        timestamp if timestamp is not None else int(datetime.now(UTC).timestamp())
    )
    event_id_value = str(event_id)
    headers = {
        "Content-Type": "application/json",
        EVENT_ID_HEADER: event_id_value,
        TIMESTAMP_HEADER: timestamp_value,
    }
    if signing_secret:
        signed_payload = (
            timestamp_value.encode("ascii") + b"." + event_id_value.encode("ascii") + b"." + body
        )
        signature = hmac.new(
            signing_secret.encode("utf-8"), signed_payload, hashlib.sha256
        ).hexdigest()
        headers[SIGNATURE_HEADER] = f"sha256={signature}"
    return headers


def _canonical_hostname(hostname: str) -> str:
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to the public address validated for this delivery.

    HTTP Core still receives the original URL hostname, so TLS verification and
    SNI use the tenant-configured host while the TCP connection cannot perform a
    second, attacker-controlled DNS lookup.
    """

    def __init__(self, hostname: str, address: str):
        self._hostname = _canonical_hostname(hostname)
        self._address = address
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if _canonical_hostname(host) != self._hostname:
            raise OSError("webhook_destination_changed")
        return await self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        raise OSError("webhook_unix_socket_forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport backed by one previously validated destination IP."""

    def __init__(self, hostname: str, address: str):
        super().__init__(
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        # HTTPX 0.27-0.28 uses HTTP Core 1.x; both ranges are constrained in
        # pyproject.toml because this security boundary intentionally replaces
        # the pool's resolver immediately after construction.
        self._pool._network_backend = _PinnedNetworkBackend(hostname, address)


async def _resolve_public_destination(webhook_url: str) -> tuple[str, ...]:
    """Resolve immediately before delivery and reject every non-global answer.

    Rejecting a hostname when *any* answer is non-public prevents mixed public /
    private DNS sets from becoming an SSRF bypass. Resolution is repeated for
    every attempt to narrow the DNS-rebinding window.
    """
    parsed = urlsplit(webhook_url)
    hostname = parsed.hostname
    if hostname is None:  # Defensive; validate_public_https_url checks this too.
        raise IntegrationConfigError("Integration URL must include a host")
    port = parsed.port or 443

    try:
        answers = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            ),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise TimeoutError("webhook_dns_resolution_timeout") from exc
    except socket.gaierror as exc:
        raise OSError("webhook_dns_resolution_failed") from exc

    addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
    if not addresses:
        raise OSError("webhook_dns_resolution_failed")
    try:
        parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
    except ValueError as exc:
        raise IntegrationConfigError("Webhook host returned an invalid address") from exc
    if any(not address.is_global for address in parsed_addresses):
        raise IntegrationConfigError("Webhook host resolved to a non-public address")
    # Prefer IPv4 where available because some worker networks do not expose an
    # IPv6 route. A retry resolves and validates afresh.
    return tuple(
        str(address)
        for address in sorted(parsed_addresses, key=lambda item: (item.version, str(item)))
    )


async def _send_request(webhook_url: str, body: bytes, headers: dict[str, str]) -> int:
    """Send to a validated, pinned IP without redirects or body buffering."""
    parsed = urlsplit(webhook_url)
    hostname = parsed.hostname
    if hostname is None:
        raise IntegrationConfigError("Integration URL must include a host")
    addresses = await _resolve_public_destination(webhook_url)
    transport = _PinnedAsyncHTTPTransport(hostname, addresses[0])
    async with httpx.AsyncClient(
        transport=transport,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        async with client.stream("POST", webhook_url, content=body, headers=headers) as response:
            # Streaming and closing without reading bounds an untrusted response to its headers.
            return response.status_code


def _safe_http_error(status_code: int) -> str:
    return f"http_{status_code}"


async def _prepare_webhook_events(
    tenant_id: str,
    event_type: str,
    payload: dict,
    source_event_id: str,
) -> list[str]:
    """Create one stable delivery record per subscribed tenant integration.

    The stable primary key makes redispatch of the same Celery task idempotent;
    retries reuse existing records instead of inserting duplicates.
    """
    tenant_uuid = uuid.UUID(tenant_id)
    delivery_ids: list[str] = []

    async with async_session_factory() as db:
        result = await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_uuid,
                Integration.integration_type == "webhook",
                Integration.is_active.is_(True),
            )
        )
        integrations = result.scalars().all()

        for integration in integrations:
            try:
                config = load_integration_config(integration.config, integration.encrypted_config)
            except IntegrationConfigUnavailableError:
                # One damaged tenant credential must neither leak nor prevent
                # healthy integrations from receiving the same event.
                logger.error(
                    "integration_config_unavailable",
                    integration_id=str(integration.id),
                )
                continue
            subscribed_events = config.get("events", [])
            if event_type not in subscribed_events and "*" not in subscribed_events:
                continue
            if not config.get("url"):
                continue

            delivery_id = uuid.uuid5(
                _DELIVERY_NAMESPACE,
                f"{tenant_uuid}:{source_event_id}:{integration.id}",
            )
            existing = await db.get(WebhookEvent, delivery_id)
            if existing is None:
                db.add(
                    WebhookEvent(
                        id=delivery_id,
                        tenant_id=tenant_uuid,
                        integration_id=integration.id,
                        event_type=event_type,
                        payload=payload,
                        status="pending",
                        last_error=WEBHOOK_QUEUE_PENDING,
                    )
                )
            elif existing.tenant_id != tenant_uuid or existing.integration_id != integration.id:
                # A UUIDv5 collision is practically impossible, but never cross tenant bounds.
                logger.error("webhook_event_identity_collision", event_id=str(delivery_id))
                continue
            elif (
                existing.status != "pending"
                or existing.last_error not in WEBHOOK_RECOVERABLE_QUEUE_STATES
            ):
                # Redispatch only an event whose durable enqueue is still
                # outstanding. HTTP backoff and terminal states own themselves.
                continue
            delivery_ids.append(str(delivery_id))

        await db.commit()
    return delivery_ids


async def _attempt_webhook_delivery(event_id: str, tenant_id: str) -> DeliveryResult:
    """Attempt one delivery and persist the attempt without exposing unsafe data."""
    try:
        event_uuid = uuid.UUID(event_id)
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        return DeliveryResult("terminal", "invalid_delivery_identity")

    async with async_session_factory() as db:
        result = await db.execute(
            select(WebhookEvent)
            .where(
                WebhookEvent.id == event_uuid,
                WebhookEvent.tenant_id == tenant_uuid,
            )
            .with_for_update()
        )
        event = result.scalar_one_or_none()
        if event is None:
            return DeliveryResult("missing", "delivery_not_found")
        if event.status == "sent":
            return DeliveryResult("sent")
        if event.status != "pending":
            # Failed is terminal until an operator explicitly replays it by
            # moving the same stable event ID back to pending. This makes late
            # or duplicate Celery messages harmless.
            return DeliveryResult("terminal", "delivery_not_pending")

        integration_result = await db.execute(
            select(Integration).where(
                Integration.id == event.integration_id,
                Integration.tenant_id == tenant_uuid,
                Integration.integration_type == "webhook",
                Integration.is_active.is_(True),
            )
        )
        integration = integration_result.scalar_one_or_none()
        if integration is None:
            event.attempts += 1
            event.status = "failed"
            event.last_error = "integration_unavailable"
            await db.commit()
            return DeliveryResult("terminal", "integration_unavailable")

        event.attempts += 1
        try:
            config = load_integration_config(integration.config, integration.encrypted_config)
        except IntegrationConfigUnavailableError:
            event.status = "failed"
            event.last_error = "invalid_configuration"
            await db.commit()
            return DeliveryResult("terminal", "invalid_configuration")

        webhook_url = config.get("url")
        signing_secret = config.get("signing_secret")

        if not isinstance(webhook_url, str):
            event.status = "failed"
            event.last_error = "invalid_destination"
            await db.commit()
            return DeliveryResult("terminal", "invalid_destination")
        if not isinstance(signing_secret, str) or len(signing_secret) < 16:
            event.status = "failed"
            event.last_error = "invalid_signing_secret"
            await db.commit()
            return DeliveryResult("terminal", "invalid_signing_secret")

        body = _deterministic_body(event.event_type, event.payload)
        headers = _delivery_headers(
            event_id=event.id,
            body=body,
            signing_secret=signing_secret,
        )

        try:
            # Re-run stored configuration validation immediately before each request.
            validate_public_https_url(webhook_url)
            status_code = await _send_request(webhook_url, body, headers)
        except IntegrationConfigError:
            outcome = DeliveryResult("terminal", "unsafe_destination")
        except (httpx.TimeoutException, TimeoutError):
            outcome = DeliveryResult("transient", "delivery_timeout")
        except (httpx.TransportError, OSError):
            outcome = DeliveryResult("transient", "transport_failure")
        except Exception:  # A delivery task must record unexpected terminal failures safely.
            outcome = DeliveryResult("terminal", "delivery_internal_error")
        else:
            if 200 <= status_code < 300:
                outcome = DeliveryResult("sent")
            elif status_code in _RETRYABLE_HTTP_STATUSES or status_code >= 500:
                outcome = DeliveryResult("transient", _safe_http_error(status_code))
            else:
                # Redirects are deliberately terminal: we never trust an unvalidated Location.
                outcome = DeliveryResult("terminal", _safe_http_error(status_code))

        if outcome.outcome == "sent":
            event.status = "sent"
            event.delivered_at = datetime.now(UTC)
            event.last_error = None
        elif outcome.outcome == "terminal":
            event.status = "failed"
            event.last_error = outcome.error_code
        else:
            event.status = "pending"
            event.last_error = outcome.error_code
        await db.commit()
        return outcome


async def _mark_terminal_failure(event_id: str, tenant_id: str, error_code: str) -> None:
    try:
        event_uuid = uuid.UUID(event_id)
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        return
    async with async_session_factory() as db:
        result = await db.execute(
            select(WebhookEvent).where(
                WebhookEvent.id == event_uuid,
                WebhookEvent.tenant_id == tenant_uuid,
            )
        )
        event = result.scalar_one_or_none()
        if event is not None and event.status != "sent":
            event.status = "failed"
            event.last_error = error_code
            await db.commit()


async def _claim_stale_pending_webhook_deliveries(
    *,
    now: datetime | None = None,
    limit: int = PENDING_DELIVERY_RECOVERY_BATCH_SIZE,
) -> list[tuple[str, str]]:
    """Claim durable deliveries whose original enqueue or retry was lost.

    Updating ``updated_at`` before enqueue creates a bounded recovery lease.
    A crash after this commit is safe: the same stable event ID becomes
    eligible again after the lease, and delivery itself is idempotent.
    """
    current_time = now or datetime.now(UTC)
    stale_before = current_time - timedelta(seconds=PENDING_DELIVERY_RECOVERY_AFTER_SECONDS)

    async with async_session_factory() as db:
        result = await db.execute(
            select(WebhookEvent)
            .where(
                WebhookEvent.status == "pending",
                # These explicit states identify an outstanding initial or
                # replay enqueue. Transient delivery errors use other codes and
                # remain exclusively governed by Celery's bounded backoff.
                WebhookEvent.last_error.in_(WEBHOOK_RECOVERABLE_QUEUE_STATES),
                WebhookEvent.updated_at <= stale_before,
            )
            .order_by(WebhookEvent.updated_at.asc(), WebhookEvent.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = result.scalars().all()
        claimed = []
        for event in events:
            event.updated_at = current_time
            claimed.append((str(event.id), str(event.tenant_id)))
        await db.commit()
    return claimed


@celery_app.task(name="app.tasks.webhook_tasks.sweep_pending_webhook_deliveries")
def sweep_pending_webhook_deliveries():
    """Re-enqueue stale pending deliveries from the database-backed outbox."""
    claimed = _run_async(_claim_stale_pending_webhook_deliveries())
    queued = 0
    for event_id, tenant_id in claimed:
        try:
            deliver_webhook_event.delay(event_id, tenant_id)
        except Exception as exc:
            # The row remains pending and is claimed again after the recovery
            # lease. Never log broker details that could include credentials.
            logger.warning(
                "webhook_delivery_recovery_enqueue_failed",
                event_id=event_id,
                error_type=type(exc).__name__,
            )
        else:
            queued += 1
    return queued


@celery_app.task(
    name="app.tasks.webhook_tasks.deliver_webhook_event",
    bind=True,
    max_retries=5,
    default_retry_delay=30,
)
def deliver_webhook_event(self, event_id: str, tenant_id: str):
    """Deliver one persisted event; Celery retries only transient failures."""
    result = _run_async(_attempt_webhook_delivery(event_id, tenant_id))
    if result.outcome != "transient":
        return result.outcome

    if self.request.retries >= self.max_retries:
        _run_async(
            _mark_terminal_failure(
                event_id,
                tenant_id,
                result.error_code or "retry_limit_exhausted",
            )
        )
        logger.error(
            "webhook_delivery_exhausted",
            event_id=event_id,
            error_code=result.error_code,
        )
        return "failed"

    countdown = min(self.default_retry_delay * (2**self.request.retries), 15 * 60)
    logger.warning(
        "webhook_delivery_retry_scheduled",
        event_id=event_id,
        error_code=result.error_code,
        retry_number=self.request.retries + 1,
    )
    raise self.retry(countdown=countdown)


@celery_app.task(
    name="app.tasks.webhook_tasks.fire_webhook_event",
    bind=True,
    max_retries=5,
    default_retry_delay=30,
)
def fire_webhook_event(
    self,
    tenant_id: str,
    event_type: str,
    payload: dict,
    source_event_id: str | None = None,
):
    """Persist matching deliveries and enqueue one idempotent task per integration."""
    stable_source_id = source_event_id or self.request.id or str(uuid.uuid4())
    try:
        event_ids = _run_async(
            _prepare_webhook_events(tenant_id, event_type, payload, stable_source_id)
        )
        for event_id in event_ids:
            deliver_webhook_event.delay(event_id, tenant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "webhook_dispatch_exhausted",
                error_type=type(exc).__name__,
            )
            raise WebhookPreparationError("webhook_event_dispatch_failed") from None
        countdown = min(self.default_retry_delay * (2**self.request.retries), 15 * 60)
        logger.warning(
            "webhook_dispatch_retry_scheduled",
            error_type=type(exc).__name__,
            retry_number=self.request.retries + 1,
        )
        raise self.retry(
            exc=WebhookPreparationError("webhook_event_dispatch_failed"),
            countdown=countdown,
        ) from None
    return len(event_ids)
