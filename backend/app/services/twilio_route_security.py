"""Tenant-safe ownership checks for self-service Twilio DID routes.

An assigned number is only a routing preference; it is not proof that a
workspace owns the number.  The authenticated Twilio account is the security
boundary.  This module deliberately ignores platform fallback credentials for
self-service routes so a tenant cannot inherit a shared platform identity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentRuntimeProfile
from app.services.provider_credentials import ProviderCredentialError, load_provider_config


@dataclass(frozen=True)
class TwilioRouteCredential:
    account_sid: str
    auth_token: str


class TwilioRouteVerificationError(RuntimeError):
    """The workspace credential or assigned DID could not be verified."""


TWILIO_ROUTE_PROBE_TIMEOUT_SECONDS = 6.0
TWILIO_ROUTE_PROBE_CONCURRENCY = 5
TWILIO_ROUTE_VERIFICATION_VERSION = 1
TWILIO_ROUTE_VERIFICATION_KEY = "twilio_route_verification"


def twilio_route_lock_key(account_sid: str, number: str) -> str:
    """Return the cross-tenant identity used by PostgreSQL route locks."""
    return f"twilio-route:{account_sid}:{number}"


def twilio_callback_credential_fingerprint(credential: TwilioRouteCredential) -> str:
    """Return a non-reversible identity for one callback-signing credential."""
    value = f"twilio-callback-v1:{credential.account_sid}:{credential.auth_token}"
    return hashlib.sha256(value.encode()).hexdigest()


def twilio_route_verification_fingerprint(
    credential: TwilioRouteCredential,
    assigned_numbers: list[str],
    *,
    expected_voice_url: str,
    expected_voice_method: str = "POST",
) -> str:
    """Bind verification to the exact credential secret and complete DID set."""
    payload = json.dumps(
        {
            "account_sid": credential.account_sid,
            "auth_token": credential.auth_token,
            "assigned_numbers": sorted(set(assigned_numbers)),
            "expected_voice_method": expected_voice_method.upper(),
            "expected_voice_url": expected_voice_url,
            "version": TWILIO_ROUTE_VERIFICATION_VERSION,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def twilio_route_verification_is_current(
    profile: AgentRuntimeProfile,
    credential: TwilioRouteCredential,
    *,
    expected_voice_url: str,
    expected_voice_method: str = "POST",
) -> bool:
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    verification = runtime_config.get(TWILIO_ROUTE_VERIFICATION_KEY)
    return bool(
        isinstance(verification, dict)
        and verification.get("version") == TWILIO_ROUTE_VERIFICATION_VERSION
        and verification.get("fingerprint")
        == twilio_route_verification_fingerprint(
            credential,
            list(profile.assigned_numbers or []),
            expected_voice_url=expected_voice_url,
            expected_voice_method=expected_voice_method,
        )
        and verification.get("verified_at")
    )


def mark_twilio_route_verified(
    profile: AgentRuntimeProfile,
    credential: TwilioRouteCredential,
    *,
    expected_voice_url: str,
    expected_voice_method: str = "POST",
    verified_at: datetime | None = None,
) -> None:
    runtime_config = profile.runtime_config if isinstance(profile.runtime_config, dict) else {}
    profile.runtime_config = {
        **runtime_config,
        TWILIO_ROUTE_VERIFICATION_KEY: {
            "version": TWILIO_ROUTE_VERIFICATION_VERSION,
            "fingerprint": twilio_route_verification_fingerprint(
                credential,
                list(profile.assigned_numbers or []),
                expected_voice_url=expected_voice_url,
                expected_voice_method=expected_voice_method,
            ),
            "verified_at": (verified_at or datetime.now(UTC)).isoformat(),
        },
    }


async def load_workspace_twilio_route_credential(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    for_update: bool = False,
) -> TwilioRouteCredential | None:
    """Load a complete tenant-owned Twilio credential without platform fallback."""
    config = await load_provider_config(
        db,
        tenant_id,
        "twilio",
        for_update=for_update,
    )
    account_sid = str((config or {}).get("account_sid") or "").strip()
    auth_token = str((config or {}).get("auth_token") or "").strip()
    if not account_sid or not auth_token:
        return None
    return TwilioRouteCredential(account_sid=account_sid, auth_token=auth_token)


async def lock_twilio_route_claims(
    db: AsyncSession,
    *,
    credential: TwilioRouteCredential,
    assigned_numbers: list[str],
) -> None:
    """Serialize all claims for one Twilio account and DID on PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        return
    for number in sorted(set(assigned_numbers)):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:route_key, 0))"),
            {"route_key": twilio_route_lock_key(credential.account_sid, number)},
        )


async def verify_twilio_route_ownership(
    *,
    credential: TwilioRouteCredential,
    assigned_numbers: list[str],
    expected_voice_url: str,
    expected_voice_method: str = "POST",
    timeout: float = TWILIO_ROUTE_PROBE_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Prove that valid account credentials own every assigned inbound DID.

    Twilio's filtered IncomingPhoneNumbers endpoint simultaneously validates
    the account credential and number ownership. Requests run with bounded
    concurrency and one total deadline so a readiness check cannot stall an
    API worker for one timeout per configured number.
    """
    numbers = sorted(set(assigned_numbers))
    if not numbers:
        raise TwilioRouteVerificationError("No Twilio number is assigned")

    timeout = max(1.0, min(float(timeout), TWILIO_ROUTE_PROBE_TIMEOUT_SECONDS))
    endpoint = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{credential.account_sid}/IncomingPhoneNumbers.json"
    )
    semaphore = asyncio.Semaphore(TWILIO_ROUTE_PROBE_CONCURRENCY)

    async with httpx.AsyncClient(
        auth=httpx.BasicAuth(credential.account_sid, credential.auth_token),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        transport=transport,
    ) as client:

        async def verify_number(number: str) -> None:
            async with semaphore:
                response = await client.get(
                    endpoint,
                    params={"PhoneNumber": number, "PageSize": "1"},
                )
            if response.status_code in {401, 403}:
                raise TwilioRouteVerificationError(
                    "Twilio rejected the workspace account SID or auth token"
                )
            if response.status_code != 200:
                raise TwilioRouteVerificationError(
                    f"Twilio route verification returned HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise TwilioRouteVerificationError(
                    "Twilio route verification returned an invalid response"
                ) from exc
            records = payload.get("incoming_phone_numbers") if isinstance(payload, dict) else None
            matching_record = next(
                (
                    record
                    for record in (records if isinstance(records, list) else [])
                    if isinstance(record, dict)
                    and str(record.get("account_sid") or "") == credential.account_sid
                    and str(record.get("phone_number") or "") == number
                ),
                None,
            )
            if matching_record is None:
                raise TwilioRouteVerificationError(
                    f"Assigned number {number} is not owned by the configured Twilio account"
                )
            if str(matching_record.get("voice_application_sid") or "").strip():
                raise TwilioRouteVerificationError(
                    f"Assigned number {number} uses a TwiML Application; configure the "
                    "VAV inbound webhook directly"
                )
            configured_voice_url = str(matching_record.get("voice_url") or "").strip()
            configured_voice_method = str(matching_record.get("voice_method") or "").upper()
            if (
                configured_voice_url != expected_voice_url
                or configured_voice_method != expected_voice_method.upper()
            ):
                raise TwilioRouteVerificationError(
                    f"Assigned number {number} is not configured for the VAV inbound POST webhook"
                )

        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(verify_number(number) for number in numbers),
                    return_exceptions=True,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise TwilioRouteVerificationError("Twilio route verification timed out") from exc
        for result in results:
            if isinstance(result, TwilioRouteVerificationError):
                raise result
            if isinstance(result, httpx.HTTPError):
                raise TwilioRouteVerificationError(
                    "Twilio route verification could not reach Twilio"
                ) from result
            if isinstance(result, BaseException):
                raise TwilioRouteVerificationError("Twilio route verification failed") from result


async def active_twilio_route_conflicts(
    db: AsyncSession,
    *,
    agent_id: UUID,
    account_sid: str,
    assigned_numbers: list[str],
    expected_voice_url: str,
    expected_voice_method: str = "POST",
    verify_legacy_claims: bool = False,
) -> list[Agent]:
    """Find active claims for the same authenticated Twilio account and DID."""
    numbers = set(assigned_numbers)
    if not numbers:
        return []
    rows = (
        await db.execute(
            select(AgentRuntimeProfile, Agent)
            .join(
                Agent,
                (Agent.id == AgentRuntimeProfile.agent_id)
                & (Agent.tenant_id == AgentRuntimeProfile.tenant_id),
            )
            .where(
                AgentRuntimeProfile.agent_id != agent_id,
                AgentRuntimeProfile.enabled.is_(True),
                AgentRuntimeProfile.status == "active",
                AgentRuntimeProfile.telephony_provider == "twilio",
                Agent.is_active.is_(True),
            )
        )
    ).all()

    credentials_by_tenant: dict[UUID, TwilioRouteCredential | None] = {}
    conflicts: list[Agent] = []
    legacy_claims: list[tuple[Agent, TwilioRouteCredential, list[str]]] = []
    for profile, candidate_agent in rows:
        overlapping_numbers = sorted(numbers.intersection(profile.assigned_numbers or []))
        if not overlapping_numbers:
            continue
        if candidate_agent.tenant_id not in credentials_by_tenant:
            try:
                credentials_by_tenant[
                    candidate_agent.tenant_id
                ] = await load_workspace_twilio_route_credential(
                    db,
                    candidate_agent.tenant_id,
                )
            except ProviderCredentialError:
                # An unreadable credential cannot authenticate an inbound
                # route. Credential restoration is separately blocked while
                # routes remain active, so skipping it cannot create a future
                # duplicate claim.
                credentials_by_tenant[candidate_agent.tenant_id] = None
        candidate_credential = credentials_by_tenant[candidate_agent.tenant_id]
        if candidate_credential is None or candidate_credential.account_sid != account_sid:
            continue
        if twilio_route_verification_is_current(
            profile,
            candidate_credential,
            expected_voice_url=expected_voice_url,
            expected_voice_method=expected_voice_method,
        ):
            conflicts.append(candidate_agent)
            continue
        if not verify_legacy_claims:
            continue
        legacy_claims.append((candidate_agent, candidate_credential, overlapping_numbers))
    if not legacy_claims:
        return conflicts

    # One bounded parallel proof prevents N legacy profiles from holding an API
    # connection (and a DB transaction in the caller) for N * timeout seconds.
    # A stale unrelated DID is deliberately excluded from each proof.
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    verify_twilio_route_ownership(
                        credential=credential,
                        assigned_numbers=overlapping_numbers,
                        expected_voice_url=expected_voice_url,
                        expected_voice_method=expected_voice_method,
                    )
                    for _agent, credential, overlapping_numbers in legacy_claims
                ),
                return_exceptions=True,
            ),
            timeout=TWILIO_ROUTE_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # An indeterminate ownership check cannot safely declare the contested
        # account+DID free. The activating route's own live proof will also fail
        # closed during a provider outage.
        conflicts.extend(agent for agent, _credential, _numbers in legacy_claims)
        return conflicts
    for (candidate_agent, _credential, _numbers), result in zip(
        legacy_claims,
        results,
        strict=True,
    ):
        if result is None:
            conflicts.append(candidate_agent)
        # A self-asserted legacy claim whose credential cannot prove the
        # contested DID has no authority to block a newly verified owner.
    return conflicts
