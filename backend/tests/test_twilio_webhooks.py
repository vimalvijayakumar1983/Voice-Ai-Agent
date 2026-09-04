"""Twilio webhook routing and authentication tests."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import webhooks
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.services.provider_credentials import (
    ProviderCredentialError,
    invalidate_active_runtimes_for_credential,
    lock_provider_runtime_boundaries,
    store_provider_config,
)
from app.services.twilio_callback_claim import (
    TWILIO_CALLBACK_CLAIM_METADATA_KEY,
    append_twilio_callback_claim,
    create_twilio_callback_claim,
)
from app.services.twilio_route_security import (
    load_workspace_twilio_route_credential,
    mark_twilio_route_verified,
    twilio_callback_credential_fingerprint,
)
from tests.conftest import engine as test_engine
from tests.conftest import test_session_factory as session_factory


async def _post_signed_twilio_status(
    client,
    monkeypatch,
    call,
    payload,
    *,
    auth_token: str,
    callback_claim: str | None = None,
):
    path = f"/api/v1/webhooks/twilio/status/{call.id}"
    url = f"http://test{path}"
    if callback_claim is not None:
        url = append_twilio_callback_claim(url, callback_claim)
        path = url.removeprefix("http://test")
    signature = RequestValidator(auth_token).compute_signature(url, payload)
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
    callback_claim_metadata: dict | None = None,
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
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider="sarvam",
        assigned_numbers=["+15551234567"],
    )
    db.add(profile)
    route_credential = await load_workspace_twilio_route_credential(db, tenant.id)
    assert route_credential is not None
    mark_twilio_route_verified(
        profile,
        route_credential,
        expected_voice_url="http://test/api/v1/webhooks/twilio/voice/inbound",
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
                "credential_fingerprint": twilio_callback_credential_fingerprint(route_credential),
            },
            "runtime": {"speech_provider": "sarvam"},
            **(
                {TWILIO_CALLBACK_CLAIM_METADATA_KEY: callback_claim_metadata}
                if callback_claim_metadata is not None
                else {}
            ),
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
    callback_claim: str | None = None,
):
    path = f"/api/v1/webhooks/twilio/voice/{call.id}"
    url = f"http://test{path}"
    if callback_claim is not None:
        url = append_twilio_callback_claim(url, callback_claim)
        path = url.removeprefix("http://test")
    signature = RequestValidator(auth_token).compute_signature(url, payload)
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
@pytest.mark.parametrize("callback_kind", ["voice", "status"])
@pytest.mark.parametrize("claim_variant", ["missing", "wrong"])
async def test_native_first_callback_requires_exact_dispatch_claim_before_binding_or_mutation(
    client,
    tenant,
    db,
    monkeypatch,
    callback_kind,
    claim_variant,
):
    callback_claim, callback_claim_metadata = create_twilio_callback_claim()
    call, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
        callback_claim_metadata=callback_claim_metadata,
    )
    call_id = call.id
    original_metadata = dict(call.call_metadata)
    capability_factory = Mock(side_effect=AssertionError("media capability must not be minted"))
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)
    supplied_claim = None if claim_variant == "missing" else f"wrong-{callback_claim}"
    payload = {
        "AccountSid": account_sid,
        "CallSid": f"CA-native-{callback_kind}-cannot-bind",
    }
    if callback_kind == "status":
        payload.update(
            {
                "CallStatus": "completed",
                "CallDuration": "99",
                "RecordingUrl": "https://recordings.example/untrusted-first-bind",
            }
        )
        response = await _post_signed_twilio_status(
            client,
            monkeypatch,
            call,
            payload,
            auth_token=auth_token,
            callback_claim=supplied_claim,
        )
    else:
        response = await _post_signed_twilio_voice(
            client,
            monkeypatch,
            call,
            payload,
            auth_token=auth_token,
            callback_claim=supplied_claim,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conflicting Twilio call identity"
    db.expire_all()
    stored = await db.get(Call, call_id)
    assert stored.provider_call_sid is None
    assert stored.status == "dispatching"
    assert stored.answered_at is None
    assert stored.ended_at is None
    assert stored.duration_seconds is None
    assert stored.provider_recording_url is None
    assert stored.call_metadata == original_metadata
    capability_factory.assert_not_called()


@pytest.mark.asyncio
async def test_native_first_voice_callback_atomically_binds_sid_with_exact_dispatch_claim(
    client,
    tenant,
    db,
    monkeypatch,
):
    callback_claim, callback_claim_metadata = create_twilio_callback_claim()
    call, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
        callback_claim_metadata=callback_claim_metadata,
    )
    call_id = call.id
    capability_factory = Mock(return_value={"token": "first-bind-media-capability"})
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)

    response = await _post_signed_twilio_voice(
        client,
        monkeypatch,
        call,
        {"AccountSid": account_sid, "CallSid": "CA-native-first-callback"},
        auth_token=auth_token,
        callback_claim=callback_claim,
    )

    assert response.status_code == 200
    assert "first-bind-media-capability" in response.text
    db.expire_all()
    stored = await db.get(Call, call_id)
    assert stored.provider_call_sid == "CA-native-first-callback"
    assert stored.status == "in_progress"
    claim = stored.call_metadata[TWILIO_CALLBACK_CLAIM_METADATA_KEY]
    assert claim["state"] == "bound"
    assert claim["bound_via"] == "provider_callback"
    assert callback_claim not in str(stored.call_metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", ["voice", "status"])
async def test_native_first_callback_rejects_sid_owned_by_another_local_call(
    client,
    tenant,
    db,
    monkeypatch,
    callback_kind,
):
    owner_claim, owner_claim_metadata = create_twilio_callback_claim()
    owner, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
        callback_claim_metadata=owner_claim_metadata,
    )
    contender_claim, contender_claim_metadata = create_twilio_callback_claim()
    contender, _account_sid, _auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
        callback_claim_metadata=contender_claim_metadata,
    )
    owner_id = owner.id
    contender_id = contender.id
    contender_metadata = dict(contender.call_metadata)
    shared_call_sid = f"CA-native-cross-call-{callback_kind}"

    if callback_kind == "voice":
        capability_factory = Mock(return_value={"token": "owner-media-capability"})
        monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)
        owner_response = await _post_signed_twilio_voice(
            client,
            monkeypatch,
            owner,
            {"AccountSid": account_sid, "CallSid": shared_call_sid},
            auth_token=auth_token,
            callback_claim=owner_claim,
        )
        contender_response = await _post_signed_twilio_voice(
            client,
            monkeypatch,
            contender,
            {"AccountSid": account_sid, "CallSid": shared_call_sid},
            auth_token=auth_token,
            callback_claim=contender_claim,
        )
        assert capability_factory.call_count == 1
    else:
        payload = {
            "AccountSid": account_sid,
            "CallSid": shared_call_sid,
            "CallStatus": "ringing",
        }
        owner_response = await _post_signed_twilio_status(
            client,
            monkeypatch,
            owner,
            payload,
            auth_token=auth_token,
            callback_claim=owner_claim,
        )
        contender_response = await _post_signed_twilio_status(
            client,
            monkeypatch,
            contender,
            payload,
            auth_token=auth_token,
            callback_claim=contender_claim,
        )

    assert owner_response.status_code == 200
    assert contender_response.status_code == 409
    assert contender_response.json()["detail"] == "Conflicting Twilio call identity"
    db.expire_all()
    stored_owner = await db.get(Call, owner_id)
    stored_contender = await db.get(Call, contender_id)
    assert stored_owner.provider_call_sid == shared_call_sid
    assert stored_contender.provider_call_sid is None
    assert stored_contender.status == "dispatching"
    assert stored_contender.call_metadata == contender_metadata


@pytest.mark.asyncio
async def test_concurrent_native_first_callbacks_with_different_sids_have_one_winner(
    client,
    tenant,
    db,
    monkeypatch,
):
    callback_claim, callback_claim_metadata = create_twilio_callback_claim()
    call, account_sid, auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
        callback_claim_metadata=callback_claim_metadata,
    )
    call_id = call.id
    capability_factory = Mock(return_value={"token": "winner-only-media-capability"})
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)

    async def send(call_sid: str):
        return await _post_signed_twilio_voice(
            client,
            monkeypatch,
            call,
            {"AccountSid": account_sid, "CallSid": call_sid},
            auth_token=auth_token,
            callback_claim=callback_claim,
        )

    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as task_group:
            first_response = task_group.create_task(send("CA-first-race-a"))
            second_response = task_group.create_task(send("CA-first-race-b"))
    responses = [first_response.result(), second_response.result()]

    assert sorted(response.status_code for response in responses) == [200, 409]
    db.expire_all()
    stored = await db.get(Call, call_id)
    assert stored.provider_call_sid in {"CA-first-race-a", "CA-first-race-b"}
    assert stored.status == "in_progress"
    assert capability_factory.call_count == 1


@pytest.mark.asyncio
async def test_postgres_native_first_callback_binding_has_one_cross_replica_winner(
    tenant,
    db,
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    callback_claim, callback_claim_metadata = create_twilio_callback_claim()
    call, _account_sid, _auth_token = await _native_twilio_voice_call(
        db,
        tenant,
        provider_call_sid=None,
        callback_claim_metadata=callback_claim_metadata,
    )
    call_id = call.id
    postgres_sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def bind(call_sid: str) -> str:
        async with postgres_sessions() as callback_db:
            probe = await callback_db.scalar(select(Call).where(Call.id == call_id))
            locked, _attempt = await webhooks._lock_callback_call_graph(callback_db, probe)
            try:
                webhooks._require_native_twilio_callback_identity(
                    {"CallSid": call_sid},
                    locked,
                    callback_claim=callback_claim,
                )
            except HTTPException as exc:
                await callback_db.rollback()
                assert exc.status_code == 409
                return "rejected"
            await callback_db.commit()
            return "bound"

    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as task_group:
            first_outcome = task_group.create_task(bind("CA-cross-replica-a"))
            second_outcome = task_group.create_task(bind("CA-cross-replica-b"))
    outcomes = [first_outcome.result(), second_outcome.result()]

    assert sorted(outcomes) == ["bound", "rejected"]
    db.expire_all()
    stored = await db.get(Call, call_id)
    assert stored.provider_call_sid in {"CA-cross-replica-a", "CA-cross-replica-b"}


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
async def test_native_voice_callback_rejects_bad_signature_before_any_contention_lock(
    client,
    tenant,
    db,
    monkeypatch,
):
    """An unauthenticated request cannot queue behind a real callback."""

    call, account_sid, _auth_token = await _native_twilio_voice_call(db, tenant)
    call_id = call.id
    original_metadata = dict(call.call_metadata)

    @asynccontextmanager
    async def forbidden_local_lock(_identity):
        raise AssertionError("invalid callbacks must not enter the local contention lock")
        yield  # pragma: no cover - makes this an async context manager

    async def forbidden_provider_lock(*_args, **_kwargs):
        raise AssertionError("invalid callbacks must not enter the provider contention lock")

    capability_factory = Mock(side_effect=AssertionError("media capability must not be minted"))
    monkeypatch.setattr(webhooks, "_local_provider_callback_lock", forbidden_local_lock)
    monkeypatch.setattr(webhooks, "lock_provider_runtime_boundaries", forbidden_provider_lock)
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)

    response = await client.post(
        f"/api/v1/webhooks/twilio/voice/{call_id}",
        data={"AccountSid": account_sid, "CallSid": call.provider_call_sid},
        headers={"X-Twilio-Signature": "invalid"},
    )

    assert response.status_code == 401
    db.expire_all()
    stored = await db.get(Call, call_id)
    assert stored.status == "dispatching"
    assert stored.answered_at is None
    assert stored.call_metadata == original_metadata
    capability_factory.assert_not_called()


@pytest.mark.asyncio
async def test_native_voice_callback_revalidates_current_credential_inside_runtime_boundary(
    client,
    tenant,
    db,
    monkeypatch,
):
    """The same credential must authorize both the cheap probe and locked admission."""

    call, account_sid, auth_token = await _native_twilio_voice_call(db, tenant)
    events: list[str] = []
    original_validate = webhooks._validate_twilio_request
    original_local_lock = webhooks._local_provider_callback_lock
    original_provider_lock = webhooks.lock_provider_runtime_boundaries
    original_credential_loader = webhooks.load_workspace_twilio_route_credential

    def tracked_validate(*args, **kwargs):
        events.append("signature_validated")
        return original_validate(*args, **kwargs)

    @asynccontextmanager
    async def tracked_local_lock(identity):
        events.append("local_lock_entered")
        async with original_local_lock(identity):
            yield

    async def tracked_provider_lock(*args, **kwargs):
        events.append("provider_boundary_acquired")
        await original_provider_lock(*args, **kwargs)

    async def tracked_credential_loader(*args, **kwargs):
        events.append(
            "locked_current_credential_loaded"
            if kwargs.get("for_update")
            else "probe_credential_loaded"
        )
        return await original_credential_loader(*args, **kwargs)

    def tracked_capability(_call_id):
        events.append("media_capability_minted")
        return {"token": "ordered-media-capability"}

    monkeypatch.setattr(webhooks, "_validate_twilio_request", tracked_validate)
    monkeypatch.setattr(webhooks, "_local_provider_callback_lock", tracked_local_lock)
    monkeypatch.setattr(webhooks, "lock_provider_runtime_boundaries", tracked_provider_lock)
    monkeypatch.setattr(
        webhooks,
        "load_workspace_twilio_route_credential",
        tracked_credential_loader,
    )
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", tracked_capability)

    response = await _post_signed_twilio_voice(
        client,
        monkeypatch,
        call,
        {"AccountSid": account_sid, "CallSid": call.provider_call_sid},
        auth_token=auth_token,
    )

    assert response.status_code == 200
    assert "ordered-media-capability" in response.text
    first_signature = events.index("signature_validated")
    local_lock = events.index("local_lock_entered")
    provider_boundary = events.index("provider_boundary_acquired")
    locked_credential = events.index("locked_current_credential_loaded")
    second_signature = events.index("signature_validated", first_signature + 1)
    media_capability = events.index("media_capability_minted")
    assert first_signature < local_lock < provider_boundary
    assert provider_boundary < locked_credential < second_signature < media_capability
    assert events.count("signature_validated") == 2


@pytest.mark.asyncio
async def test_native_voice_callback_rotation_first_rejects_without_media_or_mutation(
    client,
    tenant,
    db,
    monkeypatch,
):
    """A rotation between probe authentication and admission wins fail-closed."""

    call, account_sid, old_auth_token = await _native_twilio_voice_call(db, tenant)
    call_id = call.id
    agent_id = call.agent_id
    original_metadata = dict(call.call_metadata)
    original_local_lock = webhooks._local_provider_callback_lock
    rotated = False

    @asynccontextmanager
    async def rotate_before_admission(identity):
        nonlocal rotated
        async with session_factory() as rotation_db:
            invalidated = await invalidate_active_runtimes_for_credential(
                rotation_db,
                tenant.id,
                "twilio",
            )
            assert invalidated == [str(agent_id)]
            await store_provider_config(
                rotation_db,
                tenant.id,
                "twilio",
                {
                    "account_sid": account_sid,
                    "auth_token": "rotated_callback_token_987654321",
                    "default_from_number": "+15551234567",
                },
            )
            await rotation_db.commit()
        rotated = True
        async with original_local_lock(identity):
            yield

    capability_factory = Mock(side_effect=AssertionError("media capability must not be minted"))
    monkeypatch.setattr(webhooks, "_local_provider_callback_lock", rotate_before_admission)
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)

    response = await _post_signed_twilio_voice(
        client,
        monkeypatch,
        call,
        {"AccountSid": account_sid, "CallSid": call.provider_call_sid},
        auth_token=old_auth_token,
    )

    assert rotated is True
    assert response.status_code == 401
    assert "<Stream" not in response.text
    db.expire_all()
    stored_call = await db.get(Call, call_id)
    stored_profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent_id)
    )
    assert stored_call.status == "dispatching"
    assert stored_call.answered_at is None
    assert stored_call.call_metadata == original_metadata
    assert stored_profile.enabled is False
    assert stored_profile.status == "draft"
    capability_factory.assert_not_called()


@pytest.mark.asyncio
async def test_postgres_native_voice_rotation_first_blocks_then_rejects_callback(
    client,
    tenant,
    db,
    monkeypatch,
):
    """A committed rotation wins when its provider boundary was acquired first."""

    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL advisory-lock semantics")

    call, account_sid, old_auth_token = await _native_twilio_voice_call(db, tenant)
    call_id = call.id
    agent_id = call.agent_id
    original_metadata = dict(call.call_metadata)
    postgres_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    rotation_holds_boundary = asyncio.Event()
    release_rotation = asyncio.Event()
    callback_requested_boundary = asyncio.Event()
    loop = asyncio.get_running_loop()
    callback_task: asyncio.Task | None = None
    rotation_task: asyncio.Task | None = None
    listener_installed = False

    async def rotate_while_holding_boundary() -> None:
        async with postgres_sessions() as rotation_db, rotation_db.begin():
            await lock_provider_runtime_boundaries(rotation_db, tenant.id, "twilio")
            invalidated = await invalidate_active_runtimes_for_credential(
                rotation_db,
                tenant.id,
                "twilio",
            )
            assert invalidated == [str(agent_id)]
            await store_provider_config(
                rotation_db,
                tenant.id,
                "twilio",
                {
                    "account_sid": account_sid,
                    "auth_token": "postgres_rotation_first_token_987654321",
                    "default_from_number": "+15551234567",
                },
            )
            await rotation_db.flush()
            rotation_holds_boundary.set()
            await release_rotation.wait()

    def observe_callback_boundary_request(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = str(statement).upper()
        values = str(parameters)
        if "PG_ADVISORY_XACT_LOCK" in normalized and "runtime-provider:" in values:
            loop.call_soon_threadsafe(callback_requested_boundary.set)

    capability_factory = Mock(side_effect=AssertionError("media capability must not be minted"))
    monkeypatch.setattr(webhooks, "_runtime_stream_parameters", capability_factory)

    try:
        rotation_task = asyncio.create_task(rotate_while_holding_boundary())
        await asyncio.wait_for(rotation_holds_boundary.wait(), timeout=5)

        event.listen(
            test_engine.sync_engine,
            "before_cursor_execute",
            observe_callback_boundary_request,
        )
        listener_installed = True
        callback_task = asyncio.create_task(
            _post_signed_twilio_voice(
                client,
                monkeypatch,
                call,
                {"AccountSid": account_sid, "CallSid": call.provider_call_sid},
                auth_token=old_auth_token,
            )
        )
        await asyncio.wait_for(callback_requested_boundary.wait(), timeout=5)

        assert not callback_task.done()
        release_rotation.set()

        await asyncio.wait_for(rotation_task, timeout=5)
        response = await asyncio.wait_for(callback_task, timeout=5)
    finally:
        release_rotation.set()
        if listener_installed:
            event.remove(
                test_engine.sync_engine,
                "before_cursor_execute",
                observe_callback_boundary_request,
            )
        for task in (callback_task, rotation_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in (callback_task, rotation_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert response.status_code == 401
    assert "<Stream" not in response.text
    db.expire_all()
    stored_call = await db.get(Call, call_id)
    stored_profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent_id)
    )
    assert stored_call.status == "dispatching"
    assert stored_call.answered_at is None
    assert stored_call.call_metadata == original_metadata
    assert stored_profile.enabled is False
    assert stored_profile.status == "draft"
    capability_factory.assert_not_called()


@pytest.mark.asyncio
async def test_postgres_native_voice_callback_first_serializes_credential_rotation(
    client,
    tenant,
    db,
    monkeypatch,
):
    """A callback holding provider/runtime authority may finish before rotation."""

    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL advisory-lock semantics")

    call, account_sid, auth_token = await _native_twilio_voice_call(db, tenant)
    call_id = call.id
    agent_id = call.agent_id
    postgres_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    callback_holds_boundary = asyncio.Event()
    release_callback = asyncio.Event()
    rotation_requested_boundary = asyncio.Event()
    rotation_acquired_boundary = asyncio.Event()
    original_runtime_lock = webhooks._lock_native_twilio_voice_callback_runtime
    loop = asyncio.get_running_loop()
    callback_task: asyncio.Task | None = None
    rotation_task: asyncio.Task | None = None
    listener_installed = False

    async def hold_callback_boundary(*args, **kwargs):
        result = await original_runtime_lock(*args, **kwargs)
        callback_holds_boundary.set()
        await release_callback.wait()
        return result

    async def rotate_after_callback() -> None:
        async with postgres_sessions() as rotation_db, rotation_db.begin():
            await lock_provider_runtime_boundaries(rotation_db, tenant.id, "twilio")
            rotation_acquired_boundary.set()
            invalidated = await invalidate_active_runtimes_for_credential(
                rotation_db,
                tenant.id,
                "twilio",
            )
            assert invalidated == [str(agent_id)]
            await store_provider_config(
                rotation_db,
                tenant.id,
                "twilio",
                {
                    "account_sid": account_sid,
                    "auth_token": "postgres_callback_first_rotated_987654321",
                    "default_from_number": "+15551234567",
                },
            )

    def observe_rotation_boundary_request(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = str(statement).upper()
        values = str(parameters)
        if "PG_ADVISORY_XACT_LOCK" in normalized and "runtime-provider:" in values:
            loop.call_soon_threadsafe(rotation_requested_boundary.set)

    monkeypatch.setattr(
        webhooks,
        "_lock_native_twilio_voice_callback_runtime",
        hold_callback_boundary,
    )
    monkeypatch.setattr(
        webhooks,
        "_runtime_stream_parameters",
        lambda _call_id: {"token": "callback-first-media-capability"},
    )

    try:
        callback_task = asyncio.create_task(
            _post_signed_twilio_voice(
                client,
                monkeypatch,
                call,
                {"AccountSid": account_sid, "CallSid": call.provider_call_sid},
                auth_token=auth_token,
            )
        )
        await asyncio.wait_for(callback_holds_boundary.wait(), timeout=5)

        event.listen(
            test_engine.sync_engine,
            "before_cursor_execute",
            observe_rotation_boundary_request,
        )
        listener_installed = True
        rotation_task = asyncio.create_task(rotate_after_callback())
        await asyncio.wait_for(rotation_requested_boundary.wait(), timeout=5)

        assert not rotation_acquired_boundary.is_set()
        assert not rotation_task.done()
        release_callback.set()

        response = await asyncio.wait_for(callback_task, timeout=5)
        await asyncio.wait_for(rotation_task, timeout=5)
    finally:
        release_callback.set()
        if listener_installed:
            event.remove(
                test_engine.sync_engine,
                "before_cursor_execute",
                observe_rotation_boundary_request,
            )
        for task in (callback_task, rotation_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in (callback_task, rotation_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert response.status_code == 200
    assert "<Stream" in response.text
    assert "callback-first-media-capability" in response.text
    db.expire_all()
    stored_call = await db.get(Call, call_id)
    stored_profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent_id)
    )
    assert stored_call.status == "in_progress"
    assert stored_call.answered_at is not None
    assert stored_call.call_metadata["runtime_route"] == {
        "telephony_provider": "twilio",
        "speech_provider": "sarvam",
    }
    assert stored_profile.enabled is False
    assert stored_profile.status == "draft"


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
