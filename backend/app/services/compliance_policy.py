"""Runtime compliance decisions shared by direct and campaign dispatch."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compliance import ConsentRecord
from app.services.phone_numbers import normalize_e164

OUTBOUND_CONSENT_TYPES = frozenset({"outbound_call", "marketing_call"})


async def _is_latest_consent_revoked(
    db: AsyncSession,
    tenant_id: UUID,
    phone_number: str,
    consent_types: frozenset[str],
) -> bool:
    """Resolve append-only consent records using the latest row per scope."""
    canonical_number = normalize_e164(phone_number)
    if canonical_number is None:
        return False

    result = await db.execute(
        select(ConsentRecord)
        .where(
            ConsentRecord.tenant_id == tenant_id,
            ConsentRecord.phone_number == canonical_number,
            ConsentRecord.consent_type.in_(consent_types),
        )
        .order_by(ConsentRecord.created_at.desc(), ConsentRecord.id.desc())
    )
    latest_by_scope: dict[str, str] = {}
    for record in result.scalars():
        latest_by_scope.setdefault(record.consent_type, record.status)
    return any(status == "revoked" for status in latest_by_scope.values())


async def is_outbound_consent_revoked(
    db: AsyncSession,
    tenant_id: UUID,
    phone_number: str,
) -> bool:
    """Return whether the latest record for either outbound scope is revoked.

    Consent is append-only audit data. Absence is not treated as permission or
    denial here because workspaces may rely on another lawful basis; an
    explicit revocation is always enforced at the final provider-dispatch
    boundary. A later grant for the same scope supersedes its earlier record.
    """

    return await _is_latest_consent_revoked(
        db,
        tenant_id,
        phone_number,
        OUTBOUND_CONSENT_TYPES,
    )


async def is_recording_consent_revoked(
    db: AsyncSession,
    tenant_id: UUID,
    phone_number: str,
) -> bool:
    """Return whether the latest explicit recording-consent record is revoked.

    No record is policy-neutral: deployments can rely on a lawful basis tracked
    outside this table. A later grant supersedes an earlier revocation.
    """

    return await _is_latest_consent_revoked(
        db,
        tenant_id,
        phone_number,
        frozenset({"recording"}),
    )
