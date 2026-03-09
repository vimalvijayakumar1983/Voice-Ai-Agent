from uuid import UUID

from pydantic import BaseModel


class WorkflowNodeCreate(BaseModel):
    position: int
    node_type: str
    config: dict
    next_node_id: UUID | None = None


class WorkflowNodeResponse(BaseModel):
    id: UUID
    position: int
    node_type: str
    config: dict
    next_node_id: UUID | None

    model_config = {"from_attributes": True}


class WorkflowCreate(BaseModel):
    name: str
    description: str | None = None
    agent_id: UUID | None = None
    trigger_type: str
    config: dict | None = None
    nodes: list[WorkflowNodeCreate] = []


class WorkflowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    agent_id: UUID | None = None
    trigger_type: str | None = None
    config: dict | None = None
    is_active: bool | None = None
    nodes: list[WorkflowNodeCreate] | None = None


class WorkflowResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    agent_id: UUID | None
    name: str
    description: str | None
    is_active: bool
    trigger_type: str
    config: dict | None
    nodes: list[WorkflowNodeResponse]

    model_config = {"from_attributes": True}
