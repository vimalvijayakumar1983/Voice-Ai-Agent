"""Workflow configuration isolation, validation, and lifecycle tests."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import Workflow, WorkflowNode


async def _create_agent(client, auth_headers, name: str = "Workflow Agent") -> str:
    response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": name,
            "system_prompt": "Help callers complete their request safely.",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_workflow_draft_linking_activation_and_lifecycle(client, auth_headers):
    agent_id = await _create_agent(client, auth_headers)
    response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={
            "name": "  Support routing  ",
            "agent_id": agent_id,
            "trigger_type": "inbound_call",
            "nodes": [
                {"position": 0, "node_type": "greeting", "config": {"message": "Hello"}},
                {"position": 1, "node_type": "hangup", "config": {}},
            ],
        },
    )

    assert response.status_code == 201
    workflow = response.json()
    assert workflow["name"] == "Support routing"
    assert workflow["is_active"] is False
    original_ids = [node["id"] for node in workflow["nodes"]]

    link_response = await client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=auth_headers,
        json={
            "nodes": [
                {
                    "position": 0,
                    "node_type": "greeting",
                    "config": {"message": "Hello"},
                    "next_node_id": original_ids[1],
                },
                {"position": 1, "node_type": "hangup", "config": {}},
            ]
        },
    )
    assert link_response.status_code == 200
    assert [node["id"] for node in link_response.json()["nodes"]] == original_ids
    assert link_response.json()["nodes"][0]["next_node_id"] == original_ids[1]

    activate_response = await client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=auth_headers,
        json={"is_active": True},
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["is_active"] is True

    edit_response = await client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=auth_headers,
        json={"name": "Unsafe live edit"},
    )
    assert edit_response.status_code == 409
    assert edit_response.json()["detail"] == "Deactivate the workflow before editing it"

    delete_response = await client.delete(
        f"/api/v1/workflows/{workflow['id']}", headers=auth_headers
    )
    assert delete_response.status_code == 409

    deactivate_response = await client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=auth_headers,
        json={"is_active": False},
    )
    assert deactivate_response.status_code == 200

    delete_response = await client.delete(
        f"/api/v1/workflows/{workflow['id']}", headers=auth_headers
    )
    assert delete_response.status_code == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        (
            {"name": "Bad trigger", "trigger_type": "cron"},
            "Input should be",
        ),
        (
            {
                "name": "Bad node",
                "trigger_type": "api",
                "nodes": [{"position": 0, "node_type": "script", "config": {}}],
            },
            "Input should be",
        ),
        (
            {
                "name": "Duplicate positions",
                "trigger_type": "api",
                "nodes": [
                    {"position": 0, "node_type": "greeting", "config": {}},
                    {"position": 0, "node_type": "hangup", "config": {}},
                ],
            },
            "positions must be unique",
        ),
        (
            {
                "name": "Position gap",
                "trigger_type": "api",
                "nodes": [
                    {"position": 0, "node_type": "greeting", "config": {}},
                    {"position": 2, "node_type": "hangup", "config": {}},
                ],
            },
            "positions must be contiguous",
        ),
        (
            {
                "name": "Bad condition",
                "trigger_type": "api",
                "nodes": [
                    {
                        "position": 0,
                        "node_type": "condition",
                        "config": {"then_node": 4},
                    }
                ],
            },
            "must reference a node in this workflow",
        ),
        (
            {"name": "Empty active", "trigger_type": "api", "is_active": True},
            "active workflow must contain at least one node",
        ),
    ],
)
async def test_workflow_schema_rejects_invalid_graphs(
    client, auth_headers, payload, error_fragment
):
    response = await client.post("/api/v1/workflows", headers=auth_headers, json=payload)

    assert response.status_code == 422
    assert error_fragment in str(response.json())


@pytest.mark.asyncio
async def test_workflow_rejects_dangling_and_self_node_references(client, auth_headers):
    dangling_response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={
            "name": "Dangling graph",
            "trigger_type": "api",
            "nodes": [
                {
                    "position": 0,
                    "node_type": "greeting",
                    "config": {},
                    "next_node_id": str(uuid4()),
                }
            ],
        },
    )
    assert dangling_response.status_code == 422
    assert (
        dangling_response.json()["detail"] == "next_node_id must reference a node in this workflow"
    )

    create_response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={
            "name": "Self linked graph",
            "trigger_type": "api",
            "nodes": [{"position": 0, "node_type": "greeting", "config": {}}],
        },
    )
    assert create_response.status_code == 201
    workflow = create_response.json()
    node_id = workflow["nodes"][0]["id"]

    self_link_response = await client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=auth_headers,
        json={
            "nodes": [
                {
                    "position": 0,
                    "node_type": "greeting",
                    "config": {},
                    "next_node_id": node_id,
                }
            ]
        },
    )
    assert self_link_response.status_code == 422
    assert self_link_response.json()["detail"] == "A workflow node cannot point to itself"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "trigger_type", "is_active"])
async def test_workflow_update_rejects_null_for_required_columns(client, auth_headers, field):
    create_response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={"name": "Null-safe workflow", "trigger_type": "api"},
    )
    assert create_response.status_code == 201
    workflow_id = create_response.json()["id"]

    update_response = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=auth_headers,
        json={field: None},
    )

    assert update_response.status_code == 422
    assert f"{field} may not be null" in str(update_response.json())

    get_response = await client.get(f"/api/v1/workflows/{workflow_id}", headers=auth_headers)
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "Null-safe workflow"
    assert get_response.json()["trigger_type"] == "api"
    assert get_response.json()["is_active"] is False


@pytest.mark.asyncio
async def test_workflow_agent_references_are_tenant_scoped(client, auth_headers, tenant, db):
    other_tenant = Tenant(name="Workflow Other Corp", slug="workflow-other-corp")
    db.add(other_tenant)
    await db.flush()
    foreign_agent = Agent(
        tenant_id=other_tenant.id,
        name="Foreign workflow agent",
        system_prompt="Only the other tenant may use this voice agent.",
    )
    db.add(foreign_agent)
    await db.commit()

    create_response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={
            "name": "Foreign workflow reference",
            "trigger_type": "api",
            "agent_id": str(foreign_agent.id),
        },
    )
    assert create_response.status_code == 404
    assert create_response.json()["detail"] == "Agent not found"

    own_workflow_response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={"name": "Owned draft", "trigger_type": "api"},
    )
    assert own_workflow_response.status_code == 201
    workflow_id = own_workflow_response.json()["id"]

    update_response = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=auth_headers,
        json={"agent_id": str(foreign_agent.id)},
    )
    assert update_response.status_code == 404
    assert update_response.json()["detail"] == "Agent not found"

    node_response = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=auth_headers,
        json={
            "nodes": [
                {
                    "position": 0,
                    "node_type": "ai_conversation",
                    "config": {"agent_id": str(foreign_agent.id)},
                }
            ]
        },
    )
    assert node_response.status_code == 404
    assert node_response.json()["detail"] == "Agent not found"


@pytest.mark.asyncio
async def test_workflow_node_reads_and_replacement_are_tenant_scoped(
    client, auth_headers, tenant, db
):
    other_tenant = Tenant(name="Node Other Corp", slug="node-other-corp")
    db.add(other_tenant)
    await db.flush()
    other_tenant_id = other_tenant.id
    workflow = Workflow(
        tenant_id=tenant.id,
        name="Tenant isolated nodes",
        trigger_type="api",
        is_active=False,
    )
    db.add(workflow)
    await db.flush()
    own_node = WorkflowNode(
        tenant_id=tenant.id,
        workflow_id=workflow.id,
        position=0,
        node_type="greeting",
        config={"message": "Owned"},
    )
    foreign_node = WorkflowNode(
        tenant_id=other_tenant.id,
        workflow_id=workflow.id,
        position=1,
        node_type="hangup",
        config={},
    )
    db.add_all([own_node, foreign_node])
    await db.commit()
    foreign_node_id = foreign_node.id

    get_response = await client.get(f"/api/v1/workflows/{workflow.id}", headers=auth_headers)
    assert get_response.status_code == 200
    assert [node["id"] for node in get_response.json()["nodes"]] == [str(own_node.id)]

    replace_response = await client.patch(
        f"/api/v1/workflows/{workflow.id}",
        headers=auth_headers,
        json={"nodes": []},
    )
    assert replace_response.status_code == 200
    assert replace_response.json()["nodes"] == []

    db.expire_all()
    foreign_result = await db.execute(
        select(WorkflowNode).where(
            WorkflowNode.id == foreign_node_id,
            WorkflowNode.tenant_id == other_tenant_id,
        )
    )
    assert foreign_result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_viewer_can_read_but_cannot_mutate_workflows(client, auth_headers, tenant, db):
    create_response = await client.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={"name": "Viewer protected draft", "trigger_type": "api"},
    )
    assert create_response.status_code == 201
    workflow_id = create_response.json()["id"]

    viewer = User(
        tenant_id=tenant.id,
        email="workflow-viewer@testcorp.com",
        hashed_password=hash_password("viewer-password"),
        full_name="Workflow Viewer",
        role="viewer",
    )
    db.add(viewer)
    await db.commit()
    viewer_token = create_access_token(viewer.id, tenant.id, viewer.role)
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

    read_response = await client.get(f"/api/v1/workflows/{workflow_id}", headers=viewer_headers)
    assert read_response.status_code == 200

    create_as_viewer = await client.post(
        "/api/v1/workflows",
        headers=viewer_headers,
        json={"name": "Forbidden viewer draft", "trigger_type": "api"},
    )
    update_as_viewer = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=viewer_headers,
        json={"name": "Forbidden viewer edit"},
    )
    delete_as_viewer = await client.delete(
        f"/api/v1/workflows/{workflow_id}", headers=viewer_headers
    )

    assert create_as_viewer.status_code == 403
    assert update_as_viewer.status_code == 403
    assert delete_as_viewer.status_code == 403
