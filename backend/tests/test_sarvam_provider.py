import base64
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.api.v1.endpoints import agents as agents_endpoint
from app.models.agent import Agent, AgentKnowledgeBinding, AgentRuntimeProfile, KnowledgeBase
from app.models.audit import AuditEvent
from app.models.provider_credential import ProviderCredential
from app.providers.sarvam import SarvamAIClient, SarvamAIError, sarvam_voice_catalog
from app.providers.smallest import SmallestAIError


@pytest.mark.asyncio
async def test_sarvam_bulbul_preview_contract_is_bounded_and_namespaced():
    requests: list[httpx.Request] = []
    wav = b"RIFF\x04\x00\x00\x00WAVE"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"request_id": "req-1", "audios": [base64.b64encode(wav).decode()]},
        )

    client = SarvamAIClient(
        api_key="sk_sarvam_test_key_123456",
        base_url="https://api.sarvam.ai",
        transport=httpx.MockTransport(handler),
    )
    audio = await client.synthesize_voice_preview(
        speaker="ishita",
        language="en",
    )

    assert audio == wav
    assert requests[0].url.path == "/text-to-speech"
    assert requests[0].headers["api-subscription-key"] == "sk_sarvam_test_key_123456"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "bulbul:v3"
    assert payload["language_code"] == "en-IN"
    assert payload["speaker"] == "ishita"
    assert any(voice["id"] == "sarvam:ishita" for voice in sarvam_voice_catalog())


@pytest.mark.asyncio
async def test_sarvam_preview_fails_closed_without_server_credential():
    client = SarvamAIClient(api_key="")

    with pytest.raises(SarvamAIError) as caught:
        await client.synthesize_voice_preview(speaker="ishita", language="en")

    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_workspace_sarvam_key_is_write_only_and_can_be_removed(
    client,
    auth_headers,
    db,
):
    api_key = "sk_workspace_sarvam_123456789"
    saved = await client.put(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
        json={"api_key": api_key},
    )

    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert saved.json()["source"] == "workspace"
    assert api_key not in saved.text

    credential = await db.scalar(select(ProviderCredential))
    assert credential is not None
    assert api_key not in credential.encrypted_config

    status = await client.get("/api/v1/agents/provider/status", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["providers"]["sarvam"]["source"] == "workspace"
    assert api_key not in status.text

    deleted = await client.delete(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["source"] in {"none", "platform"}


@pytest.mark.asyncio
async def test_legacy_sarvam_credential_mutations_invalidate_all_native_dependents(
    client,
    auth_headers,
    tenant,
    db,
):
    created = await client.put(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
        json={"api_key": "sk_initial_sarvam_runtime_key_123456789"},
    )
    assert created.status_code == 200
    sarvam_agent = Agent(
        tenant_id=tenant.id,
        name="Sarvam credential dependent",
        system_prompt="Help callers.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
    )
    elevenlabs_agent = Agent(
        tenant_id=tenant.id,
        name="ElevenLabs STT credential dependent",
        system_prompt="Help callers.",
        voice_provider="elevenlabs",
        voice_id="elevenlabs:test-voice",
    )
    db.add_all([sarvam_agent, elevenlabs_agent])
    await db.flush()
    profiles = [
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=sarvam_agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider="sarvam",
        ),
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=elevenlabs_agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider="elevenlabs",
        ),
    ]
    db.add_all(profiles)
    await db.commit()

    rotated = await client.put(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
        json={"api_key": "sk_rotated_sarvam_runtime_key_123456789"},
    )

    assert rotated.status_code == 200
    for profile in profiles:
        await db.refresh(profile)
        assert profile.enabled is False
        assert profile.status == "draft"
        profile.enabled = True
        profile.status = "active"
    await db.commit()

    deleted = await client.delete(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
    )

    assert deleted.status_code == 200
    for profile in profiles:
        await db.refresh(profile)
        assert profile.enabled is False
        assert profile.status == "draft"


@pytest.mark.asyncio
async def test_sarvam_agent_is_created_locally_without_touching_smallest(
    client,
    auth_headers,
):
    response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Indian English concierge",
            "system_prompt": "Answer customer questions in concise Indian English.",
            "voice_provider": "sarvam",
            "voice_id": "sarvam:ishita",
            "language": "en",
            "supported_languages": ["en", "hi", "ml"],
        },
    )

    assert response.status_code == 201
    assert response.json()["voice_provider"] == "sarvam"
    assert response.json()["voice_id"] == "sarvam:ishita"
    assert response.json()["provider_agent_id"] is None
    assert response.json()["sync_status"] == "local_only"


@pytest.mark.asyncio
async def test_confirmed_switch_archives_smallest_and_preserves_vav_agent_and_knowledge(
    client,
    auth_headers,
    monkeypatch,
    tenant,
    db,
):
    class DeprovisionClient:
        deleted_agent_ids: list[str] = []

        async def delete_agent(self, agent_id: str):
            self.deleted_agent_ids.append(agent_id)

    fake = DeprovisionClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    await client.put(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
        json={"api_key": "sk_workspace_switch_123456789"},
    )
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Royal Medical Healthcare Concierge",
            "system_prompt": "Help patients with safe healthcare concierge information.",
        },
    )
    agent_id = UUID(created.json()["id"])
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    agent.provider_agent_id = "smallest-royal-medical"
    agent.provider_branch_id = "smallest-main-branch"
    agent.provider_revision_id = "smallest-live-revision"
    agent.provider_config = {"publish": {"phase": "committed"}}
    agent.sync_status = "synced"
    agent.last_synced_at = datetime.now(UTC)
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Royal Medical knowledge",
        provider="smallest",
        provider_knowledge_base_id="smallest-royal-medical-kb",
        sync_status="ready",
        approval_status="approved",
    )
    db.add(knowledge)
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=tenant.id,
        agent_id=agent_id,
        knowledge_base_id=knowledge.id,
        provider="smallest",
        sync_status="synced",
    )
    db.add(binding)
    await db.commit()
    knowledge_id = knowledge.id

    unconfirmed = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"voice_provider": "sarvam", "voice_id": "sarvam:ishita"},
    )
    assert unconfirmed.status_code == 409
    assert fake.deleted_agent_ids == []

    switched = await client.patch(
        f"/api/v1/agents/{agent_id}?deprovision_existing_provider=true",
        headers=auth_headers,
        json={"voice_provider": "sarvam", "voice_id": "sarvam:ishita"},
    )

    assert switched.status_code == 200
    payload = switched.json()
    assert payload["voice_provider"] == "sarvam"
    assert payload["voice_id"] == "sarvam:ishita"
    assert payload["provider_agent_id"] is None
    assert payload["provider_branch_id"] is None
    assert payload["provider_revision_id"] is None
    assert payload["provider_config"] is None
    assert payload["sync_status"] == "local_only"
    assert payload["last_synced_at"] is None
    assert fake.deleted_agent_ids == ["smallest-royal-medical"]

    db.expire_all()
    assert await db.get(Agent, agent_id) is not None
    preserved_binding = await db.scalar(
        select(AgentKnowledgeBinding).where(AgentKnowledgeBinding.agent_id == agent_id)
    )
    assert preserved_binding is not None
    assert preserved_binding.knowledge_base_id == knowledge_id
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "agent.provider_switched",
            AuditEvent.resource_id == str(agent_id),
        )
    )
    assert audit is not None
    assert audit.details["provider_deprovisioned"] is True


@pytest.mark.asyncio
async def test_ambiguous_smallest_archival_keeps_original_provider_mapping(
    client,
    auth_headers,
    monkeypatch,
    db,
):
    class AmbiguousDeprovisionClient:
        async def delete_agent(self, _agent_id: str):
            raise SmallestAIError(
                "Connection ended after archival",
                status_code=504,
                ambiguous=True,
            )

    monkeypatch.setattr(
        agents_endpoint,
        "get_smallest_client",
        lambda: AmbiguousDeprovisionClient(),
    )
    await client.put(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
        json={"api_key": "sk_workspace_ambiguous_123456789"},
    )
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Retained Smallest concierge",
            "system_prompt": "Keep the original provider mapping on uncertain deletion.",
        },
    )
    agent_id = UUID(created.json()["id"])
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    agent.provider_agent_id = "smallest-retained"
    agent.provider_branch_id = "smallest-retained-branch"
    agent.sync_status = "synced"
    await db.commit()

    failed = await client.patch(
        f"/api/v1/agents/{agent_id}?deprovision_existing_provider=true",
        headers=auth_headers,
        json={"voice_provider": "sarvam", "voice_id": "sarvam:ishita"},
    )

    assert failed.status_code == 504
    assert "archival outcome is unknown" in failed.json()["detail"]
    retained = await client.get(f"/api/v1/agents/{agent_id}", headers=auth_headers)
    assert retained.status_code == 200
    assert retained.json()["voice_provider"] == "smallest"
    assert retained.json()["provider_agent_id"] == "smallest-retained"
    assert retained.json()["provider_branch_id"] == "smallest-retained-branch"
    assert retained.json()["sync_status"] == "synced"
