"""LiveKit adapter for Inworld's OpenAI-compatible Realtime API.

Inworld uses the OpenAI Realtime event protocol, but it intentionally uses a
different WebSocket path and Basic authentication.  Keeping those differences
in this small adapter lets the rest of VAV use LiveKit's production realtime
session, tool, interruption, transcript, and metrics machinery unchanged.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import aiohttp
from livekit.agents import APIConnectionError, llm
from livekit.plugins import openai

from app.providers.inworld import INWORLD_TTS_MODEL, inworld_realtime_websocket_url


class InworldRealtimeSession(openai.realtime.RealtimeSession):
    """OpenAI-protocol session connected to Inworld's Realtime endpoint."""

    async def _create_ws_conn(self) -> aiohttp.ClientWebSocketResponse:
        url = inworld_realtime_websocket_url(
            self._opts.base_url,
            session_id=f"vav-{uuid4().hex}",
        )
        headers = {
            "Authorization": f"Basic {self._opts.api_key}",
            "User-Agent": "VAV LiveKit Agent",
        }
        started_at = time.perf_counter()
        websocket: aiohttp.ClientWebSocketResponse | None = None
        try:
            websocket = await asyncio.wait_for(
                self._realtime_model._ensure_http_session().ws_connect(
                    url=url,
                    headers=headers,
                ),
                self._opts.conn_options.timeout,
            )
            created: dict[str, Any] = await asyncio.wait_for(
                websocket.receive_json(),
                self._opts.conn_options.timeout,
            )
            if created.get("type") != "session.created":
                await websocket.close()
                detail = created.get("error") if isinstance(created.get("error"), dict) else {}
                message = str(detail.get("message") or "session was not created").strip()
                raise APIConnectionError(f"Inworld Realtime {message[:240]}")
            self._report_connection_acquired(time.perf_counter() - started_at)
            return websocket
        except (aiohttp.ClientError, ValueError, TypeError) as exc:
            if websocket is not None:
                await websocket.close()
            raise APIConnectionError("Inworld Realtime client connection error") from exc
        except TimeoutError as exc:
            if websocket is not None:
                await websocket.close()
            raise APIConnectionError(message="Inworld Realtime connection timed out") from exc

    def _create_session_update_event(self):
        event = super()._create_session_update_event()
        payload = event.model_dump(
            by_alias=True,
            exclude_unset=True,
            exclude_defaults=False,
        )
        # The shared OpenAI schema does not expose Inworld's output TTS model
        # extension. Add it only at the provider boundary so Realtime TTS-2 is
        # selected explicitly instead of relying on a changing provider default.
        payload.setdefault("session", {}).setdefault("audio", {}).setdefault(
            "output", {}
        )["model"] = INWORLD_TTS_MODEL
        return payload


class InworldRealtimeModel(openai.realtime.RealtimeModel):
    """Inworld speech-to-speech model presented as a LiveKit RealtimeModel."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._provider_label = "Inworld Realtime API"

    @property
    def provider(self) -> str:
        return "inworld"

    def session(self, *, turn_detection_disabled: bool = False) -> llm.RealtimeSession:
        session = InworldRealtimeSession(self, turn_detection_disabled=turn_detection_disabled)
        self._sessions.add(session)
        return session
