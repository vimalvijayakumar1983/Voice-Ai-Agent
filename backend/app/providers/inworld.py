"""Direct Inworld credential validation and voice-catalog adapter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
import httpx

from app.core.config import settings

INWORLD_TTS_MODEL = "inworld-tts-2"
INWORLD_REALTIME_TTS_MODELS = (
    "inworld-tts-1.5-max",
    "inworld-tts-1.5-mini",
    INWORLD_TTS_MODEL,
)
INWORLD_REALTIME_TTS_DEFAULT = "inworld-tts-1.5-max"
MAX_CATALOG_VOICES = 500
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_PROBE_RESPONSE_BYTES = 512 * 1024
MAX_PROBE_AUDIO_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 10.0
TTS_PROBE_TEXT = "OK."
ROUTER_PROBE_PROMPT = "Reply OK."
ROUTER_TOOL_NAME = "vav_readiness_check"
ROUTER_PROBE_MAX_TOKENS = 64
REALTIME_PROBE_MAX_EVENTS = 32
REALTIME_SINGLE_PASS_QUIET_SECONDS = 0.2
# ``auto`` may select a reasoning model that spends a tiny completion budget
# before producing visible text. Keep the larger bound isolated to that route.
ROUTER_AUTO_PROBE_MAX_TOKENS = 128
INWORLD_REALTIME_PATH = "/api/v1/realtime/session"


def _is_exact_readiness_response(parts: list[str]) -> bool:
    """Accept only the requested readiness sentinel, never a negated phrase."""

    normalized = " ".join(" ".join(parts).split())
    return re.fullmatch(r"READY[.!]?", normalized, flags=re.IGNORECASE) is not None


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


def inworld_realtime_websocket_url(base_url: str, *, session_id: str) -> str:
    """Build the provider-specific WebSocket URL without exposing credentials."""

    selected_session = _probe_value(session_id, "Realtime session ID", max_length=180)
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise InworldError("Inworld Realtime base URL is invalid.", status_code=422)
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            INWORLD_REALTIME_PATH,
            urlencode({"key": selected_session, "protocol": "realtime"}),
            "",
        )
    )


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

    async def realtime_readiness_probe(
        self,
        *,
        model_id: str,
        voice_id: str,
        stt_model_id: str,
        stt_language: str | None,
        output_tts_model: str = INWORLD_TTS_MODEL,
        single_pass: bool = False,
    ) -> None:
        """Prove the exact native response path selected by the runtime policy.

        Tool-loop mode executes VAV's required knowledge tool. Experimental
        single-pass mode instead proves that provider auto-response can be
        disabled and that one explicit, response-scoped, tool-free generation
        completes. Both checks remain text-only and tightly bounded.
        """

        if not self.is_configured:
            raise InworldError(
                "Inworld is not configured. Add an API key in Settings.", status_code=503
            )
        selected_model = _probe_value(model_id, "Realtime model")
        selected_voice = _probe_value(voice_id, "voice ID")
        selected_stt = _probe_value(stt_model_id, "Realtime transcription model")
        selected_tts = _probe_value(output_tts_model, "Realtime speech model")
        if selected_tts not in INWORLD_REALTIME_TTS_MODELS:
            raise InworldError("Unsupported Inworld Realtime speech model.", status_code=422)
        selected_language = (
            None
            if stt_language is None
            else _probe_value(stt_language, "Realtime transcription language")
        )
        timeout = max(1.0, min(float(self.timeout), PROBE_TIMEOUT_SECONDS))
        url = inworld_realtime_websocket_url(
            self.base_url,
            session_id=f"vav-readiness-{uuid4().hex}",
        )
        tool = {
            "type": "function",
            "name": ROUTER_TOOL_NAME,
            "description": "Confirms tool-calling capability.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }
        update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": selected_model,
                # Keep the capability check silent and tightly bounded. The
                # selected voice and TTS model are still validated when the
                # session configuration is accepted.
                "output_modalities": ["text"],
                "max_output_tokens": 16,
                "tools": [] if single_pass else [tool],
                "tool_choice": "none" if single_pass else "required",
                "audio": {
                    "input": {
                        "transcription": {
                            "model": selected_stt,
                            "language": selected_language,
                        },
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "medium",
                            "create_response": not single_pass,
                            "interrupt_response": not single_pass,
                        },
                    },
                    "output": {
                        "voice": selected_voice,
                        "model": selected_tts,
                        "speed": 1.0,
                    },
                },
            },
        }
        try:
            async with aiohttp.ClientSession() as session:
                websocket = await asyncio.wait_for(
                    session.ws_connect(
                        url,
                        headers={
                            "Authorization": f"Basic {self.api_key}",
                            "User-Agent": "VAV readiness probe",
                        },
                    ),
                    timeout,
                )
                async with websocket:
                    created = await asyncio.wait_for(websocket.receive_json(), timeout)
                    if created.get("type") == "error":
                        raise InworldError(_payload_error_message(created))
                    if created.get("type") != "session.created":
                        raise InworldError(
                            "Inworld Realtime readiness probe did not create a session."
                        )
                    await websocket.send_json(update)
                    configured = await asyncio.wait_for(websocket.receive_json(), timeout)
                    if configured.get("type") == "error":
                        raise InworldError(_payload_error_message(configured), status_code=422)
                    if configured.get("type") != "session.updated":
                        raise InworldError(
                            "Inworld Realtime readiness probe did not accept the selected route."
                        )
                    effective = configured.get("session")
                    effective_audio = (
                        effective.get("audio") if isinstance(effective, dict) else None
                    )
                    if single_pass:
                        effective_audio = (
                            effective.get("audio") if isinstance(effective, dict) else None
                        )
                        effective_input = (
                            effective_audio.get("input")
                            if isinstance(effective_audio, dict)
                            else None
                        )
                        effective_transcription = (
                            effective_input.get("transcription")
                            if isinstance(effective_input, dict)
                            else None
                        )
                        effective_turn_detection = (
                            effective_input.get("turn_detection")
                            if isinstance(effective_input, dict)
                            else None
                        )
                        if not (
                            isinstance(effective, dict)
                            and effective.get("tool_choice") == "none"
                            and effective.get("tools") == []
                            and isinstance(effective_transcription, dict)
                            and effective_transcription.get("model") == selected_stt
                            and effective_transcription.get("language") == selected_language
                            and isinstance(effective_turn_detection, dict)
                            and effective_turn_detection.get("create_response") is False
                            and effective_turn_detection.get("interrupt_response") is False
                        ):
                            raise InworldError(
                                "Inworld Realtime did not echo the required single-pass "
                                "session configuration.",
                                status_code=422,
                            )
                    effective_output = (
                        effective_audio.get("output") if isinstance(effective_audio, dict) else None
                    )
                    if not (
                        isinstance(effective_output, dict)
                        and effective_output.get("model") == selected_tts
                    ):
                        raise InworldError(
                            "Inworld Realtime did not echo the selected speech model.",
                            status_code=422,
                        )
                    await websocket.send_json(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "Reply READY only. Do not call tools."
                                            if single_pass
                                            else f"Call {ROUTER_TOOL_NAME} now. "
                                            "Do not reply with text."
                                        ),
                                    }
                                ],
                            },
                        }
                    )
                    if single_pass:
                        quiet_deadline = (
                            asyncio.get_running_loop().time() + REALTIME_SINGLE_PASS_QUIET_SECONDS
                        )
                        while True:
                            remaining = quiet_deadline - asyncio.get_running_loop().time()
                            if remaining <= 0:
                                break
                            try:
                                unexpected = await asyncio.wait_for(
                                    websocket.receive_json(),
                                    remaining,
                                )
                            except TimeoutError:
                                break
                            unexpected_type = unexpected.get("type")
                            if unexpected_type == "error":
                                raise InworldError(
                                    _payload_error_message(unexpected),
                                    status_code=422,
                                )
                            if unexpected_type in {
                                "response.created",
                                "response.done",
                                "response.function_call_arguments.done",
                                "response.output_text.delta",
                                "response.output_text.done",
                            }:
                                raise InworldError(
                                    "Inworld Realtime generated an automatic response while "
                                    "single-pass auto-response was disabled.",
                                    status_code=422,
                                )
                    client_event_id = f"vav_readiness_{uuid4().hex}"
                    response_create: dict[str, Any] = {"type": "response.create"}
                    if single_pass:
                        response_create["event_id"] = client_event_id
                        response_create["response"] = {
                            "instructions": "Reply with READY only. Do not call tools.",
                            "tools": [],
                            "tool_choice": "none",
                            "metadata": {"client_event_id": client_event_id},
                        }
                    await websocket.send_json(response_create)
                    correlated_response_id: str | None = None
                    response_text_parts: list[str] = []
                    for _ in range(REALTIME_PROBE_MAX_EVENTS):
                        event = await asyncio.wait_for(websocket.receive_json(), timeout)
                        event_type = event.get("type")
                        if event_type == "error":
                            raise InworldError(_payload_error_message(event), status_code=422)
                        if event_type == "response.created" and single_pass:
                            response = event.get("response")
                            metadata = (
                                response.get("metadata") if isinstance(response, dict) else None
                            )
                            if not (
                                isinstance(metadata, dict)
                                and metadata.get("client_event_id") == client_event_id
                                and isinstance(response.get("id"), str)
                                and response["id"]
                            ):
                                raise InworldError(
                                    "Inworld Realtime did not preserve the LiveKit response "
                                    "correlation metadata.",
                                    status_code=422,
                                )
                            correlated_response_id = response["id"]
                            continue
                        if single_pass and event_type in {
                            "response.output_text.delta",
                            "response.output_text.done",
                        }:
                            text = event.get("delta") or event.get("text")
                            if isinstance(text, str) and text:
                                response_text_parts.append(text)
                            continue
                        if event_type == "response.function_call_arguments.done":
                            if single_pass:
                                raise InworldError(
                                    "Inworld Realtime ignored the single-pass tool lockout.",
                                    status_code=422,
                                )
                            if event.get("name") != ROUTER_TOOL_NAME:
                                raise InworldError(
                                    "Inworld Realtime called an unexpected readiness tool."
                                )
                            return
                        if event_type == "response.done":
                            response = event.get("response")
                            status = response.get("status") if isinstance(response, dict) else None
                            if status in {"failed", "cancelled", "incomplete"}:
                                raise InworldError(
                                    _payload_error_message(response), status_code=422
                                )
                            if single_pass:
                                if not isinstance(response, dict):
                                    raise InworldError(
                                        "Inworld Realtime returned an invalid manual response.",
                                        status_code=422,
                                    )
                                if correlated_response_id is None:
                                    raise InworldError(
                                        "Inworld Realtime completed before the correlated "
                                        "manual response was created.",
                                        status_code=422,
                                    )
                                if response.get("id") != correlated_response_id:
                                    raise InworldError(
                                        "Inworld Realtime completed a different response than "
                                        "the LiveKit-correlated request.",
                                        status_code=422,
                                    )
                                output = (
                                    response.get("output", []) if isinstance(response, dict) else []
                                )
                                if any(
                                    isinstance(item, dict) and item.get("type") == "function_call"
                                    for item in output
                                ):
                                    raise InworldError(
                                        "Inworld Realtime ignored the single-pass tool lockout.",
                                        status_code=422,
                                    )
                                if status != "completed":
                                    raise InworldError(
                                        "Inworld Realtime did not complete the manual "
                                        "single-pass response.",
                                        status_code=422,
                                    )
                                final_text_parts: list[str] = []
                                for item in output:
                                    if not isinstance(item, dict):
                                        continue
                                    for part in item.get("content", []):
                                        if not isinstance(part, dict):
                                            continue
                                        text = part.get("text") or part.get("transcript")
                                        if isinstance(text, str) and text:
                                            final_text_parts.append(text)
                                readiness_text = final_text_parts or response_text_parts
                                if not _is_exact_readiness_response(readiness_text):
                                    raise InworldError(
                                        "Inworld Realtime manual response returned no verifiable "
                                        "text output.",
                                        status_code=422,
                                    )
                                return
                            raise InworldError(
                                "Inworld Realtime completed without executing the required tool."
                            )
                    raise InworldError("Inworld Realtime did not complete the required tool check.")
        except InworldError:
            raise
        except TimeoutError as exc:
            raise InworldError(
                f"Inworld Realtime readiness probe timed out after {timeout:g} seconds.",
                status_code=504,
            ) from exc
        except (aiohttp.ClientError, ValueError, TypeError) as exc:
            raise InworldError("Inworld Realtime readiness probe could not be completed.") from exc

    async def router_readiness_probe(self, *, model_id: str) -> None:
        """Prove Router can execute the tool calls every VAV knowledge agent needs."""

        selected_model = _probe_value(model_id, "Router model")
        max_tokens = (
            ROUTER_AUTO_PROBE_MAX_TOKENS if selected_model == "auto" else ROUTER_PROBE_MAX_TOKENS
        )
        payload = await self._bounded_probe_json(
            path="/v1/chat/completions",
            probe_name="Router readiness probe",
            body={
                "model": selected_model,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Call {ROUTER_TOOL_NAME} now. Do not reply with text.",
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": ROUTER_TOOL_NAME,
                            "description": "Confirms tool-calling capability.",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": ROUTER_TOOL_NAME},
                },
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
        tool_calls = message.get("tool_calls")
        matching_calls = [
            call
            for call in tool_calls or []
            if isinstance(call, dict)
            and isinstance(call.get("function"), dict)
            and call["function"].get("name") == ROUTER_TOOL_NAME
        ]
        if not matching_calls:
            raise InworldError(
                "Inworld Router model did not return the required VAV knowledge tool call.",
                status_code=422,
            )
        try:
            arguments = json.loads(matching_calls[0]["function"].get("arguments") or "")
        except (TypeError, ValueError) as exc:
            raise InworldError(
                "Inworld Router returned invalid tool-call arguments.", status_code=422
            ) from exc
        if not isinstance(arguments, dict):
            raise InworldError(
                "Inworld Router returned invalid tool-call arguments.", status_code=422
            )

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
