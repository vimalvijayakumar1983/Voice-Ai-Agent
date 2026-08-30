"""Tenant-safe CRUD for draft call-workflow configuration.

This module deliberately manages configuration only. Execution and visual graph
authoring require a separate, versioned workflow engine.
"""

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent
from app.models.workflow import Workflow, WorkflowNode
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowNodeCreate,
    WorkflowNodeResponse,
    WorkflowResponse,
    WorkflowUpdate,
)

router = APIRouter(prefix="/workflows", tags=["Workflows"])


async def _tenant_workflow(db: AsyncSession, workflow_id: UUID, tenant_id: UUID) -> Workflow:
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.tenant_id == tenant_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


async def _tenant_nodes(db: AsyncSession, workflow_id: UUID, tenant_id: UUID) -> list[WorkflowNode]:
    result = await db.execute(
        select(WorkflowNode)
        .where(
            WorkflowNode.workflow_id == workflow_id,
            WorkflowNode.tenant_id == tenant_id,
        )
        .order_by(WorkflowNode.position)
    )
    return list(result.scalars().all())


def _workflow_response(workflow: Workflow, nodes: Sequence[WorkflowNode]) -> WorkflowResponse:
    return WorkflowResponse(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        agent_id=workflow.agent_id,
        name=workflow.name,
        description=workflow.description,
        is_active=workflow.is_active,
        trigger_type=workflow.trigger_type,
        config=workflow.config,
        nodes=[WorkflowNodeResponse.model_validate(node) for node in nodes],
    )


def _node_agent_ids(nodes: Sequence[WorkflowNodeCreate | WorkflowNode]) -> set[UUID]:
    agent_ids: set[UUID] = set()
    for node in nodes:
        if node.node_type != "ai_conversation":
            continue
        raw_agent_id = node.config.get("agent_id")
        if raw_agent_id in (None, ""):
            continue
        try:
            agent_ids.add(UUID(str(raw_agent_id)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="AI conversation node agent_id must be a valid UUID",
            ) from exc
    return agent_ids


async def _validate_agent_references(
    db: AsyncSession,
    tenant_id: UUID,
    workflow_agent_id: UUID | None,
    nodes: Sequence[WorkflowNodeCreate | WorkflowNode],
) -> None:
    agent_ids = _node_agent_ids(nodes)
    if workflow_agent_id is not None:
        agent_ids.add(workflow_agent_id)
    if not agent_ids:
        return

    result = await db.execute(
        select(Agent.id).where(Agent.tenant_id == tenant_id, Agent.id.in_(agent_ids))
    )
    found_agent_ids = set(result.scalars().all())
    if found_agent_ids != agent_ids:
        # A tenant-neutral response avoids disclosing whether a foreign UUID exists.
        raise HTTPException(status_code=404, detail="Agent not found")


def _validate_next_node_references(nodes: Sequence[WorkflowNode]) -> None:
    node_ids = {node.id for node in nodes}
    for node in nodes:
        if node.next_node_id is None:
            continue
        if node.next_node_id == node.id:
            raise HTTPException(status_code=422, detail="A workflow node cannot point to itself")
        if node.next_node_id not in node_ids:
            raise HTTPException(
                status_code=422,
                detail="next_node_id must reference a node in this workflow",
            )


async def _replace_nodes(
    db: AsyncSession,
    workflow: Workflow,
    tenant_id: UUID,
    node_data: Sequence[WorkflowNodeCreate],
    existing_nodes: Sequence[WorkflowNode],
) -> list[WorkflowNode]:
    """Replace a graph while preserving node IDs at unchanged positions.

    Preserving IDs lets a client create nodes, read their generated IDs, then
    link them in a subsequent PATCH without accepting client-selected primary
    keys or reading node records outside the current tenant.
    """
    existing_by_position = {node.position: node for node in existing_nodes}
    final_nodes: list[WorkflowNode] = []

    for payload in node_data:
        node = existing_by_position.get(payload.position)
        if node is None:
            node = WorkflowNode(
                id=uuid4(),
                tenant_id=tenant_id,
                workflow_id=workflow.id,
                position=payload.position,
                node_type=payload.node_type,
                config=payload.config,
                next_node_id=payload.next_node_id,
            )
            db.add(node)
        else:
            node.node_type = payload.node_type
            node.config = payload.config
            node.next_node_id = payload.next_node_id
        final_nodes.append(node)

    _validate_next_node_references(final_nodes)

    final_node_ids = {node.id for node in final_nodes}
    for node in existing_nodes:
        if node.id not in final_node_ids:
            # existing_nodes came from a tenant-scoped query above.
            await db.delete(node)

    return sorted(final_nodes, key=lambda node: node.position)


def _ensure_activation_is_valid(nodes: Sequence[WorkflowNodeCreate | WorkflowNode]) -> None:
    if not nodes:
        raise HTTPException(status_code=422, detail="An active workflow must contain a node")


@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workflow)
        .where(Workflow.tenant_id == current_user.tenant_id)
        .order_by(Workflow.created_at.desc())
    )
    workflows = list(result.scalars().all())
    if not workflows:
        return []

    workflow_ids = [workflow.id for workflow in workflows]
    node_result = await db.execute(
        select(WorkflowNode)
        .where(
            WorkflowNode.tenant_id == current_user.tenant_id,
            WorkflowNode.workflow_id.in_(workflow_ids),
        )
        .order_by(WorkflowNode.position)
    )
    nodes_by_workflow: dict[UUID, list[WorkflowNode]] = defaultdict(list)
    for node in node_result.scalars().all():
        nodes_by_workflow[node.workflow_id].append(node)

    return [_workflow_response(workflow, nodes_by_workflow[workflow.id]) for workflow in workflows]


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    data: WorkflowCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    await _validate_agent_references(
        db,
        current_user.tenant_id,
        data.agent_id,
        data.nodes,
    )
    if data.is_active:
        _ensure_activation_is_valid(data.nodes)

    workflow = Workflow(
        tenant_id=current_user.tenant_id,
        name=data.name,
        description=data.description,
        agent_id=data.agent_id,
        trigger_type=data.trigger_type,
        config=data.config,
        is_active=data.is_active,
    )
    db.add(workflow)
    await db.flush()

    nodes = await _replace_nodes(
        db,
        workflow,
        current_user.tenant_id,
        data.nodes,
        existing_nodes=[],
    )
    await db.flush()
    return _workflow_response(workflow, nodes)


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workflow = await _tenant_workflow(db, workflow_id, current_user.tenant_id)
    nodes = await _tenant_nodes(db, workflow.id, current_user.tenant_id)
    return _workflow_response(workflow, nodes)


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: UUID,
    data: WorkflowUpdate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    workflow = await _tenant_workflow(db, workflow_id, current_user.tenant_id)
    existing_nodes = await _tenant_nodes(db, workflow.id, current_user.tenant_id)

    requested_fields = data.model_fields_set
    if workflow.is_active:
        is_noop = not requested_fields or (
            requested_fields == {"is_active"} and data.is_active is True
        )
        is_deactivation = requested_fields == {"is_active"} and data.is_active is False
        if is_noop:
            return _workflow_response(workflow, existing_nodes)
        if not is_deactivation:
            raise HTTPException(
                status_code=409,
                detail="Deactivate the workflow before editing it",
            )

    planned_agent_id = data.agent_id if "agent_id" in requested_fields else workflow.agent_id
    planned_nodes: Sequence[WorkflowNodeCreate | WorkflowNode] = (
        data.nodes if data.nodes is not None else existing_nodes
    )
    await _validate_agent_references(
        db,
        current_user.tenant_id,
        planned_agent_id,
        planned_nodes,
    )

    planned_active = data.is_active if data.is_active is not None else workflow.is_active
    if planned_active:
        _ensure_activation_is_valid(planned_nodes)

    update_data = data.model_dump(exclude_unset=True, exclude={"nodes"})
    for key, value in update_data.items():
        setattr(workflow, key, value)

    nodes = existing_nodes
    if data.nodes is not None:
        nodes = await _replace_nodes(
            db,
            workflow,
            current_user.tenant_id,
            data.nodes,
            existing_nodes,
        )
    else:
        _validate_next_node_references(nodes)

    await db.flush()
    return _workflow_response(workflow, nodes)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    workflow = await _tenant_workflow(db, workflow_id, current_user.tenant_id)
    if workflow.is_active:
        raise HTTPException(status_code=409, detail="Deactivate the workflow before deleting it")

    nodes = await _tenant_nodes(db, workflow.id, current_user.tenant_id)
    for node in nodes:
        await db.delete(node)
    await db.delete(workflow)
