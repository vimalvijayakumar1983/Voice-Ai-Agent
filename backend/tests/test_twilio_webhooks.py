"""Twilio webhook routing and authentication tests."""

import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import webhooks
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.services.provider_credentials import ProviderCredentialError, store_provider_config
from app.services.twilio_route_security import (
    load_workspace_twilio_route_credential,
    mark_twilio_route_verified,
)
from tests.conftest import test_session_factory as session_factory


async def _post_signed_twilio_status(client, monkeypatch, call, payload, *, auth_token: str):
    path = f"/api/v1/webhooks/twilio/status/{call.id}"
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(webhooks, "_kick_provider_outbox", lambda _ids: None)
    return await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )


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
    db,
    monkeypatch,
    path: str,
    payload: dict[str, str],
):
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "test_auth_token")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)

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
    account_sid = "AC" + "0" * 32
    path = "/api/v1/webhooks/twilio/voice/inbound"
    url = f"http://test{path}"
    payload = {
        "AccountSid": account_sid,
        "From": "+971501234567",
        "To": "+97125550100",
        "CallSid": "CA123",
    }
    signature = RequestValidator(auth_token).compute_signature(url, payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_account_sid", account_sid)
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


@pytest.mark.asyncio
async def test_invalid_tenant_claim_cannot_dos_a_platform_twilio_did(
    client,
    tenant,
    db,
    monkeypatch,
):
    platform_sid = "AC" + "c" * 32
    platform_token = "platform_twilio_signature_token"
    number = "+15551234567"
    await store_provider_config(
        db,
        tenant.id,
        "twilio",
        {
            "account_sid": platform_sid,
            "auth_token": "self_asserted_wrong_token_123456789",
            "default_from_number": number,
        },
    )
    agent = Agent(
        tenant_id=tenant.id,
        name="Platform DID shadow attempt",
        system_prompt="This route must never intercept the platform account.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        assigned_numbers=[number],
    )
    db.add(profile)
    credential = await load_workspace_twilio_route_credential(db, tenant.id)
    assert credential is not None
    mark_twilio_route_verified(
        profile,
        credential,
        expected_voice_url="http://test/api/v1/webhooks/twilio/voice/inbound",
    )
    await db.commit()

    path = "/api/v1/webhooks/twilio/voice/inbound"
    payload = {
        "AccountSid": platform_sid,
        "From": "+15557654321",
        "To": number,
        "CallSid": "CA-platform-shadow-guard",
    }
    signature = RequestValidator(platform_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_account_sid", platform_sid)
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", platform_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "this number is not configured" in response.text
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", ["voice", "status"])
@pytest.mark.parametrize("credential_state", ["absent", "unreadable"])
async def test_native_twilio_callback_never_falls_back_to_platform_credential(
    client,
    tenant,
    db,
    monkeypatch,
    callback_kind,
    credential_state,
):
    platform_sid = "AC" + "d" * 32
    platform_token = "platform_callback_token_must_not_authorize_native"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="dispatching",
        from_number="+15551234567",
        to_number="+15557654321",
        provider="twilio",
        provider_call_sid="CA-native-callback-fail-closed",
        started_at=datetime.now(UTC),
        call_metadata={
            "speech_provider": "sarvam",
            "telephony_credential_binding": {
                "provider": "twilio",
                "source": "workspace",
                "account_sid": "AC" + "e" * 32,
            },
            "runtime": {"speech_provider": "sarvam"},
        },
    )
    db.add(call)
    await db.commit()
    if credential_state == "unreadable":

        async def unreadable(*_args, **_kwargs):
            raise ProviderCredentialError("cannot decrypt")

        monkeypatch.setattr(webhooks, "load_workspace_twilio_route_credential", unreadable)

    path = f"/api/v1/webhooks/twilio/{callback_kind}/{call.id}"
    payload = {
        "AccountSid": platform_sid,
        "CallSid": call.provider_call_sid,
    }
    if callback_kind == "status":
        payload.update({"CallStatus": "completed", "CallDuration": "99"})
    signature = RequestValidator(platform_token).compute_signature(f"http://test{path}", payload)
    kick_outbox = Mock()
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_account_sid", platform_sid)
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", platform_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(webhooks, "_kick_provider_outbox", kick_outbox)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 503
    await db.refresh(call)
    assert call.status == "dispatching"
    assert call.answered_at is None
    assert call.ended_at is None
    assert call.duration_seconds is None
    kick_outbox.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_status", "expected_status"),
    [("completed", "completed"), ("in-progress", "terminal_unknown")],
)
async def test_late_signed_twilio_callback_only_terminally_reconciles_terminal_unknown(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
    callback_status,
    expected_status,
):
    auth_token = "test_auth_token"
    call_sid = f"CA-late-{callback_status}"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="inbound",
        status="terminal_unknown",
        from_number="+971501234567",
        to_number="+97125550100",
        provider="twilio",
        provider_call_sid=call_sid,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )
    db.add(call)
    await db.commit()

    path = f"/api/v1/webhooks/twilio/status/{call.id}"
    payload = {
        "CallSid": call.provider_call_sid,
        "CallStatus": callback_status,
        "CallDuration": "42",
    }
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(webhooks, "_kick_provider_outbox", lambda _ids: None)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["status"] == (
        "ok" if callback_status == "completed" else "ignored_stale_nonterminal"
    )
    await db.refresh(call)
    assert call.status == expected_status
    assert call.duration_seconds == (42 if callback_status == "completed" else None)


@pytest.mark.asyncio
async def test_late_nonterminal_callback_cannot_mutate_completed_call(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
):
    auth_token = "test_auth_token"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="inbound",
        status="completed",
        from_number="+971501234567",
        to_number="+97125550100",
        provider="twilio",
        provider_call_sid="CA-completed-late-progress",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        duration_seconds=37,
        provider_recording_url="https://recordings.example/original",
    )
    db.add(call)
    await db.commit()
    original_ended_at = call.ended_at

    path = f"/api/v1/webhooks/twilio/status/{call.id}"
    payload = {
        "CallSid": call.provider_call_sid,
        "CallStatus": "in-progress",
        "CallDuration": "99",
        "RecordingUrl": "https://recordings.example/stale",
    }
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(webhooks, "_kick_provider_outbox", lambda _ids: None)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored_stale_nonterminal"
    await db.refresh(call)
    assert call.status == "completed"
    assert call.duration_seconds == 37
    assert call.ended_at == original_ended_at.replace(tzinfo=None)
    assert call.provider_recording_url == "https://recordings.example/original"


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_duration", [None, "0", "12"])
async def test_duplicate_terminal_callback_cannot_reduce_duration(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
    callback_duration,
):
    auth_token = "test_auth_token"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="inbound",
        status="completed",
        from_number="+971501234567",
        to_number="+97125550100",
        provider="twilio",
        provider_call_sid="CA-completed-duplicate",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        duration_seconds=37,
    )
    db.add(call)
    await db.commit()

    path = f"/api/v1/webhooks/twilio/status/{call.id}"
    payload = {
        "CallSid": call.provider_call_sid,
        "CallStatus": "completed",
    }
    if callback_duration is not None:
        payload["CallDuration"] = callback_duration
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    monkeypatch.setattr(webhooks, "_kick_provider_outbox", lambda _ids: None)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    await db.refresh(call)
    assert call.status == "completed"
    assert call.duration_seconds == 37


@pytest.mark.asyncio
async def test_out_of_order_twilio_status_does_not_regress_in_progress(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
):
    auth_token = "test_auth_token"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="in_progress",
        from_number="+97125550100",
        to_number="+971501234567",
        provider="twilio",
        provider_call_sid="CA-in-progress-stale-queued",
        started_at=datetime.now(UTC),
        duration_seconds=7,
        provider_recording_url="https://recordings.example/original",
    )
    db.add(call)
    await db.commit()

    response = await _post_signed_twilio_status(
        client,
        monkeypatch,
        call,
        {
            "CallSid": call.provider_call_sid,
            "CallStatus": "queued",
            "CallDuration": "99",
            "RecordingUrl": "https://recordings.example/stale",
        },
        auth_token=auth_token,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored_stale_nonterminal"
    await db.refresh(call)
    assert call.status == "in_progress"
    assert call.duration_seconds == 7
    assert call.provider_recording_url == "https://recordings.example/original"


@pytest.mark.asyncio
async def test_unknown_twilio_status_cannot_escape_recoverable_state_machine(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
):
    auth_token = "test_auth_token"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="ringing",
        from_number="+97125550100",
        to_number="+971501234567",
        provider="twilio",
        provider_call_sid="CA-ringing-unknown-status",
        started_at=datetime.now(UTC),
        duration_seconds=5,
    )
    db.add(call)
    await db.commit()

    response = await _post_signed_twilio_status(
        client,
        monkeypatch,
        call,
        {
            "CallSid": call.provider_call_sid,
            "CallStatus": "provider-mystery-state",
            "CallDuration": "99",
        },
        auth_token=auth_token,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored_unknown_status"
    await db.refresh(call)
    assert call.status == "ringing"
    assert call.duration_seconds == 5


@pytest.mark.asyncio
async def test_twilio_queued_status_normalizes_to_watchdog_covered_ringing(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
):
    auth_token = "test_auth_token"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="dispatching",
        from_number="+97125550100",
        to_number="+971501234567",
        provider="twilio",
        provider_call_sid="CA-dispatching-queued",
        started_at=datetime.now(UTC),
    )
    db.add(call)
    await db.commit()

    response = await _post_signed_twilio_status(
        client,
        monkeypatch,
        call,
        {
            "CallSid": call.provider_call_sid,
            "CallStatus": "queued",
        },
        auth_token=auth_token,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    await db.refresh(call)
    assert call.status == "ringing"


@pytest.mark.asyncio
async def test_late_twilio_voice_webhook_cannot_reopen_terminal_unknown(
    client: AsyncClient,
    db,
    tenant,
    monkeypatch,
):
    auth_token = "test_auth_token"
    call = Call(
        tenant_id=tenant.id,
        agent_id=None,
        direction="outbound",
        status="terminal_unknown",
        from_number="+97125550100",
        to_number="+971501234567",
        provider="twilio",
        provider_call_sid="CA-late-voice-answer",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )
    db.add(call)
    await db.commit()

    path = f"/api/v1/webhooks/twilio/voice/{call.id}"
    payload = {"CallSid": call.provider_call_sid}
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", auth_token)
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "<Hangup" in response.text
    assert "<Stream" not in response.text
    await db.refresh(call)
    assert call.status == "terminal_unknown"
