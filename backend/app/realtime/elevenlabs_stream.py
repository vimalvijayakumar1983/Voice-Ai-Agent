"""ElevenLabs Flash streaming TTS for Twilio Media Streams."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect

from app.providers.elevenlabs import ELEVENLABS_MODEL


class ElevenLabsStreamError(RuntimeError):
    pass


def _websocket_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class ElevenLabsTTSStream:
    """Open a bounded synthesis socket for each VAV response turn."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        voice_id: str,
        speed: float,
        model: str = ELEVENLABS_MODEL,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.voice_id = voice_id
        self.speed = max(0.7, min(float(speed), 1.2))
        self.model = model
        self.connections: set[ClientConnection] = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        await self.close()

    async def close(self) -> None:
        connections = list(self.connections)
        self.connections.clear()
        await asyncio.gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )

    async def _connect(self, language_code: str) -> ClientConnection:
        language = language_code.strip().lower().split("-", 1)[0]
        query = urlencode(
            {
                "model_id": self.model,
                "output_format": "ulaw_8000",
                "language_code": language,
                "auto_mode": "true",
                "inactivity_timeout": "60",
            }
        )
        url = (
            f"{_websocket_origin(self.base_url)}/v1/text-to-speech/"
            f"{quote(self.voice_id, safe='')}/stream-input?{query}"
        )
        try:
            connection = await connect(
                url,
                additional_headers={"xi-api-key": self.api_key},
                compression=None,
                open_timeout=10,
                max_size=4 * 1024 * 1024,
            )
            self.connections.add(connection)
            await connection.send(
                json.dumps(
                    {
                        "text": " ",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                            "style": 0.0,
                            "use_speaker_boost": True,
                            "speed": self.speed,
                        },
                    }
                )
            )
            return connection
        except Exception as exc:
            raise ElevenLabsStreamError("ElevenLabs speech synthesis could not connect") from exc

    @staticmethod
    async def _send_fragments(
        connection: ClientConnection,
        fragments: AsyncIterator[str],
    ) -> None:
        async for fragment in fragments:
            normalized = fragment.strip()
            if normalized:
                await connection.send(json.dumps({"text": normalized + " "}))
        await connection.send(json.dumps({"text": ""}))

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
                message = receiver.result() if receiver in done else await receiver
                if not isinstance(message, str):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                audio = payload.get("audio")
                if isinstance(audio, str) and audio:
                    try:
                        base64.b64decode(audio, validate=True)
                    except ValueError:
                        continue
                    yield audio
                completed = bool(payload.get("is_final"))
            await producer
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, ElevenLabsStreamError):
                raise
            raise ElevenLabsStreamError("ElevenLabs speech synthesis failed") from exc
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            self.connections.discard(connection)
            await connection.close()
