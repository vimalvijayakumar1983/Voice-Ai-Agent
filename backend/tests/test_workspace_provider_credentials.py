from datetime import UTC, datetime
from uuid import UUID
from xml.etree import ElementTree

import pytest
from sqlalchemy import func, select
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import agents as agents_endpoint
from app.api.v1.endpoints import runtime as runtime_endpoint
from app.api.v1.endpoints import webhooks
from app.core.config import settings
from app.models.agent import Agent, AgentRuntimeProfile, KnowledgeProviderCleanup
from app.models.call import Call
from app.models.provider_credential import ProviderCredential
from app.models.tenant import Tenant
from app.providers.elevenlabs import ElevenLabsError
from app.realtime.auth import verify_media_token
from app.services.knowledge_serving import (
    INBOUND_KNOWLEDGE_ADMISSION_STATE,
    knowledge_admission_is_durable,
    knowledge_call_reservation_metadata,
    pre_admit_outbound_knowledge_call,
)
from app.services.provider_credentials import load_provider_config, store_provider_config
from app.services.twilio_route_security import (
    load_workspace_twilio_route_credential,
    mark_twilio_route_verified,
)
from tests.conftest import test_session_factory as session_factory
from tests.knowledge_test_utils import publish_test_knowledge


async def _mark_verified_twilio_profile(db, tenant_id, profile: AgentRuntimeProfile) -> None:
    credential = await load_workspace_twilio_route_credential(db, tenant_id)
    assert credential is not None
    mark_twilio_route_verified(
        profile,
        credential,
        expected_voice_url="http://test/api/v1/webhooks/twilio/voice/inbound",
    )


@pytest.mark.asyncio
async def test_workspace_provider_credentials_are_encrypted_write_only_and_removable(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "smallest_api_key", "")
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "base_url", "https://voice.example.com")
    monkeypatch.setattr(settings, "twilio_default_from_number", "")

    secrets = {
        "smallest": "smallest_workspace_key_123456789",
        "sarvam": "sarvam_workspace_key_1234567890",
        "openai": "openai_workspace_key_1234567890",
    }
    for provider, api_key in secrets.items():
        response = await client.put(
            f"/api/v1/runtime/credentials/{provider}",
            headers=auth_headers,
            json={"api_key": api_key},
        )
        assert response.status_code == 200
        assert response.json()["source"] == "workspace"
        assert api_key not in response.text

    twilio_token = "twilio_workspace_token_1234567890"
    twilio = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": "AC" + "0123456789abcdef0123456789abcdef",
            "auth_token": twilio_token,
            "default_from_number": "+15551234567",
        },
    )
    assert twilio.status_code == 200
    assert twilio.json()["account_sid_hint"] == "AC••••cdef"
    assert twilio.json()["default_from_number"] == "+15551234567"
    assert twilio_token not in twilio.text

    statuses = await client.get("/api/v1/runtime/credentials", headers=auth_headers)
    assert statuses.status_code == 200
    assert all(
        statuses.json()["providers"][provider]["source"] == "workspace"
        for provider in ("smallest", "sarvam", "openai", "twilio")
    )
    assert not any(secret in statuses.text for secret in (*secrets.values(), twilio_token))

    credentials = (await db.scalars(select(ProviderCredential))).all()
    assert {credential.provider for credential in credentials} >= {
        "smallest",
        "sarvam",
        "openai",
        "twilio",
    }
    encrypted = " ".join(credential.encrypted_config for credential in credentials)
    assert not any(secret in encrypted for secret in (*secrets.values(), twilio_token))

    removed = await client.delete(
        "/api/v1/runtime/credentials/openai",
        headers=auth_headers,
    )
    assert removed.status_code == 200
    assert removed.json()["source"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "telephony_provider", "speech_provider", "llm_provider"),
    [
        (
            "/api/v1/runtime/credentials/openai",
            {"api_key": "openai_rotation_key_123456789"},
            "twilio",
            "sarvam",
            "openai",
        ),
        (
            "/api/v1/runtime/credentials/twilio/account",
            {
                "account_sid": "AC" + "b" * 32,
                "auth_token": "twilio_rotation_token_123456789",
                "default_from_number": "+15551234567",
            },
            "twilio",
            "sarvam",
            "openai",
        ),
        (
            "/api/v1/runtime/sip/credential",
            {
                "sip_uri": "sip:rotation.example.ae",
                "inbound_trunk_id": "ST_rotation_inbound",
                "dispatch_rule_id": "SDR_rotation_dispatch",
                "outbound_trunk_id": "ST_rotation_outbound",
                "agent_name": "vav-inworld",
            },
            "livekit_sip",
            "inworld",
            "inworld",
        ),
    ],
)
async def test_runtime_credential_mutation_requires_active_agents_to_reverify(
    client,
    auth_headers,
    tenant,
    db,
    path,
    payload,
    telephony_provider,
    speech_provider,
    llm_provider,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Credential rotation guard",
        system_prompt="Never run with unverified rotated credentials.",
        voice_provider=speech_provider,
        voice_id=f"{speech_provider}:test-voice",
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider=telephony_provider,
        primary_speech_provider=speech_provider,
        llm_provider=llm_provider,
    )
    db.add(profile)
    await db.commit()

    response = await client.put(path, headers=auth_headers, json=payload)

    assert response.status_code == 200
    await db.refresh(profile)
    assert profile.enabled is False
    assert profile.status == "draft"


@pytest.mark.asyncio
async def test_elevenlabs_workspace_key_is_verified_before_storage(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    validated: list[str] = []

    async def validate_ok(self):
        validated.append(self.api_key)

    monkeypatch.setattr(runtime_endpoint.ElevenLabsClient, "validate_connection", validate_ok)
    api_key = "elevenlabs_workspace_key_123456789"

    saved = await client.put(
        "/api/v1/runtime/credentials/elevenlabs",
        headers=auth_headers,
        json={"api_key": api_key},
    )

    assert saved.status_code == 200
    assert saved.json()["source"] == "workspace"
    assert validated == [api_key]
    credential = await db.scalar(
        select(ProviderCredential).where(ProviderCredential.provider == "elevenlabs")
    )
    assert credential is not None
    assert api_key not in credential.encrypted_config


@pytest.mark.asyncio
async def test_invalid_elevenlabs_workspace_key_is_not_stored(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    async def validate_failed(self):
        raise ElevenLabsError("invalid API key", status_code=401)

    monkeypatch.setattr(runtime_endpoint.ElevenLabsClient, "validate_connection", validate_failed)

    rejected = await client.put(
        "/api/v1/runtime/credentials/elevenlabs",
        headers=auth_headers,
        json={"api_key": "elevenlabs_invalid_key_123456789"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == ("ElevenLabs API key validation failed: invalid API key")
    credential = await db.scalar(
        select(ProviderCredential).where(ProviderCredential.provider == "elevenlabs")
    )
    assert credential is None


@pytest.mark.asyncio
async def test_runtime_readiness_accepts_workspace_openai_and_twilio_credentials(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    async def verify_route(**_kwargs):
        return None

    async def provider_probe(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime_endpoint, "verify_twilio_route_ownership", verify_route)
    monkeypatch.setattr(
        runtime_endpoint.SarvamAIClient,
        "synthesize_voice_preview",
        provider_probe,
    )
    monkeypatch.setattr(runtime_endpoint, "_sarvam_stt_readiness_probe", provider_probe)
    monkeypatch.setattr(
        runtime_endpoint.OpenAIProviderClient,
        "tool_readiness_probe",
        provider_probe,
    )
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "base_url", "https://voice.example.com")
    for provider, api_key in {
        "sarvam": "sarvam_workspace_key_1234567890",
        "openai": "openai_workspace_key_1234567890",
    }.items():
        response = await client.put(
            f"/api/v1/runtime/credentials/{provider}",
            headers=auth_headers,
            json={"api_key": api_key},
        )
        assert response.status_code == 200
    response = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": "AC" + "0123456789abcdef0123456789abcdef",
            "auth_token": "twilio_workspace_token_1234567890",
            "default_from_number": "+15551234567",
        },
    )
    assert response.status_code == 200

    agent = Agent(
        tenant_id=tenant.id,
        name="Workspace credential concierge",
        system_prompt="Help callers.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Workspace credential clinic",
    )
    await db.commit()
    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={"assigned_numbers": ["+15551234567"]},
    )
    tested = await client.post(
        f"/api/v1/runtime/agents/{UUID(configured.json()['agent_id'])}/test",
        headers=auth_headers,
    )

    assert configured.status_code == 200
    assert configured.json()["ready"] is False
    assert tested.status_code == 200
    assert tested.json()["ready"] is True
    assert all(tested.json()["checks"].values())


@pytest.mark.asyncio
async def test_smallest_operations_resolve_the_workspace_key(
    client,
    auth_headers,
    tenant,
    db,
):
    api_key = "smallest_workspace_runtime_key_123456789"
    saved = await client.put(
        "/api/v1/runtime/credentials/smallest",
        headers=auth_headers,
        json={"api_key": api_key},
    )
    smallest, source, _updated_at = await agents_endpoint._tenant_smallest_client(db, tenant.id)

    assert saved.status_code == 200
    assert source == "workspace"
    assert smallest.api_key == api_key


@pytest.mark.asyncio
async def test_smallest_credential_rotation_and_deletion_wait_for_cleanup(
    client,
    auth_headers,
    tenant,
    db,
):
    original_key = "smallest_cleanup_owner_key_123456789"
    saved = await client.put(
        "/api/v1/runtime/credentials/smallest",
        headers=auth_headers,
        json={"api_key": original_key},
    )
    assert saved.status_code == 200

    db.add(
        KnowledgeProviderCleanup(
            tenant_id=tenant.id,
            provider="smallest",
            provider_knowledge_base_id="provider-kb-owned-by-original-key",
            provider_item_id="provider-item-awaiting-delete",
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC),
        )
    )
    await db.commit()

    rotated = await client.put(
        "/api/v1/runtime/credentials/smallest",
        headers=auth_headers,
        json={"api_key": "different_smallest_account_key_123456789"},
    )
    removed = await client.delete(
        "/api/v1/runtime/credentials/smallest",
        headers=auth_headers,
    )

    assert rotated.status_code == 409
    assert removed.status_code == 409
    assert "remote knowledge cleanup is pending" in rotated.json()["detail"]
    assert "remote knowledge cleanup is pending" in removed.json()["detail"]
    config = await load_provider_config(db, tenant.id, "smallest")
    assert config == {"api_key": original_key}


@pytest.mark.asyncio
async def test_missing_smallest_credential_can_be_added_to_unblock_cleanup(
    client,
    auth_headers,
    tenant,
    db,
):
    db.add(
        KnowledgeProviderCleanup(
            tenant_id=tenant.id,
            provider="smallest",
            provider_knowledge_base_id="provider-kb-needing-credential",
            provider_item_id="provider-item-needing-credential",
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC),
        )
    )
    await db.commit()

    saved = await client.put(
        "/api/v1/runtime/credentials/smallest",
        headers=auth_headers,
        json={"api_key": "restored_smallest_account_key_123456789"},
    )

    assert saved.status_code == 200
    assert saved.json()["source"] == "workspace"


@pytest.mark.asyncio
async def test_inbound_twilio_webhook_accepts_the_workspace_auth_token(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    account_sid = "AC" + "0123456789abcdef0123456789abcdef"
    auth_token = "twilio_workspace_token_1234567890"
    number = "+15551234567"
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": number,
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="Twilio workspace routing",
        system_prompt="Help callers.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
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
    await _mark_verified_twilio_profile(db, tenant.id, profile)
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Twilio workspace clinic",
    )
    await db.commit()

    path = "/api/v1/webhooks/twilio/voice/inbound"
    payload = {
        "AccountSid": account_sid,
        "From": "+15557654321",
        "To": number,
        "CallSid": "CAworkspace123",
    }
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )
    replay = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert replay.status_code == 200
    assert "/api/v1/realtime/twilio/" in response.text
    call = await db.scalar(select(Call).where(Call.provider_call_sid == "CAworkspace123"))
    assert call is not None
    assert call.tenant_id == tenant.id
    assert knowledge_admission_is_durable(call.call_metadata)
    runtime = call.call_metadata["runtime"]
    assert runtime["knowledge_admission_state"] == INBOUND_KNOWLEDGE_ADMISSION_STATE
    assert runtime["knowledge_serving_revision_id"]
    assert runtime["knowledge_serving_knowledge_base_id"]
    assert runtime["speech_lexicon_artifact_id"]
    assert runtime["speech_lexicon_content_sha256"]
    duplicate_count = await db.scalar(
        select(func.count()).select_from(Call).where(Call.provider_call_sid == "CAworkspace123")
    )
    assert duplicate_count == 1
    stream = ElementTree.fromstring(response.text).find("./Connect/Stream")
    replay_stream = ElementTree.fromstring(replay.text).find("./Connect/Stream")
    assert stream is not None
    assert replay_stream is not None
    assert replay_stream.attrib["url"] == stream.attrib["url"]
    assert "?" not in stream.attrib["url"]
    token_parameter = stream.find("./Parameter[@name='token']")
    assert token_parameter is not None
    assert verify_media_token(token_parameter.attrib["value"], call.id)


@pytest.mark.asyncio
async def test_inbound_twilio_replay_never_reuses_an_admitted_outbound_call_sid(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    account_sid = "AC" + "1234567890abcdef1234567890abcdef"
    auth_token = "twilio_outbound_collision_token_1234567890"
    number = "+15551234001"
    caller = "+15557654001"
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": number,
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="Inbound collision guard",
        system_prompt="Help callers.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
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
        assigned_numbers=[number],
    )
    db.add(profile)
    await _mark_verified_twilio_profile(db, tenant.id, profile)
    _knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Collision guard clinic",
    )
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="outbound",
        status="dispatching",
        # Keep the numbers in inbound order so direction alone proves this row
        # cannot be replayed as an inbound reservation.
        from_number=caller,
        to_number=number,
        provider="twilio",
        provider_call_sid="CA-outbound-inbound-collision",
        started_at=datetime.now(UTC),
        call_metadata={
            "speech_provider": "sarvam",
            "runtime": {
                "transport": "twilio_media_streams",
                "speech_provider": "sarvam",
                **knowledge_call_reservation_metadata(revision, 0),
            },
        },
    )
    db.add(call)
    await db.flush()
    await pre_admit_outbound_knowledge_call(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        call_id=call.id,
    )
    await db.commit()

    path = "/api/v1/webhooks/twilio/voice/inbound"
    payload = {
        "AccountSid": account_sid,
        "From": caller,
        "To": number,
        "CallSid": "CA-outbound-inbound-collision",
    }
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "temporarily unavailable" in response.text
    assert ElementTree.fromstring(response.text).find("./Connect/Stream") is None
    await db.refresh(call)
    assert call.direction == "outbound"
    assert call.status == "dispatching"
    assert call.call_metadata["runtime"]["knowledge_admission_state"] == (
        "admitted_before_dispatch"
    )
    assert "lifecycle_error" not in call.call_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch_field", ["From", "To"])
async def test_inbound_twilio_replay_rejects_mismatched_numbers_without_mutating_call(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
    mismatch_field,
):
    account_sid = "AC" + "234567890abcdef1234567890abcdef1"
    auth_token = "twilio_number_mismatch_token_1234567890"
    number = "+15551234002"
    alternate_number = "+15551234003"
    caller = "+15557654002"
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": number,
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="Inbound number guard",
        system_prompt="Help callers.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
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
        assigned_numbers=[number, alternate_number],
    )
    db.add(profile)
    await _mark_verified_twilio_profile(db, tenant.id, profile)
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Number guard clinic",
    )
    await db.commit()

    path = "/api/v1/webhooks/twilio/voice/inbound"
    original_payload = {
        "AccountSid": account_sid,
        "From": caller,
        "To": number,
        "CallSid": f"CA-inbound-number-mismatch-{mismatch_field.lower()}",
    }
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    validator = RequestValidator(auth_token)
    original = await client.post(
        path,
        data=original_payload,
        headers={
            "X-Twilio-Signature": validator.compute_signature(
                f"http://test{path}", original_payload
            )
        },
    )
    assert original.status_code == 200
    assert ElementTree.fromstring(original.text).find("./Connect/Stream") is not None

    replay_payload = dict(original_payload)
    replay_payload[mismatch_field] = (
        "+15557654999" if mismatch_field == "From" else alternate_number
    )
    replay = await client.post(
        path,
        data=replay_payload,
        headers={
            "X-Twilio-Signature": validator.compute_signature(f"http://test{path}", replay_payload)
        },
    )

    assert replay.status_code == 200
    assert "temporarily unavailable" in replay.text
    assert ElementTree.fromstring(replay.text).find("./Connect/Stream") is None
    call = await db.scalar(
        select(Call).where(Call.provider_call_sid == original_payload["CallSid"])
    )
    assert call is not None
    assert call.direction == "inbound"
    assert call.status == "in_progress"
    assert call.from_number == caller
    assert call.to_number == number
    assert call.call_metadata["runtime"]["knowledge_admission_state"] == (
        INBOUND_KNOWLEDGE_ADMISSION_STATE
    )
    assert "lifecycle_error" not in call.call_metadata


@pytest.mark.asyncio
async def test_inbound_twilio_without_immutable_knowledge_returns_friendly_hangup(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    account_sid = "AC" + "fedcba9876543210fedcba9876543210"
    auth_token = "twilio_no_knowledge_token_1234567890"
    number = "+15551234999"
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": number,
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="Unpublished inbound route",
        system_prompt="Never serve mutable knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
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
        assigned_numbers=[number],
    )
    db.add(profile)
    await _mark_verified_twilio_profile(db, tenant.id, profile)
    await db.commit()

    path = "/api/v1/webhooks/twilio/voice/inbound"
    payload = {
        "AccountSid": account_sid,
        "From": "+15557654999",
        "To": number,
        "CallSid": "CA-no-immutable-knowledge",
    }
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)

    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    xml = ElementTree.fromstring(response.text)
    assert xml.find("./Hangup") is not None
    assert xml.find("./Connect/Stream") is None
    assert "temporarily unavailable" in response.text
    call = await db.scalar(
        select(Call).where(Call.provider_call_sid == "CA-no-immutable-knowledge")
    )
    assert call is not None
    assert call.status == "failed"
    assert call.call_metadata["lifecycle_error"] == "immutable_knowledge_unavailable"


@pytest.mark.asyncio
async def test_inbound_twilio_webhook_resolves_duplicate_number_by_workspace_credential(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    account_sid = "AC" + "0123456789abcdef0123456789abcdef"
    auth_token = "twilio_workspace_token_1234567890"
    number = "+15551234567"
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": number,
        },
    )
    assert saved.status_code == 200

    routed_agent = Agent(
        tenant_id=tenant.id,
        name="Credential-owned route",
        system_prompt="Help callers.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    other_tenant = Tenant(name="Other workspace", slug="other-workspace-twilio")
    db.add_all([routed_agent, other_tenant])
    await db.flush()
    shadow_agent = Agent(
        tenant_id=other_tenant.id,
        name="Invalid-token shadow route",
        system_prompt="Do not route here.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(shadow_agent)
    await db.flush()
    routed_profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=routed_agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        assigned_numbers=[number],
    )
    shadow_profile = AgentRuntimeProfile(
        tenant_id=other_tenant.id,
        agent_id=shadow_agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        assigned_numbers=[number],
    )
    db.add_all([routed_profile, shadow_profile])
    await _mark_verified_twilio_profile(db, tenant.id, routed_profile)
    await store_provider_config(
        db,
        other_tenant.id,
        "twilio",
        {
            # Knowing another account's public SID is not proof of ownership.
            "account_sid": account_sid,
            "auth_token": "attacker_token_cannot_validate_victim_webhook",
            "default_from_number": number,
        },
    )
    await _mark_verified_twilio_profile(db, other_tenant.id, shadow_profile)
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=routed_agent,
        label="Credential-owned clinic",
    )
    await db.commit()

    path = "/api/v1/webhooks/twilio/voice/inbound"
    payload = {
        "AccountSid": account_sid,
        "From": "+15557654321",
        "To": number,
        "CallSid": "CAworkspace-duplicate-number",
    }
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_account_sid", "")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    response = await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "/api/v1/realtime/twilio/" in response.text
    call = await db.scalar(
        select(Call).where(Call.provider_call_sid == "CAworkspace-duplicate-number")
    )
    assert call is not None
    assert call.tenant_id == tenant.id
    assert call.agent_id == routed_agent.id
