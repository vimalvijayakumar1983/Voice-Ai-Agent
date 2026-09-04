"""LiveKit adapter for Inworld's OpenAI-compatible Realtime API.

Inworld uses the OpenAI Realtime event protocol, but it intentionally uses a
different WebSocket path and Basic authentication.  Keeping those differences
in this small adapter lets the rest of VAV use LiveKit's production realtime
session, tool, interruption, transcript, and metrics machinery unchanged.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
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
        payload.setdefault("session", {}).setdefault("audio", {}).setdefault("output", {})[
            "model"
        ] = INWORLD_TTS_MODEL
        self._record_wire_telemetry(payload)
        return payload

    def _record_wire_telemetry(self, payload: dict[str, Any]) -> None:
        """Record content-free serialization diagnostics before WebSocket send.

        These fields deliberately say *serialized*, not provider accepted. The
        live readiness probe separately verifies Inworld's echoed session.
        """
        telemetry = getattr(self._realtime_model, "_wire_telemetry", None)
        if not isinstance(telemetry, dict):
            return
        sequence = int(telemetry.get("stt_session_update_serialized_sequence") or 0) + 1
        telemetry.update(
            {
                "stt_session_update_serialized_model": None,
                "stt_session_update_serialized_language": None,
                "stt_session_update_serialized_prompt_chars": None,
                "stt_session_update_serialized_lexicon_count": None,
                "stt_session_update_serialized_complete": False,
                "stt_session_update_provider_acknowledgement_observed": False,
                "stt_session_update_serialized_sequence": sequence,
                "stt_session_update_serialized_at": datetime.now(UTC).isoformat(),
            }
        )
        session = payload.get("session")
        audio = session.get("audio") if isinstance(session, dict) else None
        input_audio = audio.get("input") if isinstance(audio, dict) else None
        transcription = input_audio.get("transcription") if isinstance(input_audio, dict) else None
        if not isinstance(transcription, dict):
            return
        model = str(transcription.get("model") or "").strip()
        language = str(transcription.get("language") or "").strip()
        prompt = str(transcription.get("prompt") or "")
        telemetry.update(
            {
                "stt_session_update_serialized_model": model or None,
                "stt_session_update_serialized_language": language or None,
                "stt_session_update_serialized_prompt_chars": len(prompt),
                "stt_session_update_serialized_lexicon_count": (
                    self._realtime_model._recognition_lexicon_count
                ),
                # An explicit null is the provider's valid auto-detect wire
                # value. Completeness means the field was serialized, not that
                # a fixed language was selected.
                "stt_session_update_serialized_complete": bool(
                    model and "language" in transcription
                ),
            }
        )


class InworldRealtimeModel(openai.realtime.RealtimeModel):
    """Inworld speech-to-speech model presented as a LiveKit RealtimeModel."""

    def __init__(
        self,
        *,
        wire_telemetry: dict[str, Any] | None = None,
        recognition_lexicon_count: int = 0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._provider_label = "Inworld Realtime API"
        self._wire_telemetry = wire_telemetry
        self._recognition_lexicon_count = max(0, int(recognition_lexicon_count))

    @property
    def provider(self) -> str:
        return "inworld"

    def session(self, *, turn_detection_disabled: bool = False) -> llm.RealtimeSession:
        session = InworldRealtimeSession(self, turn_detection_disabled=turn_detection_disabled)
        self._sessions.add(session)
        return session
