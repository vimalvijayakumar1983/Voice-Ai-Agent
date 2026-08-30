"""Phone-number normalization helpers used by compliance checks."""

import asyncio
import hashlib
import re
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compliance import DncEntry

_PHONE_FORMATTING = re.compile(r"[\s().-]")
_LOCAL_DNC_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def normalize_e164(phone_number: str) -> str | None:
    """Return a canonical E.164 number when the input represents one.

    DNC imports commonly contain spaces, dashes, or an international ``00``
    prefix. Normalizing both operands prevents those harmless formatting
    differences from bypassing suppression checks. Local numbers without a
    country code cannot be safely inferred and are therefore not treated as
    E.164 numbers.
    """
    normalized = _PHONE_FORMATTING.sub("", phone_number.strip())
    if normalized.startswith("00"):
        normalized = f"+{normalized[2:]}"

    digits = normalized[1:] if normalized.startswith("+") else ""
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return f"+{digits}"


async def is_number_on_tenant_dnc(
    db: AsyncSession,
    tenant_id: UUID,
    phone_number: str,
) -> bool:
    """Check a tenant's DNC registry using canonical E.164 comparison."""
    normalized_target = normalize_e164(phone_number)
    if not normalized_target:
        return False

    return normalized_target in await normalized_tenant_dnc_numbers(db, tenant_id)


async def normalized_tenant_dnc_numbers(
    db: AsyncSession,
    tenant_id: UUID,
) -> set[str]:
    """Load one tenant's DNC registry as canonical E.164 values."""

    result = await db.execute(select(DncEntry.phone_number).where(DncEntry.tenant_id == tenant_id))
    return {
        normalized
        for stored_number in result.scalars()
        if (normalized := normalize_e164(stored_number)) is not None
    }


def _tenant_phone_lock_identity(tenant_id: UUID, phone_number: str) -> str:
    return f"{tenant_id}:{phone_number}"


@asynccontextmanager
async def tenant_phone_dnc_lock(
    db: AsyncSession,
    tenant_id: UUID,
    phone_number: str,
) -> AsyncIterator[str]:
    """Serialize DNC mutations and the final provider dispatch decision.

    PostgreSQL uses a transaction-scoped advisory lock derived from the tenant
    and canonical E.164 number. SQLite and other local test databases use the
    same identity with an in-process lock. The caller must commit or roll back
    the supplied session before leaving the context; the defensive rollback in
    ``finally`` prevents a forgotten transaction from retaining the database
    lock after the local guard is released.
    """
    canonical_number = normalize_e164(phone_number)
    if canonical_number is None:
        raise ValueError("Phone number must be a valid E.164 number")

    identity = _tenant_phone_lock_identity(tenant_id, canonical_number)
    dialect_name = db.get_bind().dialect.name
    local_lock: asyncio.Lock | None = None
    if dialect_name != "postgresql":
        local_lock = _LOCAL_DNC_LOCKS.get(identity)
        if local_lock is None:
            local_lock = asyncio.Lock()
            _LOCAL_DNC_LOCKS[identity] = local_lock
        await local_lock.acquire()

    try:
        if dialect_name == "postgresql":
            lock_id = int.from_bytes(
                hashlib.sha256(identity.encode()).digest()[:8],
                "big",
                signed=True,
            )
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
        yield canonical_number
    finally:
        try:
            if db.in_transaction():
                await db.rollback()
        finally:
            if local_lock is not None:
                local_lock.release()
