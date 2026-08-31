"""ElevenLabs speech-only adapter for VAV-owned agents."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings

ELEVENLABS_MODEL = "eleven_flash_v2_5"
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_CATALOG_VOICES = 500

# Flash v2.5 model coverage. Voices remain account-scoped and are loaded from
# the authenticated v2 catalog; VAV never invents or exposes inaccessible IDs.
ELEVENLABS_LANGUAGE_CODES = (
    "ar",
    "bg",
    "zh",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "fil",
    "fi",
    "fr",
    "de",
    "el",
    "hu",
    "hi",
    "id",
    "it",
    "ja",
    "ko",
    "ms",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "es",
    "sv",
    "ta",
    "tr",
    "uk",
    "vi",
)

PREVIEW_TEXTS = {
    "ar": "مرحباً، كيف يمكنني مساعدتك اليوم؟",
    "en": "Hello, welcome. How may I help you today?",
    "fr": "Bonjour, comment puis-je vous aider aujourd'hui ?",
    "de": "Hallo, wie kann ich Ihnen heute helfen?",
    "hi": "नमस्ते, मैं आज आपकी कैसे सहायता कर सकती हूँ?",
    "es": "Hola, ¿cómo puedo ayudarle hoy?",
    "ta": "வணக்கம், இன்று நான் உங்களுக்கு எப்படி உதவலாம்?",
}


class ElevenLabsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _bounded_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail: object = None
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail") or detail.get("status")
    message = " ".join(str(detail or "ElevenLabs rejected the request.").split())
    return message[:300]


def _voice_pool(voice: dict[str, Any]) -> str:
    category = str(voice.get("category") or "").lower()
    if category in {"cloned", "generated"} or voice.get("is_owner"):
        return "cloned"
    if category == "professional":
        return "pro"
    return "standard"


def normalize_elevenlabs_voice(voice: dict[str, Any]) -> dict[str, Any] | None:
    voice_id = voice.get("voice_id") or voice.get("voiceId")
    if not isinstance(voice_id, str) or not voice_id.strip():
        return None
    labels = voice.get("labels") if isinstance(voice.get("labels"), dict) else {}
    use_case = labels.get("use_case") or labels.get("use case")
    use_cases = [str(use_case)] if use_case else ["conversational"]
    pool = _voice_pool(voice)
    return {
        "provider": "elevenlabs",
        "id": f"elevenlabs:{voice_id.strip()}",
        "name": str(voice.get("name") or voice_id).strip(),
        "languages": list(ELEVENLABS_LANGUAGE_CODES),
        "accent": str(labels.get("accent") or "").strip() or None,
        "gender": str(labels.get("gender") or "").strip().lower() or None,
        "age": str(labels.get("age") or "").strip() or None,
        "use_cases": use_cases,
        "synthesizer_model": ELEVENLABS_MODEL,
        "unavailability_reason": None,
        "voice_pool": pool,
        "source": "cloned" if pool == "cloned" else "catalog",
    }


class ElevenLabsClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.elevenlabs_api_key
        self.base_url = (base_url or settings.elevenlabs_base_url).rstrip("/")
        self.timeout = timeout or settings.elevenlabs_request_timeout_seconds
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    async def list_voices(self) -> list[dict[str, Any]]:
        if not self.is_configured:
            return []
        voices: list[dict[str, Any]] = []
        next_page_token: str | None = None
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                while len(voices) < MAX_CATALOG_VOICES:
                    params: dict[str, str | int | bool] = {
                        "page_size": min(100, MAX_CATALOG_VOICES - len(voices)),
                        "include_total_count": False,
                        "sort": "name",
                        "sort_direction": "asc",
                    }
                    if next_page_token:
                        params["next_page_token"] = next_page_token
                    response = await client.get(
                        "/v2/voices",
                        headers={"xi-api-key": self.api_key, "Accept": "application/json"},
                        params=params,
                    )
                    if response.status_code >= 400:
                        status_code = (
                            response.status_code
                            if response.status_code in {400, 401, 403, 422, 429}
                            else 502
                        )
                        raise ElevenLabsError(
                            _bounded_error(response), status_code=status_code
                        )
                    payload = response.json()
                    page = payload.get("voices") if isinstance(payload, dict) else None
                    if not isinstance(page, list):
                        raise ElevenLabsError("ElevenLabs returned an invalid voice catalog.")
                    for item in page:
                        normalized = (
                            normalize_elevenlabs_voice(item)
                            if isinstance(item, dict)
                            else None
                        )
                        if normalized is not None:
                            voices.append(normalized)
                    if not payload.get("has_more"):
                        break
                    token = payload.get("next_page_token")
                    if not isinstance(token, str) or not token or token == next_page_token:
                        break
                    next_page_token = token
        except ElevenLabsError:
            raise
        except httpx.TimeoutException as exc:
            raise ElevenLabsError(
                "ElevenLabs timed out while loading voices.", status_code=504
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ElevenLabsError("ElevenLabs could not be reached.") from exc
        return voices[:MAX_CATALOG_VOICES]

    async def synthesize_voice_preview(
        self,
        *,
        voice_id: str,
        language: str,
        speed: float = 1.0,
    ) -> bytes:
        if not self.is_configured:
            raise ElevenLabsError(
                "ElevenLabs is not configured. Add an API key in Settings.",
                status_code=503,
            )
        normalized_language = language.strip().lower().split("-", 1)[0]
        if normalized_language not in ELEVENLABS_LANGUAGE_CODES:
            raise ElevenLabsError(
                f"ElevenLabs Flash v2.5 does not support {language}.", status_code=422
            )
        text = PREVIEW_TEXTS.get(normalized_language, PREVIEW_TEXTS["en"])
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"/v1/text-to-speech/{quote(voice_id, safe='')}/stream",
                    headers={
                        "xi-api-key": self.api_key,
                        "Accept": "audio/mpeg",
                        "Content-Type": "application/json",
                    },
                    params={"output_format": "mp3_44100_128"},
                    json={
                        "text": text,
                        "model_id": ELEVENLABS_MODEL,
                        "language_code": normalized_language,
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                            "style": 0.0,
                            "use_speaker_boost": True,
                            "speed": max(0.7, min(float(speed), 1.2)),
                        },
                    },
                )
        except httpx.TimeoutException as exc:
            raise ElevenLabsError(
                "ElevenLabs timed out while generating the preview.", status_code=504
            ) from exc
        except httpx.HTTPError as exc:
            raise ElevenLabsError("ElevenLabs could not be reached.") from exc
        if response.status_code >= 400:
            status_code = (
                response.status_code
                if response.status_code in {400, 401, 403, 422, 429}
                else 502
            )
            raise ElevenLabsError(_bounded_error(response), status_code=status_code)
        audio = response.content
        if not audio or len(audio) > MAX_PREVIEW_BYTES:
            raise ElevenLabsError("ElevenLabs returned an invalid audio preview.")
        return audio


def get_elevenlabs_client() -> ElevenLabsClient:
    return ElevenLabsClient()
