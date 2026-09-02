"""Agent endpoint tests."""

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.models.campaign import Campaign, CampaignContact, CampaignContactAttempt


@pytest.mark.asyncio
async def test_create_agent(client: AsyncClient, auth_headers):
    response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Sales Agent",
            "system_prompt": "You are a helpful sales agent.",
            "greeting_message": "Hello! How can I help you today?",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Sales Agent"
    assert data["model_provider"] == "smallest"
    assert data["voice_provider"] == "smallest"
    assert data["sync_status"] == "local_only"


@pytest.mark.asyncio
async def test_list_agents(client: AsyncClient, auth_headers):
    # Create an agent first
    await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Test Agent", "system_prompt": "Test prompt"},
    )

    response = await client.get("/api/v1/agents", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_update_agent(client: AsyncClient, auth_headers):
    # Create
    create_resp = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Original", "system_prompt": "Original prompt"},
    )
    agent_id = create_resp.json()["id"]

    # Update
    response = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"name": "Updated", "temperature": 0.5},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Updated"
    assert response.json()["temperature"] == 0.5


@pytest.mark.asyncio
async def test_inworld_voice_tuning_preserves_explicit_runtime_llm_choice(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Hybrid support", "system_prompt": "Use approved knowledge."},
    )
    agent_id = UUID(created.json()["id"])
    agent = await db.get(Agent, agent_id)
    agent.voice_provider = "inworld"
    agent.voice_id = "inworld:Ashley"
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o",
        status="active",
        enabled=True,
    )
    db.add(profile)
    await db.commit()

    response = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"speech_rate": 0.95},
    )

    assert response.status_code == 200
    await db.refresh(profile)
    assert profile.llm_provider == "inworld"
    assert profile.llm_model == "openai/gpt-4o"
    assert profile.status == "draft"
    assert profile.enabled is False


@pytest.mark.asyncio
async def test_agent_accepts_safe_provider_language_tags_longer_than_ten_characters(
    client: AsyncClient,
    auth_headers,
):
    provider_language = "abc-provider-tag"
    response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Future language",
            "system_prompt": "Support provider language tags without truncation.",
            "language": provider_language,
            "supported_languages": [provider_language],
        },
    )

    assert response.status_code == 201
    assert response.json()["language"] == provider_language


@pytest.mark.asyncio
async def test_agent_delete_waits_for_nonterminal_calls_and_campaign_attempts(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Reachable watchdog", "system_prompt": "Keep active work reachable."},
    )
    agent_id = created.json()["id"]
    agent_uuid = UUID(agent_id)
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        direction="outbound",
        status="ringing",
        from_number="provider-managed",
        to_number="+971501234567",
        provider="smallest",
    )
    db.add(call)
    await db.commit()

    blocked_by_call = await client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
    )
    assert blocked_by_call.status_code == 409
    assert "call is still nonterminal" in blocked_by_call.json()["detail"]

    call.status = "completed"
    campaign = Campaign(
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        name="Accepted watchdog campaign",
        status="paused",
    )
    db.add(campaign)
    await db.flush()
    contact = CampaignContact(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        phone_number="+971501234567",
        status="calling",
    )
    db.add(contact)
    await db.flush()
    attempt = CampaignContactAttempt(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        call_id=call.id,
        attempt_number=1,
        idempotency_key=f"delete-guard-{uuid4()}",
        provider="smallest",
        state="accepted",
    )
    db.add(attempt)
    await db.commit()

    blocked_by_attempt = await client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
    )
    assert blocked_by_attempt.status_code == 409
    assert "campaign attempt is unresolved" in blocked_by_attempt.json()["detail"]

    attempt.state = "completed"
    await db.commit()
    deleted = await client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204
