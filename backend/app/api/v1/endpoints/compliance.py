"""Compliance endpoints - DNC, consent management."""

from datetime import UTC
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user
from app.models.compliance import ConsentRecord, DncEntry
from app.schemas.compliance import (
    ConsentRecordCreate,
    ConsentRecordResponse,
    DncCheckResponse,
    DncEntryCreate,
    DncEntryResponse,
)

router = APIRouter(prefix="/compliance", tags=["Compliance"])


# DNC Management
@router.get("/dnc", response_model=list[DncEntryResponse])
async def list_dnc_entries(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    result = await db.execute(
        select(DncEntry)
        .where(DncEntry.tenant_id == current_user.tenant_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return [DncEntryResponse.model_validate(d) for d in result.scalars().all()]


@router.post("/dnc", response_model=DncEntryResponse, status_code=status.HTTP_201_CREATED)
async def add_dnc_entry(
    data: DncEntryCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Check if already exists
    existing = await db.execute(
        select(DncEntry).where(
            DncEntry.tenant_id == current_user.tenant_id,
            DncEntry.phone_number == data.phone_number,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Number already on DNC list")

    entry = DncEntry(
        tenant_id=current_user.tenant_id,
        phone_number=data.phone_number,
        reason=data.reason,
        source=data.source,
        added_by=current_user.id,
    )
    db.add(entry)
    await db.flush()
    return DncEntryResponse.model_validate(entry)


@router.get("/dnc/check", response_model=DncCheckResponse)
async def check_dnc(
    phone_number: str = Query(...),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if a phone number is on the DNC list."""
    result = await db.execute(
        select(DncEntry).where(
            DncEntry.tenant_id == current_user.tenant_id,
            DncEntry.phone_number == phone_number,
        )
    )
    return DncCheckResponse(
        phone_number=phone_number,
        is_on_dnc=result.scalar_one_or_none() is not None,
    )


@router.delete("/dnc/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_dnc_entry(
    entry_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DncEntry).where(
            DncEntry.id == entry_id, DncEntry.tenant_id == current_user.tenant_id
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="DNC entry not found")
    await db.delete(entry)


# Consent Management
@router.get("/consent", response_model=list[ConsentRecordResponse])
async def list_consent_records(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    phone_number: str | None = None,
):
    query = select(ConsentRecord).where(ConsentRecord.tenant_id == current_user.tenant_id)
    if phone_number:
        query = query.where(ConsentRecord.phone_number == phone_number)
    result = await db.execute(query)
    return [ConsentRecordResponse.model_validate(c) for c in result.scalars().all()]


@router.post("/consent", response_model=ConsentRecordResponse, status_code=status.HTTP_201_CREATED)
async def create_consent_record(
    data: ConsentRecordCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime

    record = ConsentRecord(
        tenant_id=current_user.tenant_id,
        phone_number=data.phone_number,
        consent_type=data.consent_type,
        status=data.status,
        evidence=data.evidence,
        granted_at=datetime.now(UTC) if data.status == "granted" else None,
        revoked_at=datetime.now(UTC) if data.status == "revoked" else None,
    )
    db.add(record)
    await db.flush()
    return ConsentRecordResponse.model_validate(record)
