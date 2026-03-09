"""Call workflow configuration endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.workflow import Workflow, WorkflowNode
from app.schemas.workflow import WorkflowCreate, WorkflowResponse, WorkflowUpdate

router = APIRouter(prefix="/workflows", tags=["Workflows"])


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
    return [WorkflowResponse.model_validate(w) for w in result.scalars().all()]


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    data: WorkflowCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workflow = Workflow(
        tenant_id=current_user.tenant_id,
        name=data.name,
        description=data.description,
        agent_id=data.agent_id,
        trigger_type=data.trigger_type,
        config=data.config,
    )
    db.add(workflow)
    await db.flush()

    # Create nodes
    for node_data in data.nodes:
        node = WorkflowNode(
            tenant_id=current_user.tenant_id,
            workflow_id=workflow.id,
            **node_data.model_dump(),
        )
        db.add(node)

    await db.flush()

    # Reload with nodes
    result = await db.execute(select(Workflow).where(Workflow.id == workflow.id))
    return WorkflowResponse.model_validate(result.scalar_one())


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workflow).where(
            Workflow.id == workflow_id, Workflow.tenant_id == current_user.tenant_id
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowResponse.model_validate(workflow)


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: UUID,
    data: WorkflowUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workflow).where(
            Workflow.id == workflow_id, Workflow.tenant_id == current_user.tenant_id
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    update_data = data.model_dump(exclude_unset=True, exclude={"nodes"})
    for key, value in update_data.items():
        setattr(workflow, key, value)

    # Replace nodes if provided
    if data.nodes is not None:
        # Delete existing nodes
        existing = await db.execute(
            select(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id)
        )
        for node in existing.scalars().all():
            await db.delete(node)

        # Create new nodes
        for node_data in data.nodes:
            node = WorkflowNode(
                tenant_id=current_user.tenant_id,
                workflow_id=workflow.id,
                **node_data.model_dump(),
            )
            db.add(node)

    await db.flush()
    result = await db.execute(select(Workflow).where(Workflow.id == workflow.id))
    return WorkflowResponse.model_validate(result.scalar_one())


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workflow).where(
            Workflow.id == workflow_id, Workflow.tenant_id == current_user.tenant_id
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await db.delete(workflow)
