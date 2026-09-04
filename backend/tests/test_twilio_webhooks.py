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


async def _native_twilio_voice_call(
    db,
    tenant,
    *,
    provider_call_sid: str | None = "CA-native-voice-bound",
):
    account_sid = "AC" + "7" * 32
    auth_token = "native_voice_callback_token_123456789"
    await store_provider_config(
        db,
        tenant.id,
        "twilio",
        {
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": "+15551234567",
        },
    )
    agent = Agent(
        tenant_id=tenant.id,
        name="Native voice callback identity",
        system_prompt="Answer from approved knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider="sarvam",
            assigned_numbers=["+15551234567"],
        )
    )
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        from_number="+15551234567",
        to_number="+15557654321",
        provider="twilio",
        provider_call_sid=provider_call_sid,
        started_at=datetime.now(UTC),
        call_metadata={
            "speech_provider": "sarvam",
            "telephony_credential_binding": {
                "provider": "twilio",
                "source": "workspace",
                "account_sid": account_sid,
            },
            "runtime": {"speech_provider": "sarvam"},
        },
    )
    db.add(call)
    await db.commit()
    return call, account_sid, auth_token


async def _post_signed_twilio_voice(
    client,
    monkeypatch,
    call,
    payload,
    *,
    auth_token: str,
):
    path = f"/api/v1/webhooks/twilio/voice/{call.id}"
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
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
    "incoming_call_sid",
    ["CA-native-voice-other-call", None],
    ids=["mismatched", "missing"],
)
async def test_native_voice_callback_rejects_unbound_call_identity_before_mutation_or_capability(
    client,
    tenant,
    db,
    monkeypatch,
    incoming_call_sid,
):
    call, account_sid, auth_token = await _native_twilio_voice_call(db, tenant)
    original_metadata = dict(call.call_metadata)
    capability_factory = Mock(side_effect=AssertionError("media capability must not be minted"))
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)
    payload = {"AccountSid": account_sid}
    if incoming_call_sid is not None:
        payload["CallSid"] = incoming_call_sid

    response = await _post_signed_twilio_voice(
        client,
        monkeypatch,
        call,
        payload,
        auth_token=auth_token,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conflicting Twilio call identity"
    await db.refresh(call)
    assert call.status == "dispatching"
    assert call.answered_at is None
    assert call.call_metadata == original_metadata
    capability_factory.assert_not_called()


@pytest.mark.asyncio
async def test_native_voice_callback_fails_closed_without_persisted_call_identity(
    client,
    tenant,
    db,
    monkeypatch,
):
    call, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
    )
    capability_factory = Mock(side_effect=AssertionError("media capability must not be minted"))
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)

    response = await _post_signed_twilio_voice(
        client,
        monkeypatch,
        call,
        {"AccountSid": account_sid, "CallSid": "CA-native-voice-unbound"},
        auth_token=auth_token,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conflicting Twilio call identity"
    await db.refresh(call)
    assert call.status == "dispatching"
    assert call.answered_at is None
    capability_factory.assert_not_called()


@pytest.mark.asyncio
async def test_native_voice_callback_accepts_matching_call_identity_and_mints_capability(
    client,
    tenant,
    db,
    monkeypatch,
):
    call, account_sid, auth_token = await _native_twilio_voice_call(db, tenant)
    capability_factory = Mock(return_value={"token": "bound-media-capability"})
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)

    response = await _post_signed_twilio_voice(
        client,
        monkeypatch,
        call,
        {"AccountSid": account_sid, "CallSid": call.provider_call_sid},
        auth_token=auth_token,
    )

    assert response.status_code == 200
    assert "<Stream" in response.text
    assert "bound-media-capability" in response.text
    capability_factory.assert_called_once_with(call.id)
    await db.refresh(call)
    assert call.status == "in_progress"
    assert call.answered_at is not None
    assert call.call_metadata["runtime_route"] == {
        "telephony_provider": "twilio",
        "speech_provider": "sarvam",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_call_sid", "incoming_call_sid"),
    [
        ("CA-native-status-bound", "CA-native-status-other-call"),
        ("CA-native-status-bound", None),
        (None, "CA-native-status-unbound"),
    ],
    ids=["mismatched", "missing-incoming", "missing-persisted"],
)
async def test_native_status_callback_rejects_unbound_call_identity_before_mutation(
    client,
    tenant,
    db,
    monkeypatch,
    persisted_call_sid,
    incoming_call_sid,
):
    call, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=persisted_call_sid,
    )
    original_metadata = dict(call.call_metadata)
    payload = {
        "AccountSid": account_sid,
        "CallStatus": "completed",
        "CallDuration": "99",
        "RecordingUrl": "https://recordings.example/wrong-call",
    }
    if incoming_call_sid is not None:
        payload["CallSid"] = incoming_call_sid

    response = await _post_signed_twilio_status(
        client,
        monkeypatch,
        call,
        payload,
        auth_token=auth_token,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conflicting Twilio call identity"
    await db.refresh(call)
    assert call.status == "dispatching"
    assert call.provider_call_sid == persisted_call_sid
    assert call.answered_at is None
    assert call.ended_at is None
    assert call.duration_seconds is None
    assert call.provider_recording_url is None
    assert call.call_metadata == original_metadata


@pytest.mark.asyncio
async def test_native_status_callback_accepts_matching_call_identity(
    client,
    tenant,
    db,
    monkeypatch,
):
    call, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid="CA-native-status-match",
    )

    response = await _post_signed_twilio_status(
        client,
        monkeypatch,
        call,
        {
            "AccountSid": account_sid,
            "CallSid": call.provider_call_sid,
            "CallStatus": "completed",
            "CallDuration": "42",
            "RecordingUrl": "https://recordings.example/matching-call",
        },
        auth_token=auth_token,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    await db.refresh(call)
    assert call.status == "completed"
    assert call.provider_call_sid == "CA-native-status-match"
    assert call.ended_at is not None
    assert call.duration_seconds == 42
    assert call.provider_recording_url == "https://recordings.example/matching-call"


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
    assert call.ended_at.replace(tzinfo=UTC) == original_ended_at.replace(tzinfo=UTC)
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
