"""Authenticated VAV dispatch envelopes for LiveKit browser sessions.

LiveKit agent-dispatch metadata is created by the API, but browser jobs also
carry an application-level MAC.  The worker verifies that MAC before using any
tenant, agent, call, room, or participant identifier.  Participant metadata is
never an authorization source.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import settings

BROWSER_DISPATCH_VERSION = 1
BROWSER_DISPATCH_MAX_TTL_SECONDS = 10 * 60
BROWSER_DISPATCH_CLOCK_SKEW_SECONDS = 30


@dataclass(frozen=True)
class BrowserDispatchEnvelope:
    tenant_id: UUID
    agent_id: UUID
    call_id: UUID
    room_name: str
    participant_identity: str
    issued_at: datetime
    expires_at: datetime


def _dispatch_key() -> bytes:
    value = settings.integration_encryption_key.strip() or settings.secret_key.strip()
    if not value:
        raise RuntimeError("VAV dispatch signing key is unavailable")
    # Domain separation prevents a dispatch MAC from being useful as another
    # application HMAC even when the deployment uses the compatibility key.
    return hmac.new(value.encode(), b"vav-livekit-browser-dispatch-v1", hashlib.sha256).digest()


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _signature(payload: dict[str, object]) -> str:
    digest = hmac.new(_dispatch_key(), _canonical(payload), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def create_browser_dispatch_metadata(
    *,
    tenant_id: UUID,
    agent_id: UUID,
    call_id: UUID,
    room_name: str,
    participant_identity: str,
    now: datetime | None = None,
    ttl_seconds: int = 5 * 60,
) -> str:
    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    bounded_ttl = min(max(int(ttl_seconds), 30), BROWSER_DISPATCH_MAX_TTL_SECONDS)
    payload: dict[str, object] = {
        "version": BROWSER_DISPATCH_VERSION,
        "channel": "browser",
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "call_id": str(call_id),
        "room_name": room_name,
        "participant_identity": participant_identity,
        "issued_at": int(issued_at.timestamp()),
        "expires_at": int((issued_at + timedelta(seconds=bounded_ttl)).timestamp()),
    }
    payload["signature"] = _signature(payload)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def verify_browser_dispatch_metadata(
    raw_metadata: str | None,
    *,
    expected_room_name: str,
    now: datetime | None = None,
) -> BrowserDispatchEnvelope:
    try:
        payload = json.loads(raw_metadata or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LiveKit browser dispatch metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("LiveKit browser dispatch metadata is invalid")

    expected_fields = {
        "version",
        "channel",
        "tenant_id",
        "agent_id",
        "call_id",
        "room_name",
        "participant_identity",
        "issued_at",
        "expires_at",
        "signature",
    }
    if set(payload) != expected_fields:
        raise RuntimeError("LiveKit browser dispatch metadata has an invalid schema")
    supplied_signature = payload.pop("signature", None)
    if not isinstance(supplied_signature, str) or not hmac.compare_digest(
        supplied_signature,
        _signature(payload),
    ):
        raise RuntimeError("LiveKit browser dispatch signature is invalid")
    if payload.get("version") != BROWSER_DISPATCH_VERSION or payload.get("channel") != "browser":
        raise RuntimeError("LiveKit browser dispatch version is unsupported")

    try:
        tenant_id = UUID(str(payload["tenant_id"]))
        agent_id = UUID(str(payload["agent_id"]))
        call_id = UUID(str(payload["call_id"]))
        issued_at = datetime.fromtimestamp(int(payload["issued_at"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["expires_at"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise RuntimeError("LiveKit browser dispatch contains an invalid identifier") from exc

    room_name = str(payload.get("room_name") or "")
    participant_identity = str(payload.get("participant_identity") or "")
    if room_name != expected_room_name:
        raise RuntimeError("LiveKit browser dispatch was replayed into a different room")
    if room_name != f"vav-browser-{call_id}":
        raise RuntimeError("LiveKit browser dispatch room does not match its call")
    if participant_identity != f"browser-{call_id}":
        raise RuntimeError("LiveKit browser participant does not match its call")

    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    if issued_at > observed_at + timedelta(seconds=BROWSER_DISPATCH_CLOCK_SKEW_SECONDS):
        raise RuntimeError("LiveKit browser dispatch was issued in the future")
    if expires_at <= observed_at - timedelta(seconds=BROWSER_DISPATCH_CLOCK_SKEW_SECONDS):
        raise RuntimeError("LiveKit browser dispatch has expired")
    if (
        expires_at <= issued_at
        or (expires_at - issued_at).total_seconds() > BROWSER_DISPATCH_MAX_TTL_SECONDS
    ):
        raise RuntimeError("LiveKit browser dispatch lifetime is invalid")

    return BrowserDispatchEnvelope(
        tenant_id=tenant_id,
        agent_id=agent_id,
        call_id=call_id,
        room_name=room_name,
        participant_identity=participant_identity,
        issued_at=issued_at,
        expires_at=expires_at,
    )
