"""Twilio webhook routing and authentication tests."""

import uuid

import pytest
from httpx import AsyncClient
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import webhooks
from tests.conftest import test_session_factory as session_factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/webhooks/twilio/voice/inbound", {"CallSid": "CA123"}),
        (f"/api/v1/webhooks/twilio/voice/{uuid.uuid4()}", {"CallSid": "CA123"}),
        (
            f"/api/v1/webhooks/twilio/status/{uuid.uuid4()}",
            {"CallSid": "CA123", "CallStatus": "completed"},
        ),
    ],
)
async def test_every_twilio_webhook_rejects_invalid_signatures(
    client: AsyncClient,
    monkeypatch,
    path: str,
    payload: dict[str, str],
):
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "test_auth_token")

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": "invalid"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Twilio webhook signature"


@pytest.mark.asyncio
async def test_twilio_webhook_fails_closed_without_auth_token(
    client: AsyncClient,
    monkeypatch,
):
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")

    response = await client.post(
        "/api/v1/webhooks/twilio/voice/inbound",
        data={"CallSid": "CA123"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Twilio webhook validation is not configured"


@pytest.mark.asyncio
async def test_signed_inbound_webhook_is_not_shadowed_by_call_id_route(
    client: AsyncClient,
    monkeypatch,
):
    auth_token = "test_auth_token"
    path = "/api/v1/webhooks/twilio/voice/inbound"
    url = f"http://test{path}"
    payload = {
        "From": "+971501234567",
        "To": "+97125550100",
        "CallSid": "CA123",
    }
    signature = RequestValidator(auth_token).compute_signature(url, payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "this number is not configured" in response.text
