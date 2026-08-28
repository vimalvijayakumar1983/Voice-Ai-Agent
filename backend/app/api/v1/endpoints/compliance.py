"""Compliance endpoints - DNC, consent management."""

import hashlib
import hmac
from datetime import UTC
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.compliance import ConsentRecord, DncEntry
from app.schemas.compliance import (
    ConsentRecordCreate,
    ConsentRecordResponse,
    DncCheckResponse,
    DncEntryCreate,
    DncEntryResponse,
)
from app.services.audit import record_audit_event
from app.services.phone_numbers import (
    is_number_on_tenant_dnc,
    normalize_e164,
    tenant_phone_dnc_lock,
)

router = APIRouter(prefix="/compliance", tags=["Compliance"])


def _phone_audit_fingerprint(tenant_id: UUID, phone_number: str) -> str:
    """Create a workspace-bound identifier without copying the phone number."""
    message = f"{tenant_id}:{phone_number}".encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()


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
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    normalized_number = normalize_e164(data.phone_number)
    if not normalized_number:
        raise HTTPException(status_code=422, detail="Phone number must be a valid E.164 number")

    async with tenant_phone_dnc_lock(db, current_user.tenant_id, normalized_number):
        if await is_number_on_tenant_dnc(db, current_user.tenant_id, normalized_number):
            raise HTTPException(status_code=400, detail="Number already on DNC list")

        entry = DncEntry(
            tenant_id=current_user.tenant_id,
            phone_number=normalized_number,
            reason=data.reason,
            source=data.source,
            added_by=current_user.id,
        )
        db.add(entry)
        await db.flush()
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="compliance.dnc_added",
            resource_type="dnc_entry",
            resource_id=str(entry.id),
            details={
                "phone_fingerprint": _phone_audit_fingerprint(
                    current_user.tenant_id,
                    normalized_number,
                ),
                "source": data.source,
            },
        )
        await db.commit()
    return DncEntryResponse.model_validate(entry)


@router.get("/dnc/check", response_model=DncCheckResponse)
async def check_dnc(
    phone_number: str = Query(...),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if a phone number is on the DNC list."""
    normalized_number = normalize_e164(phone_number)
    if not normalized_number:
        raise HTTPException(status_code=422, detail="Phone number must be a valid E.164 number")
    return DncCheckResponse(
        phone_number=normalized_number,
        is_on_dnc=await is_number_on_tenant_dnc(
            db,
            current_user.tenant_id,
            normalized_number,
        ),
    )


@router.delete("/dnc/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_dnc_entry(
    entry_id: UUID,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DncEntry).where(
            DncEntry.id == entry_id, DncEntry.tenant_id == current_user.tenant_id
        )
    )
    entry_probe = result.scalar_one_or_none()
    if not entry_probe:
        raise HTTPException(status_code=404, detail="DNC entry not found")
    normalized_number = normalize_e164(entry_probe.phone_number)
    if not normalized_number:
        raise HTTPException(status_code=409, detail="DNC entry has an invalid phone number")

    # Re-read after acquiring the number guard because a concurrent delete may
    # have removed the probed row while this request was waiting.
    await db.commit()
    async with tenant_phone_dnc_lock(db, current_user.tenant_id, normalized_number):
        locked_result = await db.execute(
            select(DncEntry).where(
                DncEntry.id == entry_id,
                DncEntry.tenant_id == current_user.tenant_id,
            )
        )
        entry = locked_result.scalar_one_or_none()
        if not entry:
            raise HTTPException(status_code=404, detail="DNC entry not found")
        await db.delete(entry)
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action="compliance.dnc_removed",
            resource_type="dnc_entry",
            resource_id=str(entry.id),
            details={
                "phone_fingerprint": _phone_audit_fingerprint(
                    current_user.tenant_id,
                    normalized_number,
                )
            },
        )
        await db.commit()


# Consent Management
@router.get("/consent", response_model=list[ConsentRecordResponse])
async def list_consent_records(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    phone_number: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    query = select(ConsentRecord).where(ConsentRecord.tenant_id == current_user.tenant_id)
    if phone_number:
        normalized_number = normalize_e164(phone_number)
        if not normalized_number:
            raise HTTPException(status_code=422, detail="Phone number must be a valid E.164 number")
        query = query.where(ConsentRecord.phone_number == normalized_number)
    query = (
        query.order_by(ConsentRecord.created_at.desc(), ConsentRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    return [ConsentRecordResponse.model_validate(c) for c in result.scalars().all()]


@router.post("/consent", response_model=ConsentRecordResponse, status_code=status.HTTP_201_CREATED)
async def create_consent_record(
    data: ConsentRecordCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime

    # Use the same tenant+number lock as the final dial decision so a committed
    # revocation can never race past a direct or campaign provider request.
    async with tenant_phone_dnc_lock(db, current_user.tenant_id, data.phone_number):
        now = datetime.now(UTC)
        record = ConsentRecord(
            tenant_id=current_user.tenant_id,
            phone_number=data.phone_number,
            consent_type=data.consent_type,
            status=data.status,
            evidence=data.evidence,
            granted_at=now if data.status == "granted" else None,
            revoked_at=now if data.status == "revoked" else None,
        )
        db.add(record)
        await db.flush()
        await record_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            actor_user_id=current_user.id,
            action=f"consent.{data.status}",
            resource_type="consent_record",
            resource_id=str(record.id),
            details={
                "consent_type": data.consent_type,
                "status": data.status,
                # The canonical phone and evidence remain on the consent row;
                # avoid duplicating contact data into the administrative log.
            },
        )
        await db.commit()
    return ConsentRecordResponse.model_validate(record)
