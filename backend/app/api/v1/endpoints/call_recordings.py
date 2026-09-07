"""Explicit caller-controlled recording for authenticated LiveKit browser calls."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from livekit import api
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, require_role
from app.models.call import Call
from app.services import r2_recording as recordings
from app.services.audit import record_audit_event
from app.services.browser_access import require_call_access

router = APIRouter(prefix="/call-recordings", tags=["Call recordings"])


class RecordingConsent(BaseModel):
    consent: Literal[True]
    notice_version: Literal["browser-recording-90d-v1"]
    model_config = {"extra": "forbid"}


def client():
    return api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


async def owned_call(db, user, call_id, *, lock=False, caller_only=False):
    await require_call_access(db, user, call_id)
    query = (
        select(Call)
        .where(Call.id == call_id, Call.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    call = (await db.execute(query)).scalar_one_or_none()
    if call is None:
        raise HTTPException(404, "Call not found")
    if call.provider != "livekit_webrtc":
        raise HTTPException(409, "Phone recording requires a separate caller-consent flow")
    if caller_only and (call.call_metadata or {}).get("browser_user_id") != str(user.id):
        raise HTTPException(403, "Only the participant who started this browser call can consent")
    return call


def save_state(call, state):
    call.call_metadata = {**(call.call_metadata or {}), "private_recording": state}
    # Only a marker, never a user-provided URL or arbitrary object path.
    call.provider_recording_url = "private-r2" if recordings.available(state) else None


def response(state):
    return {
        "state": state.get("state", "off"),
        "available": recordings.available(state),
        "expires_at": state.get("expires_at"),
        "retention_days": 90,
        "notice_version": recordings.NOTICE_VERSION,
    }


@router.get("/configuration")
async def configuration(user: CurrentUser = Depends(require_role("owner", "admin", "member"))):
    return {
        "enabled": recordings.configured(),
        "notice": recordings.NOTICE,
        "notice_version": recordings.NOTICE_VERSION,
        "retention_days": 90,
        "supported_transport": "livekit_webrtc",
    }


@router.post("/{call_id}/start")
async def start_recording(
    call_id: UUID,
    data: RecordingConsent,
    user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    if not recordings.configured():
        raise HTTPException(503, "Recording is not enabled")
    call = await owned_call(db, user, call_id, lock=True, caller_only=True)
    existing = recordings.recording_state(call.call_metadata)
    if existing:
        return response(existing)  # Never create a duplicate or restart after stop.
    if call.status != "in_progress":
        raise HTTPException(409, "Connect to the browser call before recording")
    room_name = f"vav-browser-{call.id}"
    if (call.call_metadata or {}).get("livekit_room") != room_name:
        raise HTTPException(409, "Invalid call-room binding")
    now = datetime.now(UTC)
    state = {
        "state": "preparing",
        "consent_user_id": str(user.id),
        "consent_at": now.isoformat(),
        "notice_version": data.notice_version,
        "expires_at": (now + timedelta(days=90)).isoformat(),
        "room_name": room_name,
    }
    save_state(call, state)
    await record_audit_event(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="call.recording_consent_granted",
        resource_type="call",
        resource_id=str(call.id),
        details={"notice_version": data.notice_version},
    )
    await db.commit()
    lk = client()
    info = None
    try:
        participants = await asyncio.wait_for(
            lk.room.list_participants(api.ListParticipantsRequest(room=room_name)), 10
        )
        if not any(p.identity == f"browser-{call_id}" for p in participants.participants):
            raise HTTPException(409, "Caller is no longer connected")
        info = await asyncio.wait_for(
            lk.egress.start_egress(recordings.egress_request(user.tenant_id, call_id, room_name)),
            timeout=30,
        )
    except (Exception, asyncio.CancelledError) as error:
        # An interrupted response may be ambiguous. Never retry StartEgress.
        call = await owned_call(db, user, call_id, lock=True, caller_only=True)
        save_state(call, {**state, "state": "unconfirmed"})
        await db.commit()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise HTTPException(
            502, "Recording start was not confirmed. End the call to stop any capture."
        ) from None
    finally:
        await lk.aclose()
    call = await owned_call(db, user, call_id, lock=True, caller_only=True)
    state = recordings.apply_provider_state({**state, "egress_id": info.egress_id}, info)
    save_state(call, state)
    await db.commit()
    return response(state)


@router.post("/{call_id}/stop")
async def stop_recording(
    call_id: UUID,
    user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    call = await owned_call(db, user, call_id, lock=True, caller_only=True)
    state = recordings.recording_state(call.call_metadata)
    if not state or state.get("state") in {"ready", "failed", "expired"}:
        return response(state)
    egress_id = state.get("egress_id")
    if not egress_id:
        raise HTTPException(409, "Recording is not yet confirmed. End the call to stop capture.")
    state = {**state, "state": "processing", "stopped_by_user_at": datetime.now(UTC).isoformat()}
    save_state(call, state)
    await record_audit_event(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="call.recording_stopped",
        resource_type="call",
        resource_id=str(call.id),
        details={},
    )
    await db.commit()
    lk = client()
    try:
        info = await asyncio.wait_for(
            lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id)), 15
        )
    except Exception:
        raise HTTPException(502, "Stop was not confirmed. End the call to stop capture.") from None
    finally:
        await lk.aclose()
    call = await owned_call(db, user, call_id, lock=True, caller_only=True)
    state = recordings.apply_provider_state(recordings.recording_state(call.call_metadata), info)
    save_state(call, state)
    await db.commit()
    return response(state)


@router.get("/{call_id}/status")
async def status(
    call_id: UUID,
    user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    call = await owned_call(db, user, call_id, lock=True)
    state = recordings.recording_state(call.call_metadata)
    egress_id = state.get("egress_id")
    if state and state.get("state") not in {"ready", "failed", "expired"}:
        await db.rollback()
        lk = client()
        try:
            query = (
                api.ListEgressRequest(egress_id=egress_id)
                if egress_id
                else api.ListEgressRequest(room_name=f"vav-browser-{call_id}")
            )
            listing = await asyncio.wait_for(lk.egress.list_egress(query), 5)
            candidates = [
                item
                for item in listing.items
                if (
                    item.egress_id == egress_id
                    if egress_id
                    else recordings.matches_request(item, user.tenant_id, call_id)
                )
            ]
            info = candidates[0] if len(candidates) == 1 else None
        except Exception:
            info = None
        finally:
            await lk.aclose()
        call = await owned_call(db, user, call_id, lock=True)
        state = recordings.recording_state(call.call_metadata)
        if info is not None and info.room_name == state.get("room_name"):
            state = recordings.apply_provider_state({**state, "egress_id": info.egress_id}, info)
    expiry = recordings.expires_at(state)
    if expiry and expiry <= datetime.now(UTC):
        state = {**state, "state": "expired"}
    if state:
        save_state(call, state)
        await db.commit()
    return response(state)


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Webhook too large")
    try:
        receiver = api.WebhookReceiver(
            api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
        )
        event = receiver.receive(raw.decode(), request.headers.get("authorization", ""))
    except Exception:
        raise HTTPException(401, "Invalid recording webhook") from None
    if event.event not in {"egress_started", "egress_updated", "egress_ended"}:
        return {"ok": True}
    info = event.egress_info
    if not info.room_name.startswith("vav-browser-"):
        return {"ok": True}
    try:
        call_id = UUID(info.room_name.removeprefix("vav-browser-"))
    except ValueError:
        return {"ok": True}
    call = (
        await db.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is not None and call.provider == "livekit_webrtc":
        state = recordings.recording_state(call.call_metadata)
        matches = state.get("egress_id") == info.egress_id or (
            not state.get("egress_id") and recordings.matches_request(info, call.tenant_id, call.id)
        )
        if matches and state.get("room_name") == info.room_name and state.get("consent_at"):
            save_state(
                call, recordings.apply_provider_state({**state, "egress_id": info.egress_id}, info)
            )
            await db.commit()
    return {"ok": True}
