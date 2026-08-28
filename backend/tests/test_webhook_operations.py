"""Operator-facing webhook delivery log, test, and replay endpoints."""

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.api.v1.endpoints import integrations as integrations_endpoint
from app.models.integration import Integration, WebhookEvent
from app.tasks import webhook_tasks


@pytest.fixture
async def webhook_integration(db, tenant):
    integration = Integration(
        tenant_id=tenant.id,
        name="Operations webhook",
        integration_type="webhook",
        config={"url": "https://example.com/hook", "events": ["call.completed"]},
        encrypted_config="test-envelope",
        config_encryption_version=1,
        is_active=True,
    )
    db.add(integration)
    await db.commit()
    return integration


@pytest.mark.asyncio
async def test_delivery_log_is_tenant_scoped_and_omits_payload(
    client,
    auth_headers,
    db,
    tenant,
    webhook_integration,
):
    event = WebhookEvent(
        tenant_id=tenant.id,
        integration_id=webhook_integration.id,
        event_type="call.completed",
        payload={"private": "must-not-be-returned"},
        status="failed",
        attempts=3,
        last_error="http_500",
    )
    db.add(event)
    await db.commit()

    response = await client.get(
        f"/api/v1/integrations/{webhook_integration.id}/deliveries",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] == str(event.id)
    assert response.json()[0]["last_error"] == "http_500"
    assert "payload" not in response.json()[0]


@pytest.mark.asyncio
async def test_webhook_test_event_is_persisted_and_queued(
    client,
    auth_headers,
    db,
    webhook_integration,
    monkeypatch,
):
    delay = Mock()
    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "delay", delay)

    response = await client.post(
        f"/api/v1/integrations/{webhook_integration.id}/test",
        headers=auth_headers,
    )

    assert response.status_code == 202
    assert response.json()["event_type"] == "integration.test"
    assert response.json()["status"] == "pending"
    assert response.json()["last_error"] == "queue_pending"
    event = await db.get(WebhookEvent, UUID(response.json()["id"]))
    assert event is not None
    delay.assert_called_once_with(str(event.id), str(webhook_integration.tenant_id))


@pytest.mark.asyncio
async def test_failed_webhook_delivery_can_be_replayed_once(
    client,
    auth_headers,
    db,
    tenant,
    webhook_integration,
    monkeypatch,
):
    event = WebhookEvent(
        tenant_id=tenant.id,
        integration_id=webhook_integration.id,
        event_type="call.completed",
        payload={"call_id": "safe-id"},
        status="failed",
        attempts=5,
        last_error="delivery_timeout",
    )
    db.add(event)
    await db.commit()
    delay = Mock()
    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "delay", delay)

    response = await client.post(
        f"/api/v1/integrations/{webhook_integration.id}/deliveries/{event.id}/replay",
        headers=auth_headers,
    )

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["last_error"] == "queue_pending"
    assert response.json()["attempts"] == 5
    delay.assert_called_once_with(str(event.id), str(tenant.id))

    duplicate = await client.post(
        f"/api/v1/integrations/{webhook_integration.id}/deliveries/{event.id}/replay",
        headers=auth_headers,
    )
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_inactive_webhook_delivery_cannot_be_replayed(
    client,
    auth_headers,
    db,
    tenant,
    webhook_integration,
    monkeypatch,
):
    webhook_integration.is_active = False
    event = WebhookEvent(
        tenant_id=tenant.id,
        integration_id=webhook_integration.id,
        event_type="call.completed",
        payload={"call_id": "safe-id"},
        status="failed",
        attempts=1,
        last_error="http_500",
    )
    db.add(event)
    await db.commit()
    delay = Mock()
    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "delay", delay)

    response = await client.post(
        f"/api/v1/integrations/{webhook_integration.id}/deliveries/{event.id}/replay",
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Activate the webhook before replaying it"
    await db.refresh(event)
    assert event.status == "failed"
    assert event.last_error == "http_500"
    delay.assert_not_called()


@pytest.mark.asyncio
async def test_queue_failure_leaves_test_delivery_pending_for_durable_recovery(
    client,
    auth_headers,
    db,
    webhook_integration,
    monkeypatch,
):
    delay = Mock(side_effect=ConnectionError("broker unavailable"))
    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "delay", delay)

    response = await client.post(
        f"/api/v1/integrations/{webhook_integration.id}/test",
        headers=auth_headers,
    )

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["last_error"] == "queue_unavailable"
    event = await db.get(WebhookEvent, UUID(response.json()["id"]))
    assert event is not None
    assert event.status == "pending"
    assert event.last_error == "queue_unavailable"


@pytest.mark.asyncio
async def test_ambiguous_publish_failure_never_overwrites_a_fast_worker_result(
    db,
    tenant,
    webhook_integration,
    monkeypatch,
):
    event = WebhookEvent(
        tenant_id=tenant.id,
        integration_id=webhook_integration.id,
        event_type="integration.test",
        payload={"message": "safe"},
        status="pending",
    )
    db.add(event)
    await db.flush()

    def consumed_then_raised(*_args, **_kwargs):
        event.status = "sent"
        event.delivered_at = datetime.now(UTC)
        event.last_error = None
        raise ConnectionError("ambiguous broker acknowledgement")

    monkeypatch.setattr(webhook_tasks.deliver_webhook_event, "delay", consumed_then_raised)

    await integrations_endpoint._enqueue_webhook_delivery(db, event, tenant.id)

    assert event.status == "sent"
    assert event.delivered_at is not None
    assert event.last_error is None
