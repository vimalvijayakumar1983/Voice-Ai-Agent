from uuid import UUID

import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import agents as agents_endpoint
from app.api.v1.endpoints import webhooks
from app.core.config import settings
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.models.provider_credential import ProviderCredential
from tests.conftest import test_session_factory as session_factory


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
async def test_runtime_readiness_accepts_workspace_openai_and_twilio_credentials(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
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
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            assigned_numbers=[number],
        )
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

    assert response.status_code == 200
    assert "/api/v1/realtime/twilio/" in response.text
    call = await db.scalar(select(Call).where(Call.provider_call_sid == "CAworkspace123"))
    assert call is not None
    assert call.tenant_id == tenant.id
