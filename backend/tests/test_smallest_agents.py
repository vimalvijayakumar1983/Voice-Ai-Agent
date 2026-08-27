from dataclasses import dataclass, field

import pytest
from httpx import AsyncClient

from app.api.v1.endpoints import agents as agents_endpoint
from app.api.v1.endpoints import calls as calls_endpoint
from app.providers.smallest import BrowserSession


@dataclass
class FakeSmallestClient:
    is_configured: bool = True
    draft_calls: list[dict] = field(default_factory=list)

    async def create_agent(self, **_kwargs):
        return "smallest_agent_123"

    async def get_default_branch_id(self, _agent_id):
        return "main_branch_123"

    async def update_agent_draft(self, **kwargs):
        self.draft_calls.append(kwargs)
        return {"status": True}

    async def list_voices(self):
        return [
            {
                "voiceId": "emily",
                "displayName": "Emily",
                "tags": {
                    "language": ["English", "Hindi"],
                    "accent": "Indian",
                    "gender": "female",
                },
            }
        ]

    async def list_voice_clones(self):
        return [
            {
                "voiceId": "brand_voice",
                "displayName": "Brand Voice",
                "language": "English",
                "status": "completed",
                "modelIds": ["lightning-v3.1"],
            }
        ]

    async def publish_draft(self, **_kwargs):
        return {"state": "committed", "revision": {"_id": "revision_123"}}

    async def create_browser_session(self, **_kwargs):
        return BrowserSession(access_token="wct_test", expires_in=30, sample_rate=24000)

    async def start_outbound_call(self, **_kwargs):
        return "smallest_call_123"


@pytest.mark.asyncio
async def test_provision_sync_and_mint_browser_session(
    client: AsyncClient, auth_headers, monkeypatch
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(calls_endpoint, "get_smallest_client", lambda: fake)

    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Al Zaabi Receptionist",
            "system_prompt": "Be concise and helpful.",
            "greeting_message": "Welcome to Al Zaabi Group.",
            "supported_languages": ["en", "hi"],
        },
    )
    agent_id = created.json()["id"]

    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision", headers=auth_headers
    )
    assert provisioned.status_code == 200
    assert provisioned.json()["provider_agent_id"] == "smallest_agent_123"
    assert provisioned.json()["provider_revision_id"] == "revision_123"
    assert provisioned.json()["sync_status"] == "synced"
    assert fake.draft_calls[0]["supported_languages"] == ["en", "hi"]

    updated = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"system_prompt": "Updated prompt."},
    )
    assert updated.json()["sync_status"] == "dirty"

    synced = await client.post(f"/api/v1/agents/{agent_id}/smallest/sync", headers=auth_headers)
    assert synced.status_code == 200
    assert synced.json()["sync_status"] == "synced"

    session = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/session",
        headers=auth_headers,
        json={"variables": {"customer_name": "Vimal"}},
    )
    assert session.status_code == 200
    assert session.json() == {
        "access_token": "wct_test",
        "expires_in": 30,
        "sample_rate": 24000,
    }

    outbound = await client.post(
        "/api/v1/calls",
        headers=auth_headers,
        json={
            "agent_id": agent_id,
            "to_number": "+971501234567",
            "context": {"customer_name": "Aisha"},
        },
    )
    assert outbound.status_code == 201
    assert outbound.json()["provider"] == "smallest"
    assert outbound.json()["provider_call_sid"] == "smallest_call_123"
    assert outbound.json()["status"] == "ringing"


@pytest.mark.asyncio
async def test_catalog_includes_public_cloned_voices_languages_and_templates(
    client: AsyncClient, auth_headers, monkeypatch
):
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", FakeSmallestClient)

    response = await client.get("/api/v1/agents/provider/catalog", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert [voice["id"] for voice in payload["voices"]] == ["brand_voice", "emily"]
    assert {language["code"] for language in payload["languages"]} == {"en", "hi"}
    assert len(payload["templates"]) == 5
    assert payload["templates"][0]["id"] == "receptionist"


@pytest.mark.asyncio
async def test_agents_can_be_edited_with_multiple_languages(client: AsyncClient, auth_headers):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Support Agent",
            "system_prompt": "Help the customer solve their issue.",
            "language": "en",
            "supported_languages": ["en", "hi"],
        },
    )
    assert created.status_code == 201

    updated = await client.patch(
        f"/api/v1/agents/{created.json()['id']}",
        headers=auth_headers,
        json={
            "name": "Multilingual Support",
            "language": "hi",
            "supported_languages": ["hi", "en", "ml"],
            "voice_id": "emily",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "Multilingual Support"
    assert updated.json()["language"] == "hi"
    assert updated.json()["supported_languages"] == ["hi", "en", "ml"]
    assert updated.json()["voice_id"] == "emily"


@pytest.mark.asyncio
async def test_agent_language_configuration_is_validated_on_create_and_edit(
    client: AsyncClient, auth_headers
):
    invalid_create = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Invalid Agent",
            "system_prompt": "This prompt is long enough.",
            "language": "en",
            "supported_languages": ["hi"],
        },
    )
    assert invalid_create.status_code == 422

    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Tamil Agent",
            "system_prompt": "This prompt is long enough.",
            "language": "ta",
            "supported_languages": ["ta"],
        },
    )
    invalid_edit = await client.patch(
        f"/api/v1/agents/{created.json()['id']}",
        headers=auth_headers,
        json={"supported_languages": ["ta", "en"]},
    )
    assert invalid_edit.status_code == 422
    assert (
        invalid_edit.json()["detail"] == "Tamil cannot be combined with other supported languages"
    )
