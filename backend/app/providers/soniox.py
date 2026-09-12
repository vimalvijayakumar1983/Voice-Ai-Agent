"""Direct Soniox credentials and catalog. Secrets stay on the VAV server."""

from __future__ import annotations

import asyncio
import json

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.core.config import settings

SONIOX_STT_MODEL = "stt-rt-v5"
SONIOX_TTS_MODEL = "tts-rt-v1"


class SonioxError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _records(payload: dict, key: str) -> list[dict]:
    values = payload.get(key)
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise SonioxError(f"Soniox returned invalid {key} metadata")
    return values


class SonioxClient:
    def __init__(self, *, api_key: str = "", transport=None):
        self.api_key = api_key.strip()
        self._transport = transport
        self.source = "provided" if self.api_key else "none"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, **params) -> dict:
        if not self.is_configured:
            raise SonioxError("Add a Soniox API key in Settings", status_code=409)
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, transport=self._transport
            ) as client:
                async with client.stream(
                    "GET",
                    f"https://api.soniox.com/v1/{path}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    params=params,
                ) as response:
                    if response.status_code != 200:
                        # Never echo untrusted provider response bodies or credentials.
                        status = response.status_code
                        raise SonioxError(
                            f"Soniox request failed (HTTP {status}). Check key and account access.",
                            status_code=422 if status in {400, 401, 403, 404} else 502,
                        )
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 2 * 1024 * 1024:
                            raise SonioxError("Soniox catalog exceeded the response limit")
                    result = json.loads(payload)
                    if not isinstance(result, dict):
                        raise SonioxError("Soniox returned an invalid catalog")
                    return result
        except (httpx.HTTPError, ValueError) as exc:
            raise SonioxError("Soniox catalog could not be read; please retry") from exc

    async def validate_connection(self) -> None:
        stt = await self._get("models")
        tts = await self._get("tts-models")
        for payload, required in ((stt, SONIOX_STT_MODEL), (tts, SONIOX_TTS_MODEL)):
            if not any(item.get("id") == required for item in _records(payload, "models")):
                raise SonioxError(f"Soniox account does not expose {required}", status_code=422)

    async def list_voices(self) -> list[dict]:
        model_catalog = await self._get("tts-models")
        model = next(
            (
                item
                for item in _records(model_catalog, "models")
                if item.get("id") == SONIOX_TTS_MODEL
            ),
            None,
        )
        if model is None:
            raise SonioxError("Soniox TTS model is unavailable", status_code=422)
        languages = [
            item["code"]
            for item in _records(model, "languages")
            if isinstance(item.get("code"), str) and item["code"]
        ]
        voices, cursor, seen = [], None, set()
        for _ in range(5):
            params = {"model": SONIOX_TTS_MODEL, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await self._get("shared-voices", **params)
            for voice in _records(payload, "voices"):
                voice_id = str(voice.get("id") or "").strip()
                if not voice_id or voice_id in seen:
                    continue
                seen.add(voice_id)
                voices.append(
                    {
                        "id": f"soniox:{voice_id}",
                        "name": voice_id,
                        "provider": "soniox",
                        "languages": languages,
                        "gender": voice.get("gender"),
                        "accent": voice.get("accent"),
                        "description": str(voice.get("description") or "")[:1000],
                        "synthesizer_model": SONIOX_TTS_MODEL,
                        "voice_pool": "standard",
                    }
                )
            cursor = payload.get("next_page_cursor")
            if not cursor:
                return voices
        raise SonioxError("Soniox voice catalog pagination limit reached; catalog is incomplete")

    async def voice_preview(self, *, voice_id: str, language: str, speed: float = 1.0) -> bytes:
        """A fixed, bounded phrase; not an arbitrary text-to-speech proxy."""
        if not self.is_configured:
            raise SonioxError("Add a Soniox API key in Settings", status_code=409)
        try:
            async with asyncio.timeout(20):
                async with httpx.AsyncClient(
                    timeout=15, follow_redirects=False, transport=self._transport
                ) as client:
                    async with client.stream(
                        "POST",
                        "https://tts-rt.soniox.com/tts",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={
                            "model": SONIOX_TTS_MODEL,
                            "voice": voice_id.removeprefix("soniox:"),
                            "language": language.split("-")[0].lower(),
                            "speed": max(0.7, min(1.3, speed)),
                            "audio_format": "wav",
                            "text": "Hello, I am your voice assistant. How can I help you today?",
                        },
                    ) as response:
                        if response.status_code != 200:
                            raise SonioxError(
                                f"Soniox voice synthesis failed (HTTP {response.status_code})"
                            )
                        audio = bytearray()
                        async for chunk in response.aiter_bytes():
                            audio.extend(chunk)
                            if len(audio) > 2 * 1024 * 1024:
                                raise SonioxError("Soniox preview exceeded the audio limit")
                        if len(audio) <= 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
                            raise SonioxError("Soniox returned no valid preview audio")
                        return bytes(audio)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise SonioxError("Soniox voice synthesis could not complete; please retry") from exc

    async def stt_readiness_probe(self, *, languages: list[str]) -> None:
        """Prove authenticated streaming completion, not recognition accuracy."""
        if not self.is_configured:
            raise SonioxError("Add a Soniox API key in Settings", status_code=409)
        try:
            async with asyncio.timeout(12):
                async with connect(
                    "wss://stt-rt.soniox.com/transcribe-websocket",
                    open_timeout=5,
                    close_timeout=1,
                    max_size=256 * 1024,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "api_key": self.api_key,
                                "model": SONIOX_STT_MODEL,
                                "audio_format": "pcm_s16le",
                                "sample_rate": 16000,
                                "num_channels": 1,
                                "language_hints": languages,
                                "language_hints_strict": True,
                                "enable_endpoint_detection": True,
                                "max_endpoint_delay_ms": 1000,
                            }
                        )
                    )
                    await websocket.send(bytes(6400))
                    await websocket.send("")
                    async for message in websocket:
                        result = json.loads(message)
                        if not isinstance(result, dict) or result.get("error_code"):
                            raise SonioxError("Soniox rejected the streaming recognition probe")
                        if result.get("finished") is True:
                            return
            raise SonioxError("Soniox recognition closed without completion")
        except (TimeoutError, WebSocketException, OSError, ValueError) as exc:
            raise SonioxError("Soniox streaming recognition could not complete") from exc


async def tenant_soniox_client(db, tenant_id) -> SonioxClient:
    from app.services.provider_credentials import load_provider_config

    config = await load_provider_config(db, tenant_id, "soniox")
    key = str(config.get("api_key") or "") if config is not None else settings.soniox_api_key
    client = SonioxClient(api_key=key)
    client.source = "workspace" if config is not None else "platform" if key else "none"
    return client
