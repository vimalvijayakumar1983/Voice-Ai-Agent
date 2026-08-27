from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from app.api.v1.endpoints import agents as agents_endpoint
from app.api.v1.endpoints import calls as calls_endpoint
from app.providers.smallest import BrowserSession


@dataclass
class FakeSmallestClient:
    is_configured: bool = True

    async def create_agent(self, **_kwargs):
        return "smallest_agent_123"

    async def get_default_branch_id(self, _agent_id):
        return "main_branch_123"

    async def update_agent_draft(self, **_kwargs):
        return {"status": True}

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
