"""Provider media WebSocket endpoints."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.realtime.auth import verify_media_token
from app.realtime.session import load_runtime_session, run_twilio_media_session

router = APIRouter(prefix="/realtime", tags=["Realtime Media"])
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
    config = await load_runtime_session(call_id)
    if config is None:
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
