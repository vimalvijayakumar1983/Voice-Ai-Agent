"""Provider media WebSocket endpoints."""

from uuid import UUID

from fastapi import APIRouter, Query, WebSocket

from app.realtime.auth import verify_media_token
from app.realtime.session import load_runtime_session, run_twilio_media_session

router = APIRouter(prefix="/realtime", tags=["Realtime Media"])


@router.websocket("/twilio/{call_id}")
async def twilio_media_socket(
    websocket: WebSocket,
    call_id: UUID,
    token: str = Query(min_length=20, max_length=1000),
):
    await websocket.accept()
    if not verify_media_token(token, call_id):
        await websocket.close(code=4401, reason="Invalid media capability")
        return
    config = await load_runtime_session(call_id)
    if config is None:
        await websocket.close(code=4403, reason="Runtime is not active")
        return
    try:
        await run_twilio_media_session(websocket, config)
    finally:
        try:
            await websocket.close(code=1000)
        except RuntimeError:
            pass
