import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

WorkflowTrigger = Literal["inbound_call", "campaign", "api"]
WorkflowNodeType = Literal[
    "greeting",
    "gather_input",
    "ai_conversation",
    "transfer",
    "hangup",
    "condition",
    "webhook",
]

MAX_WORKFLOW_NODES = 100
MAX_CONFIG_BYTES = 64 * 1024


def _validate_config_size(value: dict | None) -> dict | None:
    if value is not None and len(json.dumps(value, separators=(",", ":"))) > MAX_CONFIG_BYTES:
        raise ValueError(f"Configuration must be at most {MAX_CONFIG_BYTES} bytes")
    return value


def _validate_node_sequence(nodes: list["WorkflowNodeCreate"]) -> list["WorkflowNodeCreate"]:
    if not nodes:
        return nodes

    positions = [node.position for node in nodes]
    unique_positions = set(positions)
    if len(unique_positions) != len(positions):
        raise ValueError("Workflow node positions must be unique")

    first_position = min(positions)
    if first_position not in {0, 1}:
        raise ValueError("Workflow node positions must start at 0 or 1")
    if sorted(positions) != list(range(first_position, first_position + len(positions))):
        raise ValueError("Workflow node positions must be contiguous")

    for node in nodes:
        if node.node_type != "condition":
            continue
        for branch_key in ("then_node", "else_node"):
            branch_position = node.config.get(branch_key)
            if branch_position is None:
                continue
            if not isinstance(branch_position, int) or isinstance(branch_position, bool):
                raise ValueError(f"Condition {branch_key} must be a node position")
            if branch_position not in unique_positions:
                raise ValueError(f"Condition {branch_key} must reference a node in this workflow")

    return nodes


class WorkflowNodeCreate(BaseModel):
    position: int = Field(ge=0, le=MAX_WORKFLOW_NODES)
    node_type: WorkflowNodeType
    config: dict = Field(default_factory=dict)
    next_node_id: UUID | None = None

    @field_validator("config")
    @classmethod
    def validate_config_size(cls, value: dict) -> dict:
        return _validate_config_size(value) or {}


class WorkflowNodeResponse(BaseModel):
    id: UUID
    position: int
    node_type: str
    config: dict
    next_node_id: UUID | None

    model_config = {"from_attributes": True}


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str | None = Field(None, max_length=4000)
    agent_id: UUID | None = None
    trigger_type: WorkflowTrigger
    config: dict | None = None
    nodes: list[WorkflowNodeCreate] = Field(default_factory=list, max_length=MAX_WORKFLOW_NODES)
    is_active: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("Workflow name must contain at least 2 non-whitespace characters")
        return normalized

    @field_validator("config")
    @classmethod
    def validate_config_size(cls, value: dict | None) -> dict | None:
        return _validate_config_size(value)

    @field_validator("nodes")
    @classmethod
    def validate_nodes(cls, value: list[WorkflowNodeCreate]) -> list[WorkflowNodeCreate]:
        return _validate_node_sequence(value)

    @model_validator(mode="after")
    def validate_active_workflow(self):
        if self.is_active and not self.nodes:
            raise ValueError("An active workflow must contain at least one node")
        return self


class WorkflowUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=255)
    description: str | None = Field(None, max_length=4000)
    agent_id: UUID | None = None
    trigger_type: WorkflowTrigger | None = None
    config: dict | None = None
    is_active: bool | None = None
    nodes: list[WorkflowNodeCreate] | None = Field(None, max_length=MAX_WORKFLOW_NODES)

    @field_validator("name", "trigger_type", "is_active", mode="before")
    @classmethod
    def reject_null_for_required_columns(cls, value, info):
        if value is None:
            raise ValueError(f"{info.field_name} may not be null")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("Workflow name must contain at least 2 non-whitespace characters")
        return normalized

    @field_validator("config")
    @classmethod
    def validate_config_size(cls, value: dict | None) -> dict | None:
        return _validate_config_size(value)

    @field_validator("nodes")
    @classmethod
    def validate_nodes(
        cls, value: list[WorkflowNodeCreate] | None
    ) -> list[WorkflowNodeCreate] | None:
        return _validate_node_sequence(value) if value is not None else None


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
