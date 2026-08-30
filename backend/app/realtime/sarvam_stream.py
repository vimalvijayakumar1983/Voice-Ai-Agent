"""Sarvam Saaras realtime STT and Bulbul v3 streaming TTS clients."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect


class SarvamStreamError(RuntimeError):
    pass


def _websocket_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    language_code: str | None = None


def parse_transcript_event(payload: dict) -> TranscriptEvent | None:
    event_type = str(payload.get("event") or payload.get("type") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    transcript = data.get("transcript") or data.get("text")
    if isinstance(transcript, dict):
        transcript = transcript.get("text") or transcript.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    is_final = event_type in {
        "transcript.final",
        "transcript_final",
        "final_transcript",
    } or bool(data.get("is_final") or data.get("final"))
    return TranscriptEvent(
        text=transcript.strip(),
        is_final=is_final,
        language_code=(
            data.get("language_code") if isinstance(data.get("language_code"), str) else None
        ),
    )


def is_speech_start(payload: dict) -> bool:
    return str(payload.get("event") or payload.get("type") or "") in {
        "vad.speech_start",
        "speech_start",
        "speech_started",
    }


class SarvamSTTStream:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        language_code: str,
        sample_rate: int = 8000,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.language_code = language_code
        self.sample_rate = sample_rate
        self.connection: ClientConnection | None = None

    async def __aenter__(self):
        query = urlencode(
            {
                "model": "saaras:v3-realtime",
                "language_code": self.language_code,
                "stream_type": "fast",
                "mode": "transcribe",
                "endpointing": "vad",
                "encoding": "mulaw",
                "sample_rate": self.sample_rate,
                "threshold": 0.3,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "min_speech_duration_ms": 250,
            }
        )
        url = f"{_websocket_origin(self.base_url)}/speech-to-text-realtime/ws?{query}"
        try:
            self.connection = await connect(
                url,
                additional_headers={"API-SUBSCRIPTION-KEY": self.api_key},
                compression=None,
                open_timeout=10,
                max_size=2 * 1024 * 1024,
            )
        except Exception as exc:
            raise SarvamStreamError("Sarvam realtime transcription could not connect") from exc
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        if self.connection is not None:
            await self.connection.close()

    async def send_audio(self, encoded_audio: str) -> None:
        if self.connection is None:
            raise SarvamStreamError("Sarvam transcription stream is not connected")
        await self.connection.send(json.dumps({"event": "audio_input", "audio": encoded_audio}))

    async def events(self) -> AsyncIterator[dict]:
        if self.connection is None:
            raise SarvamStreamError("Sarvam transcription stream is not connected")
        async for message in self.connection:
            if not isinstance(message, str):
                continue
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


async def stream_tts_audio(
    *,
    api_key: str,
    base_url: str,
    text: str,
    speaker: str,
    language_code: str,
    pace: float,
) -> AsyncIterator[str]:
    url = (
        f"{_websocket_origin(base_url)}/text-to-speech/ws"
        "?model=bulbul:v3&send_completion_event=true"
    )
    try:
        async with connect(
            url,
            additional_headers={"Api-Subscription-Key": api_key},
            compression=None,
            open_timeout=10,
            max_size=4 * 1024 * 1024,
        ) as connection:
            await connection.send(
                json.dumps(
                    {
                        "type": "config",
                        "data": {
                            "speaker": speaker,
                            "language_code": language_code,
                            "pace": max(0.5, min(float(pace), 2.0)),
                            "min_buffer_size": 50,
                            "max_chunk_length": 200,
                            "output_audio_codec": "mulaw",
                        },
                    }
                )
            )
            await connection.send(json.dumps({"type": "text", "data": {"text": text}}))
            await connection.send(json.dumps({"type": "flush"}))
            async for message in connection:
                if not isinstance(message, str):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                event_type = payload.get("type") if isinstance(payload, dict) else None
                data = payload.get("data") if isinstance(payload, dict) else None
                if event_type == "audio" and isinstance(data, dict):
                    audio = data.get("audio")
                    if isinstance(audio, str):
                        try:
                            base64.b64decode(audio, validate=True)
                        except ValueError:
                            continue
                        yield audio
                if event_type in {"final", "completion", "completed"}:
                    return
    except SarvamStreamError:
        raise
    except Exception as exc:
        raise SarvamStreamError("Sarvam speech synthesis failed") from exc
