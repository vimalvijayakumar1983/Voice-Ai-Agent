import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantScopedModel


class Workflow(TenantScopedModel):
    __tablename__ = "workflows"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Workflows are drafts until an operator explicitly activates them. This
    # keeps partially-authored graphs out of campaign execution.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger_type: Mapped[str] = mapped_column(String(50))  # inbound_call, campaign, api
    config: Mapped[dict | None] = mapped_column(JSONB)

    # Relationships
    nodes = relationship(
        "WorkflowNode",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="WorkflowNode.position",
        passive_deletes=True,
    )


class WorkflowNode(TenantScopedModel):
    __tablename__ = "workflow_nodes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    node_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # greeting, gather_input, ai_conversation, transfer, hangup, condition, webhook
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Config examples:
    # greeting: {"message": "Hello!", "voice": "en-US-Standard-A"}
    # gather_input: {"prompt": "Press 1 for sales", "num_digits": 1, "timeout": 5}
    # ai_conversation: {"agent_id": "...", "max_duration": 300}
    # transfer: {"number": "+1234567890", "whisper": "Transferring a lead"}
    # condition: {"field": "digit", "operator": "eq", "value": "1", "then_node": 3, "else_node": 4}
    # webhook: {"url": "https://...", "method": "POST"}
    next_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Relationships
    workflow = relationship("Workflow", back_populates="nodes")
