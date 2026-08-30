"""Role-boundary tests for operator and paid mutation paths."""

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token


@pytest.mark.asyncio
async def test_viewer_can_read_but_cannot_mutate_agents_calls_or_compliance(
    client: AsyncClient,
    db,
    tenant,
    user,
):
    user.role = "viewer"
    await db.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token(user.id, tenant.id, 'viewer')}",
        "Idempotency-Key": "viewer-rbac-call-0001",
    }

    # Viewer remains useful for monitoring and review.
    assert (await client.get("/api/v1/agents", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/compliance/dnc", headers=headers)).status_code == 200

    agent_id = uuid4()
    knowledge_id = uuid4()
    knowledge_source_id = uuid4()
    dnc_id = uuid4()
    mutations = [
        (
            "POST",
            "/api/v1/agents",
            {"name": "Viewer Agent", "system_prompt": "A valid prompt for RBAC testing."},
        ),
        ("PATCH", f"/api/v1/agents/{agent_id}", {"name": "Blocked edit"}),
        ("DELETE", f"/api/v1/agents/{agent_id}", None),
        ("POST", f"/api/v1/agents/{agent_id}/smallest/session", {"variables": {}}),
        (
            "POST",
            f"/api/v1/agents/{agent_id}/knowledge",
            {"name": "Private KB", "content_type": "text", "content": "secret"},
        ),
        ("DELETE", f"/api/v1/agents/{agent_id}/knowledge/{knowledge_id}", None),
        (
            "DELETE",
            f"/api/v1/knowledge/{knowledge_id}/sources/{knowledge_source_id}",
            None,
        ),
        (
            "POST",
            "/api/v1/calls",
            {"agent_id": str(agent_id), "to_number": "+971501234567"},
        ),
        ("POST", "/api/v1/compliance/dnc", {"phone_number": "+971501234567"}),
        ("DELETE", f"/api/v1/compliance/dnc/{dnc_id}", None),
        (
            "POST",
            "/api/v1/compliance/consent",
            {
                "phone_number": "+971501234567",
                "consent_type": "outbound",
                "status": "granted",
            },
        ),
    ]

    for method, path, payload in mutations:
        response = await client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 403, (method, path, response.text)
