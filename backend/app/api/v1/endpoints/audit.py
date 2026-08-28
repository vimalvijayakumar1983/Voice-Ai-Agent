"""Tenant-scoped audit log endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, require_role
from app.models.audit import AuditEvent
from app.schemas.audit import AuditEventResponse

router = APIRouter(prefix="/audit-events", tags=["Audit logs"])


@router.get("", response_model=list[AuditEventResponse])
async def list_audit_events(
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """List recent administrative activity for the current workspace."""
    query = select(AuditEvent).where(AuditEvent.tenant_id == current_user.tenant_id)
    if action:
        query = query.where(AuditEvent.action == action)
    if resource_type:
        query = query.where(AuditEvent.resource_type == resource_type)
    query = query.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    return [AuditEventResponse.model_validate(event) for event in result.scalars().all()]
