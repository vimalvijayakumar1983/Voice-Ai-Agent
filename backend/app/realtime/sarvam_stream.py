"""Sarvam Saaras realtime STT and Bulbul v3 streaming TTS clients."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect

SARVAM_STT_SILENCE_DURATION_MS = 450
SARVAM_TTS_MIN_BUFFER_SIZE = 30
SARVAM_TTS_MAX_CHUNK_LENGTH = 120


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


def is_speech_end(payload: dict) -> bool:
    return str(payload.get("event") or payload.get("type") or "") in {
        "vad.speech_end",
        "speech_end",
        "speech_ended",
    }


class SarvamSTTStream:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        language_code: str,
        sample_rate: int = 8000,
        silence_duration_ms: int = SARVAM_STT_SILENCE_DURATION_MS,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.language_code = language_code
        self.sample_rate = sample_rate
        self.silence_duration_ms = max(250, min(int(silence_duration_ms), 1200))
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
                "silence_duration_ms": self.silence_duration_ms,
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


class SarvamTTSStream:
    """One reusable Bulbul v3 WebSocket for a realtime voice call.

    A language change or interrupted synthesis deliberately reconnects because
    Sarvam requires configuration to be the first message on a connection.
    Normal same-language turns reuse the socket and avoid another TLS/WebSocket
    handshake.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        speaker: str,
        pace: float,
        min_buffer_size: int = SARVAM_TTS_MIN_BUFFER_SIZE,
        max_chunk_length: int = SARVAM_TTS_MAX_CHUNK_LENGTH,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.speaker = speaker
        self.pace = pace
        self.min_buffer_size = max(10, min(int(min_buffer_size), 100))
        self.max_chunk_length = max(50, min(int(max_chunk_length), 500))
        self.connection: ClientConnection | None = None
        self.language_code: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        await self.close()

    async def close(self) -> None:
        connection, self.connection = self.connection, None
        self.language_code = None
        if connection is not None:
            await connection.close()

    async def _connect(self, language_code: str) -> ClientConnection:
        if (
            self.connection is not None
            and self.language_code == language_code
            and getattr(self.connection, "close_code", None) is None
        ):
            return self.connection
        await self.close()
        url = (
            f"{_websocket_origin(self.base_url)}/text-to-speech/ws"
            "?model=bulbul:v3&send_completion_event=true"
        )
        connection: ClientConnection | None = None
        try:
            connection = await connect(
                url,
                additional_headers={"Api-Subscription-Key": self.api_key},
                compression=None,
                open_timeout=10,
                max_size=4 * 1024 * 1024,
            )
            await connection.send(
                json.dumps(
                    {
                        "type": "config",
                        "data": {
                            "speaker": self.speaker,
                            "language_code": language_code,
                            "pace": max(0.5, min(float(self.pace), 2.0)),
                            "min_buffer_size": self.min_buffer_size,
                            "max_chunk_length": self.max_chunk_length,
                            "output_audio_codec": "mulaw",
                            "speech_sample_rate": 8000,
                        },
                    }
                )
            )
        except Exception as exc:
            if connection is not None:
                await connection.close()
            await self.close()
            raise SarvamStreamError("Sarvam speech synthesis could not connect") from exc
        self.connection = connection
        self.language_code = language_code
        return connection

    @staticmethod
    async def _send_fragments(
        connection: ClientConnection,
        fragments: AsyncIterator[str],
    ) -> None:
        async for fragment in fragments:
            if fragment.strip():
                await connection.send(json.dumps({"type": "text", "data": {"text": fragment}}))
        await connection.send(json.dumps({"type": "flush"}))

    async def audio_for(
        self,
        fragments: AsyncIterator[str],
        *,
        language_code: str,
    ) -> AsyncIterator[str]:
        connection = await self._connect(language_code)
        producer = asyncio.create_task(self._send_fragments(connection, fragments))
        completed = False
        try:
            while not completed:
                receiver = asyncio.create_task(connection.recv())
                done, _ = await asyncio.wait(
                    {producer, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if producer in done:
                    error = producer.exception()
                    if error is not None:
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
                        raise error
                if receiver not in done:
                    message = await receiver
                else:
                    message = receiver.result()
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
                completed = event_type in {"final", "completion", "completed"} or bool(
                    event_type == "event"
                    and isinstance(data, dict)
                    and data.get("event_type") == "final"
                )
            await producer
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            if isinstance(exc, SarvamStreamError):
                raise
            raise SarvamStreamError("Sarvam speech synthesis failed") from exc
        finally:
            if not completed:
                await self.close()
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)


async def stream_tts_audio(
    *,
    api_key: str,
    base_url: str,
    text: str,
    speaker: str,
    language_code: str,
    pace: float,
) -> AsyncIterator[str]:
    """Compatibility wrapper for one-off synthesis outside a call session."""

    async def fragments() -> AsyncIterator[str]:
        yield text

    async with SarvamTTSStream(
        api_key=api_key,
        base_url=base_url,
        speaker=speaker,
        pace=pace,
    ) as stream:
        async for audio in stream.audio_for(fragments(), language_code=language_code):
            yield audio
