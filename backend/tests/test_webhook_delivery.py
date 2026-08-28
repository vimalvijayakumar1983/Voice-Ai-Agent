"""Outbound webhook delivery security and reliability tests."""

import hashlib
import hmac
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.integration import Integration, WebhookEvent
from app.services.integration_security import (
    IntegrationConfigError,
    prepare_integration_config_storage,
)
from app.tasks import webhook_tasks


@pytest.fixture
def use_test_session_factory(monkeypatch, db: AsyncSession):
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(webhook_tasks, "async_session_factory", factory)
    return factory


async def _seed_delivery(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    url: str = "https://hooks.vendor.com/events",
    secret: str = "delivery-signing-secret",
) -> tuple[Integration, WebhookEvent]:
    public_config, encrypted_config = prepare_integration_config_storage(
        {
            "url": url,
            "events": ["call.completed"],
            "signing_secret": secret,
        },
        "webhook",
    )
    integration = Integration(
        tenant_id=tenant_id,
        name="Delivery webhook",
        integration_type="webhook",
        config=public_config,
        encrypted_config=encrypted_config,
    )
    db.add(integration)
    await db.flush()
    event = WebhookEvent(
        tenant_id=tenant_id,
        integration_id=integration.id,
        event_type="call.completed",
        payload={"z": "last", "call_id": "call-123", "nested": {"b": 2, "a": 1}},
        status="pending",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return integration, event


@pytest.mark.asyncio
async def test_delivery_signs_the_exact_deterministic_body(
    monkeypatch,
    db: AsyncSession,
    tenant,
    use_test_session_factory,
):
    _integration, event = await _seed_delivery(db, tenant.id)
    captured: dict = {}

    async def fake_send(url: str, body: bytes, headers: dict[str, str]) -> int:
        captured.update(url=url, body=body, headers=headers)
        return 204

    monkeypatch.setattr(webhook_tasks, "_send_request", fake_send)
    result = await webhook_tasks._attempt_webhook_delivery(str(event.id), str(tenant.id))

    assert result.outcome == "sent"
    assert captured["url"] == "https://hooks.vendor.com/events"
    assert captured["body"] == (
        b'{"data":{"call_id":"call-123","nested":{"a":1,"b":2},"z":"last"},'
        b'"event":"call.completed"}'
    )
    timestamp = captured["headers"][webhook_tasks.TIMESTAMP_HEADER]
    event_id = captured["headers"][webhook_tasks.EVENT_ID_HEADER]
    signed = timestamp.encode() + b"." + event_id.encode() + b"." + captured["body"]
    expected = hmac.new(b"delivery-signing-secret", signed, hashlib.sha256).hexdigest()
    assert captured["headers"][webhook_tasks.SIGNATURE_HEADER] == f"sha256={expected}"

    await db.refresh(event)
    assert event.status == "sent"
    assert event.attempts == 1
    assert event.last_error is None
    assert event.delivered_at is not None


@pytest.mark.asyncio
async def test_delivery_rejects_non_global_dns_answer(monkeypatch):
    monkeypatch.setattr(
        webhook_tasks.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                webhook_tasks.socket.AF_INET,
                webhook_tasks.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            )
        ],
    )

    with pytest.raises(IntegrationConfigError, match="non-public"):
        await webhook_tasks._send_request(
            "https://hooks.vendor.com/events",
            b"{}",
            {"Content-Type": "application/json"},
        )


@pytest.mark.asyncio
async def test_delivery_transport_pins_the_validated_address(monkeypatch):
    monkeypatch.setattr(
        webhook_tasks.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                webhook_tasks.socket.AF_INET,
                webhook_tasks.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ],
    )
    addresses = await webhook_tasks._resolve_public_destination("https://hooks.vendor.com/events")
    assert addresses == ("93.184.216.34",)

    captured: dict[str, object] = {}

    class FakeNetworkBackend:
        async def connect_tcp(self, host, port, **kwargs):
            captured.update(host=host, port=port, kwargs=kwargs)
            return object()

        async def sleep(self, _seconds):
            return None

    backend = webhook_tasks._PinnedNetworkBackend("hooks.vendor.com", addresses[0])
    backend._backend = FakeNetworkBackend()
    await backend.connect_tcp("hooks.vendor.com", 443, timeout=3.0)

    assert captured["host"] == "93.184.216.34"
    assert captured["port"] == 443
    with pytest.raises(OSError, match="destination_changed"):
        await backend.connect_tcp("attacker.example", 443, timeout=3.0)


@pytest.mark.asyncio
async def test_redirect_is_terminal_and_response_body_is_not_recorded(
    monkeypatch,
    db: AsyncSession,
    tenant,
    use_test_session_factory,
):
    _integration, event = await _seed_delivery(db, tenant.id)

    async def redirect_response(_url: str, _body: bytes, _headers: dict[str, str]) -> int:
        return 302

    monkeypatch.setattr(webhook_tasks, "_send_request", redirect_response)
    result = await webhook_tasks._attempt_webhook_delivery(str(event.id), str(tenant.id))

    assert result == webhook_tasks.DeliveryResult("terminal", "http_302")
    await db.refresh(event)
    assert event.status == "failed"
    assert event.attempts == 1
    assert event.last_error == "http_302"


@pytest.mark.asyncio
async def test_corrupt_envelope_fails_delivery_closed_without_network_access(
    monkeypatch,
    db: AsyncSession,
    tenant,
    use_test_session_factory,
):
    integration, event = await _seed_delivery(db, tenant.id)
    integration.encrypted_config = "fernet:v1:not-authenticated"
    await db.commit()

    async def should_not_send(*_args, **_kwargs) -> int:
        raise AssertionError("corrupt credentials must never reach the network")

    monkeypatch.setattr(webhook_tasks, "_send_request", should_not_send)
    result = await webhook_tasks._attempt_webhook_delivery(str(event.id), str(tenant.id))

    assert result == webhook_tasks.DeliveryResult("terminal", "invalid_configuration")
    await db.refresh(event)
    assert event.status == "failed"
    assert event.attempts == 1
    assert event.last_error == "invalid_configuration"


@pytest.mark.asyncio
async def test_transient_attempts_reuse_event_record_and_increment_attempts(
    monkeypatch,
    db: AsyncSession,
    tenant,
    use_test_session_factory,
):
    public_config, encrypted_config = prepare_integration_config_storage(
        {
            "url": "https://hooks.vendor.com/events",
            "events": ["call.completed"],
            "signing_secret": "idempotency-secret-value",
        },
        "webhook",
    )
    integration = Integration(
        tenant_id=tenant.id,
        name="Idempotent webhook",
        integration_type="webhook",
        config=public_config,
        encrypted_config=encrypted_config,
    )
    db.add(integration)
    await db.commit()

    first = await webhook_tasks._prepare_webhook_events(
        str(tenant.id), "call.completed", {"call_id": "same-call"}, "source-event-1"
    )
    second = await webhook_tasks._prepare_webhook_events(
        str(tenant.id), "call.completed", {"call_id": "same-call"}, "source-event-1"
    )
    assert second == first
    assert len(first) == 1

    async def unavailable(_url: str, _body: bytes, _headers: dict[str, str]) -> int:
        return 503

    monkeypatch.setattr(webhook_tasks, "_send_request", unavailable)
    first_attempt = await webhook_tasks._attempt_webhook_delivery(first[0], str(tenant.id))
    second_attempt = await webhook_tasks._attempt_webhook_delivery(first[0], str(tenant.id))
    assert first_attempt == webhook_tasks.DeliveryResult("transient", "http_503")
    assert second_attempt == first_attempt

    count = await db.scalar(select(func.count()).select_from(WebhookEvent))
    stored = await db.get(WebhookEvent, uuid.UUID(first[0]))
    assert count == 1
    assert stored is not None
    assert stored.status == "pending"
    assert stored.attempts == 2
    assert stored.last_error == "http_503"

    await webhook_tasks._mark_terminal_failure(first[0], str(tenant.id), "http_503")
    await db.refresh(stored)
    assert stored.status == "failed"
    assert stored.last_error == "http_503"


def test_transient_task_result_schedules_celery_retry(monkeypatch):
    seen: dict = {}

    def fake_run(coro):
        coro.close()
        return webhook_tasks.DeliveryResult("transient", "delivery_timeout")

    class RetryScheduledError(Exception):
        pass

    def fake_retry(*, countdown: int):
        seen["countdown"] = countdown
        raise RetryScheduledError

    monkeypatch.setattr(webhook_tasks, "_run_async", fake_run)
    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "retry", fake_retry)

    with pytest.raises(RetryScheduledError):
        webhook_tasks.deliver_webhook_event.run(str(uuid.uuid4()), str(uuid.uuid4()))

    assert seen["countdown"] == 30


def test_preparation_failure_schedules_safe_bounded_retry(monkeypatch):
    seen: dict = {}

    def failed_run(coro):
        coro.close()
        raise RuntimeError("payload-or-secret-must-not-escape")

    class RetryScheduledError(Exception):
        pass

    def fake_retry(*, exc: Exception, countdown: int):
        seen.update(exc=exc, countdown=countdown)
        raise RetryScheduledError

    monkeypatch.setattr(webhook_tasks, "_run_async", failed_run)
    monkeypatch.setattr(webhook_tasks.fire_webhook_event, "retry", fake_retry)

    with pytest.raises(RetryScheduledError):
        webhook_tasks.fire_webhook_event.run(
            str(uuid.uuid4()),
            "call.completed",
            {"secret": "payload-or-secret-must-not-escape"},
            "source-id",
        )

    assert seen["countdown"] == 30
    assert isinstance(seen["exc"], webhook_tasks.WebhookPreparationError)
    assert "payload-or-secret-must-not-escape" not in str(seen["exc"])


def test_delivery_enqueue_failure_retries_the_stable_dispatch(monkeypatch):
    seen: dict = {}

    def prepared_run(coro):
        coro.close()
        return [str(uuid.uuid4())]

    class RetryScheduledError(Exception):
        pass

    def broker_unavailable(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    def fake_retry(*, exc: Exception, countdown: int):
        seen.update(exc=exc, countdown=countdown)
        raise RetryScheduledError

    monkeypatch.setattr(webhook_tasks, "_run_async", prepared_run)
    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "delay", broker_unavailable)
    monkeypatch.setattr(webhook_tasks.fire_webhook_event, "retry", fake_retry)

    with pytest.raises(RetryScheduledError):
        webhook_tasks.fire_webhook_event.run(
            str(uuid.uuid4()),
            "call.completed",
            {"call_id": "stable-call"},
            "stable-source-id",
        )

    assert seen["countdown"] == 30
    assert isinstance(seen["exc"], webhook_tasks.WebhookPreparationError)
