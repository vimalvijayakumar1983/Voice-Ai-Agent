"""Agent endpoint tests."""

import pytest
from httpx import AsyncClient


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
    assert data["model_provider"] == "openai"


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
