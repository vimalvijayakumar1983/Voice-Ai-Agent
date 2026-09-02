"""Direct Inworld credential validation and voice-catalog adapter."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

import httpx

from app.core.config import settings

INWORLD_TTS_MODEL = "inworld-tts-2"
MAX_CATALOG_VOICES = 500
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_PROBE_RESPONSE_BYTES = 512 * 1024
MAX_PROBE_AUDIO_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 10.0
TTS_PROBE_TEXT = "OK."
ROUTER_PROBE_PROMPT = "Reply OK."
ROUTER_PROBE_MAX_TOKENS = 8
# ``auto`` may select a reasoning model that spends a tiny completion budget
# before producing visible text. Keep the larger bound isolated to that route.
ROUTER_AUTO_PROBE_MAX_TOKENS = 128

# Realtime TTS-2 is cross-lingual. The Voice API's ``langCode`` describes the
# native prompt/accent of a voice; it is not that voice's complete synthesis
# capability. Keep this to the language codes VAV names and exposes today that
# are also in Inworld's published TTS-2 Tier 1/Tier 2 list. In particular, every
# catalog voice can synthesize English, Arabic, and Hindi even when its native
# prompt uses a different language.
INWORLD_TTS_SUPPORTED_LANGUAGES = (
    "ar",
    "bn",
    "de",
    "en",
    "es",
    "el",
    "fi",
    "fr",
    "gu",
    "hi",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "ml",
    "mr",
    "ms",
    "nl",
    "or",
    "pa",
    "pl",
    "pt",
    "ru",
    "sv",
    "ta",
    "te",
    "tr",
    "vi",
    "zh",
)

_ACCENT_TAGS = (
    "american",
    "australian",
    "british",
    "canadian",
    "gulf",
    "indian",
    "irish",
    "middle eastern",
    "new zealand",
    "scottish",
    "south african",
    "uae",
)
_GENDER_TAGS = {"female", "male", "neutral", "non-binary", "nonbinary"}


class InworldError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _payload_error_message(payload: object) -> str:
    detail: object = None
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("message") or payload.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail")
    return " ".join(str(detail or "Inworld rejected the request.").split())[:300]


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return _payload_error_message(payload)


def _error_message_from_content(content: bytes) -> str:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        payload = None
    return _payload_error_message(payload)


def _probe_value(value: str, label: str, *, max_length: int = 120) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > max_length
        or any(ord(character) < 32 for character in normalized)
    ):
        raise InworldError(f"Inworld {label} is invalid.", status_code=422)
    return normalized


def _tag_metadata(voice: dict[str, Any]) -> tuple[str | None, str | None]:
    raw_tags = voice.get("tags")
    if isinstance(raw_tags, dict):
        accent = str(raw_tags.get("accent") or voice.get("accent") or "").strip() or None
        gender = str(raw_tags.get("gender") or voice.get("gender") or "").strip().lower()
        return accent, gender or None

    tags = [
        str(tag).strip()
        for tag in (raw_tags if isinstance(raw_tags, list) else [])
        if str(tag).strip()
    ]
    gender = str(voice.get("gender") or "").strip().lower()
    if not gender:
        gender = next(
            (tag.lower() for tag in tags if tag.lower() in _GENDER_TAGS),
            "",
        )
    accent = str(voice.get("accent") or "").strip()
    if not accent:
        accent = next(
            (
                tag
                for tag in tags
                if any(marker in tag.lower().replace("_", " ") for marker in _ACCENT_TAGS)
            ),
            "",
        )
    return accent or None, gender or None


def _plausible_audio(content: bytes) -> bool:
    if len(content) < 4:
        return False
    if content.startswith((b"ID3", b"OggS", b"fLaC")):
        return True
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WAVE":
        return True
    return content[0] == 0xFF and content[1] & 0xE0 == 0xE0


def normalize_inworld_voice(voice: dict[str, Any]) -> dict[str, Any] | None:
    voice_id = voice.get("voiceId") or voice.get("voice_id") or voice.get("id")
    if not isinstance(voice_id, str) or not voice_id.strip():
        return None
    accent, gender = _tag_metadata(voice)
    return {
        "provider": "inworld",
        "id": f"inworld:{voice_id.strip()}",
        "name": str(voice.get("displayName") or voice.get("name") or voice_id).strip(),
        "languages": list(INWORLD_TTS_SUPPORTED_LANGUAGES),
        "accent": accent,
        "gender": gender,
        "age": None,
        "use_cases": ["conversational", "multilingual"],
        "synthesizer_model": INWORLD_TTS_MODEL,
        "unavailability_reason": None,
        "voice_pool": "standard",
        "source": "catalog",
    }


class InworldClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.inworld_api_key
        self.base_url = (base_url or settings.inworld_base_url).rstrip("/")
        self.timeout = timeout or settings.inworld_request_timeout_seconds
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    async def _voices_payload(self) -> dict[str, Any]:
        if not self.is_configured:
            raise InworldError(
                "Inworld is not configured. Add an API key in Settings.", status_code=503
            )
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    "/voices/v1/voices",
                    headers={
                        "Authorization": f"Basic {self.api_key}",
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise InworldError(
                "Inworld timed out while validating the API key.", status_code=504
            ) from exc
        except httpx.HTTPError as exc:
            raise InworldError("Inworld could not be reached.") from exc
        if response.status_code >= 400:
            code = (
                response.status_code if response.status_code in {400, 401, 403, 422, 429} else 502
            )
            raise InworldError(_error_message(response), status_code=code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise InworldError("Inworld returned an invalid validation response.") from exc
        if not isinstance(payload, dict):
            raise InworldError("Inworld returned an invalid validation response.")
        return payload

    async def validate_connection(self) -> None:
        await self._voices_payload()

    async def _bounded_probe_json(
        self,
        *,
        path: str,
        body: dict[str, Any],
        probe_name: str,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise InworldError(
                "Inworld is not configured. Add an API key in Settings.", status_code=503
            )
        timeout = max(1.0, min(float(self.timeout), PROBE_TIMEOUT_SECONDS))
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(timeout),
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    path,
                    headers={
                        "Authorization": f"Basic {self.api_key}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response:
                    status_code = response.status_code
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > MAX_PROBE_RESPONSE_BYTES:
                            raise InworldError(
                                f"Inworld {probe_name} returned an unexpectedly large response."
                            )
                        content.extend(chunk)
        except httpx.TimeoutException as exc:
            raise InworldError(
                f"Inworld {probe_name} timed out after {timeout:g} seconds.",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise InworldError(f"Inworld {probe_name} could not be reached.") from exc
        if status_code >= 400:
            code = status_code if status_code in {400, 401, 403, 404, 422, 429} else 502
            raise InworldError(_error_message_from_content(bytes(content)), status_code=code)
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise InworldError(f"Inworld {probe_name} returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise InworldError(f"Inworld {probe_name} returned an invalid response.")
        return payload

    async def synthesize_readiness_probe(
        self,
        *,
        voice_id: str,
        model_id: str,
    ) -> None:
        """Generate three characters to verify the exact selected TTS route."""

        selected_voice = _probe_value(voice_id, "voice ID")
        selected_model = _probe_value(model_id, "TTS model")
        payload = await self._bounded_probe_json(
            path="/tts/v1/voice",
            probe_name="TTS readiness probe",
            body={
                "text": TTS_PROBE_TEXT,
                "voiceId": selected_voice,
                "modelId": selected_model,
                "audioConfig": {
                    "audioEncoding": "MP3",
                    "bitRate": 32_000,
                    "sampleRateHertz": 16_000,
                },
                "deliveryMode": "STABLE",
                "applyTextNormalization": "OFF",
            },
        )
        encoded = payload.get("audioContent") or payload.get("audio_content")
        if not isinstance(encoded, str) or not encoded.strip():
            raise InworldError("Inworld TTS readiness probe returned no audio.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InworldError(
                "Inworld TTS readiness probe returned invalid audio encoding."
            ) from exc
        if not content or len(content) > MAX_PROBE_AUDIO_BYTES or not _plausible_audio(content):
            raise InworldError("Inworld TTS readiness probe returned invalid audio.")
        usage = payload.get("usage")
        actual_model = (
            usage.get("modelId") or usage.get("model_id") if isinstance(usage, dict) else None
        )
        if actual_model and actual_model != selected_model:
            raise InworldError(
                "Inworld TTS readiness probe used a different model than the selected route."
            )

    async def router_readiness_probe(self, *, model_id: str) -> None:
        """Request a small, bounded completion from the selected Router model."""

        selected_model = _probe_value(model_id, "Router model")
        max_tokens = (
            ROUTER_AUTO_PROBE_MAX_TOKENS if selected_model == "auto" else ROUTER_PROBE_MAX_TOKENS
        )
        payload = await self._bounded_probe_json(
            path="/v1/chat/completions",
            probe_name="Router readiness probe",
            body={
                "model": selected_model,
                "messages": [{"role": "user", "content": ROUTER_PROBE_PROMPT}],
                "max_tokens": max_tokens,
                "temperature": 0,
                "stream": False,
            },
        )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InworldError("Inworld Router readiness probe returned no completion.")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        if first.get("finish_reason") == "length":
            raise InworldError(
                "Inworld Router readiness probe exhausted its bounded output token budget."
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise InworldError("Inworld Router readiness probe returned invalid content.")
        if not content.strip():
            raise InworldError("Inworld Router readiness probe returned an empty completion.")

    async def list_voices(self) -> list[dict[str, Any]]:
        payload = await self._voices_payload()
        items = payload.get("voices") or payload.get("items") or []
        if not isinstance(items, list):
            raise InworldError("Inworld returned an invalid voice catalog.")
        voices = [normalize_inworld_voice(item) for item in items if isinstance(item, dict)]
        return [voice for voice in voices if voice is not None][:MAX_CATALOG_VOICES]

    async def voice_preview(self, voice_id: str) -> bytes:
        """Fetch Inworld's provider-hosted sample without generating billable text."""
        if not self.is_configured:
            raise InworldError(
                "Inworld is not configured. Add an API key in Settings.", status_code=503
            )
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    "/tts/v1/voice:preview",
                    headers={
                        "Authorization": f"Basic {self.api_key}",
                        "Accept": "application/json",
                    },
                    params={"voice_id": voice_id, "model_id": INWORLD_TTS_MODEL},
                )
        except httpx.TimeoutException as exc:
            raise InworldError(
                "Inworld timed out while loading the voice preview.", status_code=504
            ) from exc
        except httpx.HTTPError as exc:
            raise InworldError("Inworld could not be reached.") from exc
        if response.status_code >= 400:
            code = (
                response.status_code
                if response.status_code in {400, 401, 403, 404, 422, 429}
                else 502
            )
            raise InworldError(_error_message(response), status_code=code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise InworldError("Inworld returned an invalid voice preview response.") from exc
        encoded = payload.get("audioContent") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or not encoded.strip():
            raise InworldError("Inworld returned a voice preview without audio.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InworldError("Inworld returned an invalid voice preview encoding.") from exc
        if not content or len(content) > MAX_PREVIEW_BYTES or not _plausible_audio(content):
            raise InworldError("Inworld returned an invalid voice preview.")
        return content
