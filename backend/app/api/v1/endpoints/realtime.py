"""Provider media WebSocket endpoints."""

import asyncio
import json
from uuid import UUID

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.realtime.auth import verify_media_token
from app.realtime.session import (
    RuntimeMediaSessionAlreadyClaimedError,
    claim_runtime_media_session,
    fail_inbound_runtime_start,
    load_runtime_session,
    run_twilio_media_session,
)

router = APIRouter(prefix="/realtime", tags=["Realtime Media"])
logger = structlog.get_logger()
TWILIO_START_TIMEOUT_SECONDS = 8
TWILIO_START_MESSAGE_LIMIT = 4


def _twilio_start_token(payload: object) -> str | None:
    if not isinstance(payload, dict) or payload.get("event") != "start":
        return None
    start = payload.get("start")
    if not isinstance(start, dict):
        return None
    parameters = start.get("customParameters")
    if not isinstance(parameters, dict):
        return None
    token = parameters.get("token")
    return token if isinstance(token, str) and token else None


def _twilio_start_identity(messages: list[str]) -> tuple[str, str] | None:
    """Extract Twilio's immutable StreamSid and provider CallSid."""

    for message in reversed(messages):
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("event") != "start":
            continue
        start = payload.get("start")
        if not isinstance(start, dict):
            return None
        stream_sid = str(start.get("streamSid") or payload.get("streamSid") or "").strip()
        provider_call_sid = str(start.get("callSid") or "").strip()
        if stream_sid and provider_call_sid:
            return stream_sid, provider_call_sid
        return None
    return None


async def _authenticate_twilio_stream(
    websocket: WebSocket,
    call_id: UUID,
) -> list[str] | None:
    buffered_messages: list[str] = []
    try:
        async with asyncio.timeout(TWILIO_START_TIMEOUT_SECONDS):
            for _ in range(TWILIO_START_MESSAGE_LIMIT):
                message = await websocket.receive_text()
                payload = json.loads(message)
                buffered_messages.append(message)
                if not isinstance(payload, dict):
                    break
                if payload.get("event") != "start":
                    continue
                if verify_media_token(_twilio_start_token(payload) or "", call_id):
                    return buffered_messages
                break
    except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError):
        pass
    try:
        await websocket.close(code=4401, reason="Invalid media capability")
    except RuntimeError:
        pass
    return None


@router.websocket("/twilio/{call_id}")
async def twilio_media_socket(
    websocket: WebSocket,
    call_id: UUID,
):
    await websocket.accept()
    initial_messages = await _authenticate_twilio_stream(websocket, call_id)
    if initial_messages is None:
        return
    stream_identity = _twilio_start_identity(initial_messages)
    if stream_identity is None:
        await websocket.close(code=4401, reason="Invalid Twilio stream identity")
        return
    stream_sid, provider_call_sid = stream_identity
    try:
        media_session_claim_id = await claim_runtime_media_session(
            call_id,
            stream_sid=stream_sid,
            provider_call_sid=provider_call_sid,
        )
    except RuntimeMediaSessionAlreadyClaimedError:
        logger.warning(
            "twilio_media_session_replay_rejected",
            call_id=str(call_id),
            stream_sid=stream_sid,
        )
        await websocket.close(code=4409, reason="Media session already active")
        return
    except Exception:
        logger.exception("twilio_media_session_claim_failed", call_id=str(call_id))
        await websocket.close(code=1011, reason="Runtime could not start")
        return
    if media_session_claim_id is None:
        await websocket.close(code=4403, reason="Runtime is not active")
        return
    try:
        config = await load_runtime_session(
            call_id,
            media_session_claim_id=media_session_claim_id,
        )
    except Exception:
        logger.exception("twilio_runtime_configuration_load_failed", call_id=str(call_id))
        try:
            await fail_inbound_runtime_start(
                call_id,
                reason="runtime_configuration_load_failed",
                media_session_claim_id=media_session_claim_id,
            )
        except Exception:
            logger.exception("twilio_runtime_start_terminalization_failed", call_id=str(call_id))
        await websocket.close(code=1011, reason="Runtime could not start")
        return
    if config is None:
        try:
            await fail_inbound_runtime_start(
                call_id,
                reason="runtime_configuration_unavailable",
                media_session_claim_id=media_session_claim_id,
            )
        except Exception:
            logger.exception("twilio_runtime_start_terminalization_failed", call_id=str(call_id))
        await websocket.close(code=4403, reason="Runtime is not active")
        return
    try:
        await run_twilio_media_session(
            websocket,
            config,
            initial_messages=initial_messages,
        )
    finally:
        try:
            await websocket.close(code=1000)
        except RuntimeError:
            pass
