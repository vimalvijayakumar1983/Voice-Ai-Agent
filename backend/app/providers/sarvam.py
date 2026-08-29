"""Sarvam AI speech adapter.

Sarvam is a speech/model provider, not an Atoms-compatible hosted agent
runtime.  This adapter intentionally exposes only provider capabilities VAV
can verify directly: the Bulbul v3 catalog and bounded TTS synthesis.  Live
calls are routed by the VAV realtime runtime, never through Smallest agent
provisioning endpoints.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import httpx

from app.core.config import settings

SARVAM_MODEL = "bulbul:v3"
MAX_PREVIEW_BYTES = 8 * 1024 * 1024

SARVAM_LANGUAGE_CODES: dict[str, str] = {
    "bn": "bn-IN",
    "en": "en-IN",
    "gu": "gu-IN",
    "hi": "hi-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "or": "od-IN",
    "pa": "pa-IN",
    "ta": "ta-IN",
    "te": "te-IN",
}

SARVAM_PREVIEW_TEXTS: dict[str, str] = {
    "bn": "নমস্কার, আমি কীভাবে আপনাকে সাহায্য করতে পারি?",
    "en": "Hello, welcome. How may I help you today?",
    "gu": "નમસ્તે, હું આજે તમને કેવી રીતે મદદ કરી શકું?",
    "hi": "नमस्ते, मैं आज आपकी कैसे सहायता कर सकती हूँ?",
    "kn": "ನಮಸ್ಕಾರ, ಇಂದು ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "ml": "നമസ്കാരം, ഇന്ന് ഞാൻ നിങ്ങളെ എങ്ങനെ സഹായിക്കാം?",
    "mr": "नमस्कार, आज मी तुम्हाला कशी मदत करू शकते?",
    "or": "ନମସ୍କାର, ଆଜି ମୁଁ ଆପଣଙ୍କୁ କିପରି ସାହାଯ୍ୟ କରିପାରିବି?",
    "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਅੱਜ ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦੀ ਹਾਂ?",
    "ta": "வணக்கம், இன்று நான் உங்களுக்கு எப்படி உதவலாம்?",
    "te": "నమస్కారం, ఈ రోజు నేను మీకు ఎలా సహాయం చేయగలను?",
}

_MALE_SPEAKERS = (
    "shubh",
    "aditya",
    "rahul",
    "rohan",
    "amit",
    "dev",
    "ratan",
    "varun",
    "manan",
    "sumit",
    "kabir",
    "aayan",
    "ashutosh",
    "advait",
    "anand",
    "tarun",
    "sunny",
    "mani",
    "gokul",
    "vijay",
    "mohit",
    "rehan",
    "soham",
)
_FEMALE_SPEAKERS = (
    "ritu",
    "priya",
    "neha",
    "pooja",
    "simran",
    "kavya",
    "ishita",
    "shreya",
    "roopa",
    "tanya",
    "shruti",
    "suhani",
    "kavitha",
    "rupali",
)


class SarvamAIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def sarvam_voice_catalog() -> list[dict[str, Any]]:
    """Return the documented Bulbul v3 speaker catalog in VAV's shape."""

    languages = list(SARVAM_LANGUAGE_CODES)
    voices: list[dict[str, Any]] = []
    for speaker, gender in (
        *((speaker, "male") for speaker in _MALE_SPEAKERS),
        *((speaker, "female") for speaker in _FEMALE_SPEAKERS),
    ):
        voices.append(
            {
                "provider": "sarvam",
                # Namespace IDs because the combined VAV catalog may contain a
                # Smallest voice with the same human-readable speaker name.
                "id": f"sarvam:{speaker}",
                "name": speaker.title(),
                "languages": languages,
                "accent": "Indian",
                "gender": gender,
                "age": None,
                "use_cases": ["conversational", "support", "multilingual"],
                "synthesizer_model": SARVAM_MODEL,
                "unavailability_reason": None,
                "voice_pool": "standard",
                "source": "catalog",
            }
        )
    return voices


def sarvam_language_code(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-")
    base = normalized.split("-", 1)[0]
    try:
        return SARVAM_LANGUAGE_CODES[base]
    except KeyError as exc:
        raise SarvamAIError(
            f"Sarvam Bulbul v3 does not support the selected language: {language}",
            status_code=422,
        ) from exc


def _bounded_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    message: object = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("detail") or payload.get("error")
    if isinstance(message, dict):
        message = message.get("message") or message.get("detail")
    text = " ".join(str(message or "Sarvam AI rejected the request.").split())
    return text[:300]


class SarvamAIClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.sarvam_api_key
        self.base_url = (base_url or settings.sarvam_base_url).rstrip("/")
        self.timeout = timeout or settings.sarvam_request_timeout_seconds
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    async def synthesize_voice_preview(
        self,
        *,
        speaker: str,
        language: str,
        pace: float = 1.0,
    ) -> bytes:
        if not self.is_configured:
            raise SarvamAIError(
                "Sarvam AI is not configured. Add SARVAM_API_KEY to the backend service.",
                status_code=503,
            )
        known_speakers = {*_MALE_SPEAKERS, *_FEMALE_SPEAKERS}
        if speaker not in known_speakers:
            raise SarvamAIError("Unknown Sarvam Bulbul v3 speaker.", status_code=422)
        language_code = sarvam_language_code(language)
        preview_text = SARVAM_PREVIEW_TEXTS[language_code.split("-", 1)[0].replace("od", "or")]

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/text-to-speech",
                    headers={
                        "api-subscription-key": self.api_key,
                        "Accept": "application/json",
                    },
                    json={
                        "text": preview_text,
                        "language_code": language_code,
                        "speaker": speaker,
                        "pace": max(0.5, min(float(pace), 2.0)),
                        "speech_sample_rate": 24000,
                        "model": SARVAM_MODEL,
                        "output_audio_codec": "wav",
                        "temperature": 0.6,
                    },
                )
        except httpx.TimeoutException as exc:
            raise SarvamAIError(
                "Sarvam AI timed out while generating the preview.",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise SarvamAIError("Sarvam AI could not be reached.", status_code=502) from exc

        if response.status_code >= 400:
            status_code = (
                response.status_code if response.status_code in {400, 403, 422, 429} else 502
            )
            raise SarvamAIError(_bounded_error(response), status_code=status_code)

        try:
            payload = response.json()
            audios = payload.get("audios") if isinstance(payload, dict) else None
            encoded = audios[0] if isinstance(audios, list) and audios else None
            if not isinstance(encoded, str):
                raise ValueError("missing audio")
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise SarvamAIError("Sarvam AI returned an invalid audio response.") from exc
        if not audio or len(audio) > MAX_PREVIEW_BYTES or not audio.startswith(b"RIFF"):
            raise SarvamAIError("Sarvam AI returned an invalid WAV preview.")
        return audio


def get_sarvam_client() -> SarvamAIClient:
    return SarvamAIClient()
