"""Async client for the Smallest.ai Atoms API.

The adapter keeps provider calls behind our backend so the raw Smallest API key
is never exposed to a browser or stored on an agent record.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()

ATOMS_SYNTHESIZER_MODEL = "waves_lightning_v3_1"
VOICE_PREVIEW_TEXT = "Hello. This is a preview of my voice."
VOICE_PREVIEW_TEXTS = {
    "ar": "مرحبًا. هذه معاينة للصوت.",
    "bn": "নমস্কার। এটি কণ্ঠস্বরের একটি নমুনা।",
    "de": "Hallo. Dies ist eine Stimmvorschau.",
    "el": "Γεια σας. Αυτή είναι μια προεπισκόπηση φωνής.",
    "en": VOICE_PREVIEW_TEXT,
    "es": "Hola. Esta es una vista previa de la voz.",
    "fi": "Hei. Tämä on äänen esikatselu.",
    "fr": "Bonjour. Ceci est un aperçu de la voix.",
    "gu": "નમસ્તે. આ અવાજનું પૂર્વદર્શન છે.",
    "hi": "नमस्ते। यह आवाज़ का पूर्वावलोकन है।",
    "id": "Halo. Ini adalah pratinjau suara.",
    "it": "Ciao. Questa è un'anteprima della voce.",
    "ja": "こんにちは。これは音声のプレビューです。",
    "kn": "ನಮಸ್ಕಾರ. ಇದು ಧ್ವನಿಯ ಮುನ್ನೋಟವಾಗಿದೆ.",
    "ko": "안녕하세요. 음성 미리 듣기입니다.",
    "ml": "നമസ്കാരം. ഇത് ശബ്ദത്തിന്റെ പ്രിവ്യൂ ആണ്.",
    "mr": "नमस्कार. हा आवाजाचा नमुना आहे.",
    "ms": "Helo. Ini ialah pratonton suara.",
    "nl": "Hallo. Dit is een stemvoorbeeld.",
    "no": "Hei. Dette er en forhåndsvisning av stemmen.",
    "or": "ନମସ୍କାର। ଏହା କଣ୍ଠସ୍ୱରର ଏକ ନମୁନା।",
    "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ। ਇਹ ਆਵਾਜ਼ ਦੀ ਝਲਕ ਹੈ।",
    "pl": "Dzień dobry. To jest podgląd głosu.",
    "pt": "Olá. Esta é uma prévia da voz.",
    "ru": "Здравствуйте. Это предварительный просмотр голоса.",
    "sv": "Hej. Det här är en förhandsvisning av rösten.",
    "ta": "வணக்கம். இது குரல் முன்னோட்டம்.",
    "te": "నమస్కారం. ఇది వాయిస్ ప్రివ్యూ.",
    "tr": "Merhaba. Bu bir ses önizlemesidir.",
    "vi": "Xin chào. Đây là bản nghe thử giọng nói.",
    "zh": "你好。这是一段语音试听。",
}
MAX_VOICE_PREVIEW_BYTES = 2_000_000
MAX_KNOWLEDGE_BASE_DESCRIPTION_CHARS = 150
_KNOWLEDGE_BINDING_UNSET = object()


def _provider_error_message(details: Any, fallback: str) -> str:
    """Extract a bounded human-readable message from provider error envelopes."""

    def extract(value: Any, *, depth: int = 0) -> list[str]:
        if depth > 4:
            return []
        if isinstance(value, str):
            normalized = " ".join(value.split())
            return [normalized] if normalized else []
        if isinstance(value, list):
            messages: list[str] = []
            for item in value[:5]:
                messages.extend(extract(item, depth=depth + 1))
            return messages
        if isinstance(value, dict):
            for key in ("message", "error", "detail", "errors"):
                if key in value:
                    messages = extract(value[key], depth=depth + 1)
                    if messages:
                        return messages
        return []

    messages = list(dict.fromkeys(extract(details))) if isinstance(details, dict) else []
    return ("; ".join(messages) or fallback)[:500]


def _provider_knowledge_description(description: str) -> str:
    """Fit local governance notes into Smallest.ai's 150-character field."""
    return " ".join(description.split())[:MAX_KNOWLEDGE_BASE_DESCRIPTION_CHARS]


def _validate_timezone_name(timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SmallestAIError(f"Unknown IANA timezone: {timezone_name}", status_code=422) from exc


def _validate_language_switching(
    supported_languages: list[str],
    *,
    enabled: bool,
    mode: str,
) -> None:
    if enabled != (mode == "automatic"):
        raise SmallestAIError(
            "Language switching mode and enabled state are inconsistent.",
            status_code=422,
        )
    if enabled and len(supported_languages) < 2:
        raise SmallestAIError(
            "Automatic language switching requires at least two supported languages.",
            status_code=422,
        )


class SmallestAIError(RuntimeError):
    """A normalized Smallest.ai API failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        upstream_status_code: int | None = None,
        details: Any = None,
        ambiguous: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        # Keep the provider's raw status for internal dispatch classification
        # while exposing only an application-safe status to API clients.
        self.upstream_status_code = upstream_status_code
        self.details = details
        # A timeout/connection loss after a mutating request may mean Smallest
        # accepted the operation even though we never received its response.
        # Callers must reconcile these failures instead of blindly retrying.
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class BrowserSession:
    access_token: str
    expires_in: int
    sample_rate: int


class SmallestAIClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        waves_base_url: str | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.smallest_api_key
        self.base_url = (base_url or settings.smallest_base_url).rstrip("/")
        self.timeout = timeout or settings.smallest_request_timeout_seconds
        self.transport = transport
        self.waves_base_url = (waves_base_url or settings.smallest_waves_base_url).rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise SmallestAIError(
                "Smallest.ai is not configured. Add SMALLEST_API_KEY to the backend environment.",
                status_code=503,
            )

        mutation = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        async with httpx.AsyncClient(
            base_url=base_url or self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        ) as client:
            try:
                response = await client.request(method, path, json=json, params=params)
            except httpx.TimeoutException as exc:
                raise SmallestAIError(
                    "Smallest.ai timed out. Please retry.",
                    status_code=504,
                    ambiguous=mutation,
                ) from exc
            except httpx.HTTPError as exc:
                raise SmallestAIError(
                    "Could not connect to Smallest.ai.",
                    ambiguous=mutation,
                ) from exc

        if response.is_error:
            try:
                details: Any = response.json()
            except ValueError:
                details = response.text[:500]
            logger.warning(
                "smallest_api_error",
                method=method,
                path=path,
                status_code=response.status_code,
            )
            message = _provider_error_message(details, "Smallest.ai rejected the request.")
            public_status_code = response.status_code
            if response.status_code in {401, 403}:
                # These credentials belong to our server, not the signed-in
                # operator. Never surface a provider authentication status as
                # an application authentication failure: the frontend treats
                # our own 401 as a signal to rotate or clear the user session.
                public_status_code = 502
                message = "Smallest.ai rejected the configured server credentials or permissions."
            raise SmallestAIError(
                message,
                status_code=public_status_code,
                upstream_status_code=response.status_code,
                details=details,
                ambiguous=mutation and (response.status_code == 408 or response.status_code >= 500),
            )

        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmallestAIError(
                "Smallest.ai returned an invalid JSON response.",
                ambiguous=mutation,
            ) from exc
        if not isinstance(payload, dict):
            raise SmallestAIError(
                "Smallest.ai returned an unexpected response.",
                ambiguous=mutation,
            )
        return payload

    async def _request_audio(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
        base_url: str,
    ) -> bytes:
        """Return a bounded WAV response while preserving provider error hygiene."""
        if not self.api_key:
            raise SmallestAIError(
                "Smallest.ai is not configured. Add SMALLEST_API_KEY to the backend environment.",
                status_code=503,
            )

        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=self.timeout,
            transport=self.transport,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "audio/wav",
                "Content-Type": "application/json",
            },
        ) as client:
            try:
                async with client.stream(method, path, json=payload) as response:
                    if response.is_error:
                        error_body = bytearray()
                        async for chunk in response.aiter_bytes():
                            remaining = 16_384 - len(error_body)
                            if remaining <= 0:
                                break
                            error_body.extend(chunk[:remaining])
                        try:
                            details: Any = jsonlib.loads(error_body)
                        except (UnicodeDecodeError, jsonlib.JSONDecodeError):
                            details = bytes(error_body[:500]).decode("utf-8", errors="replace")
                        message = _provider_error_message(
                            details,
                            "Smallest.ai rejected the voice preview request.",
                        )
                        public_status_code = response.status_code
                        if response.status_code in {401, 403}:
                            public_status_code = 502
                            message = (
                                "Smallest.ai rejected the configured server credentials "
                                "or permissions."
                            )
                        raise SmallestAIError(
                            message,
                            status_code=public_status_code,
                            upstream_status_code=response.status_code,
                            details=details,
                        )

                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {
                        "audio/wav",
                        "audio/x-wav",
                        "application/octet-stream",
                    }:
                        raise SmallestAIError(
                            "Smallest.ai returned an unexpected voice preview response."
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > MAX_VOICE_PREVIEW_BYTES:
                                raise SmallestAIError(
                                    "Smallest.ai returned an oversized voice preview."
                                )
                        except ValueError:
                            pass

                    audio = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(audio) + len(chunk) > MAX_VOICE_PREVIEW_BYTES:
                            raise SmallestAIError(
                                "Smallest.ai returned an oversized voice preview."
                            )
                        audio.extend(chunk)
            except httpx.TimeoutException as exc:
                raise SmallestAIError(
                    "Smallest.ai timed out. Please retry.", status_code=504
                ) from exc
            except httpx.HTTPError as exc:
                raise SmallestAIError("Could not connect to Smallest.ai.") from exc

        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise SmallestAIError("Smallest.ai returned an invalid WAV voice preview.")
        return bytes(audio)

    async def _request_media(
        self,
        method: str,
        path: str,
        *,
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload one bounded server-validated media item without exposing credentials."""
        if not self.api_key:
            raise SmallestAIError(
                "Smallest.ai is not configured. Add SMALLEST_API_KEY to the backend environment.",
                status_code=503,
            )
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        ) as client:
            try:
                response = await client.request(
                    method,
                    path,
                    files={"media": (file_name, content, content_type)},
                )
            except httpx.TimeoutException as exc:
                raise SmallestAIError(
                    "Smallest.ai timed out while uploading the knowledge source.",
                    status_code=504,
                    ambiguous=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise SmallestAIError(
                    "Could not connect to Smallest.ai while uploading the knowledge source.",
                    ambiguous=True,
                ) from exc
        if response.is_error:
            try:
                details: Any = response.json()
            except ValueError:
                details = response.text[:500]
            message = _provider_error_message(
                details,
                "Smallest.ai rejected the knowledge source upload.",
            )
            if response.status_code in {401, 403}:
                message = "Smallest.ai rejected the configured server credentials or permissions."
            raise SmallestAIError(
                message,
                status_code=502 if response.status_code in {401, 403} else response.status_code,
                upstream_status_code=response.status_code,
                details=details,
                ambiguous=response.status_code == 408 or response.status_code >= 500,
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmallestAIError(
                "Smallest.ai returned an invalid knowledge upload response.", ambiguous=True
            ) from exc
        if not isinstance(payload, dict):
            raise SmallestAIError(
                "Smallest.ai returned an unexpected knowledge upload response.", ambiguous=True
            )
        return payload

    async def list_voices(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "/lightning-v3.1/get_voices",
            base_url=self.waves_base_url,
        )
        voices = response.get("voices") or response.get("data") or []
        return voices if isinstance(voices, list) else []

    async def list_voice_clones(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/voice-cloning", base_url=self.waves_base_url)
        voices = response.get("data") or response.get("voices") or []
        return voices if isinstance(voices, list) else []

    async def synthesize_voice_preview(
        self,
        *,
        voice_id: str,
        model: str,
        language: str,
    ) -> bytes:
        """Synthesize one server-owned preview phrase; callers cannot supply billable text."""
        if model not in {"lightning_v3.1", "lightning_v3.1_pro"}:
            raise SmallestAIError("Voice preview model could not be verified.", status_code=422)
        base_language = language.split("-", 1)[0]
        provider_language = base_language if base_language in VOICE_PREVIEW_TEXTS else language
        return await self._request_audio(
            "POST",
            "/tts",
            base_url=self.waves_base_url,
            payload={
                "text": VOICE_PREVIEW_TEXTS.get(
                    provider_language,
                    VOICE_PREVIEW_TEXT,
                ),
                "voice_id": voice_id,
                "model": model,
                "language": provider_language,
                "output_format": "wav",
            },
        )

    async def create_agent(
        self,
        *,
        name: str,
        description: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"name": name}
        if description:
            payload["description"] = description
        response = await self._request("POST", "/agent", json=payload)
        agent_id = response.get("data")
        if isinstance(agent_id, dict):
            agent_id = agent_id.get("_id") or agent_id.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            raise SmallestAIError(
                "Smallest.ai accepted the request but did not return an agent ID.",
                ambiguous=True,
            )
        return agent_id

    async def create_knowledge_base(self, *, name: str, description: str = "") -> str:
        payload: dict[str, Any] = {"name": name}
        if description:
            payload["description"] = _provider_knowledge_description(description)
        response = await self._request("POST", "/knowledgebase", json=payload)
        knowledge_base_id = response.get("data")
        if isinstance(knowledge_base_id, dict):
            knowledge_base_id = knowledge_base_id.get("_id") or knowledge_base_id.get("id")
        if not isinstance(knowledge_base_id, str) or not knowledge_base_id:
            raise SmallestAIError(
                "Smallest.ai accepted the request but did not return a knowledge base ID.",
                ambiguous=True,
            )
        return knowledge_base_id

    async def update_knowledge_base(
        self, *, knowledge_base_id: str, name: str, description: str = ""
    ) -> None:
        await self._request(
            "POST",
            f"/knowledgebase/{quote(knowledge_base_id, safe='')}",
            json={"name": name, "description": _provider_knowledge_description(description)},
        )

    async def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        await self._request("DELETE", f"/knowledgebase/{quote(knowledge_base_id, safe='')}")

    async def discover_sitemap_urls(self, *, knowledge_base_id: str, sitemap_url: str) -> list[str]:
        response = await self._request(
            "POST",
            "/knowledgebase/get-sitemap-urls",
            json={"siteUrl": sitemap_url, "knowledgeBaseId": knowledge_base_id},
        )
        data = response.get("data")
        urls = data.get("urls") if isinstance(data, dict) else None
        if not isinstance(urls, list):
            raise SmallestAIError("Smallest.ai returned an invalid sitemap response.")
        return [str(url) for url in urls if isinstance(url, str)][:1000]

    async def scrape_knowledge_urls(self, *, knowledge_base_id: str, urls: list[str]) -> None:
        await self._request(
            "POST",
            f"/knowledgebase/{quote(knowledge_base_id, safe='')}/scrape-urls",
            json={"urls": urls},
        )

    async def list_scraped_knowledge_urls(self, knowledge_base_id: str) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/knowledgebase/{quote(knowledge_base_id, safe='')}/scraped-urls"
        )
        data = response.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def list_knowledge_items(self, knowledge_base_id: str) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"/knowledgebase/{quote(knowledge_base_id, safe='')}/items"
        )
        data = response.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def upload_knowledge_pdf(
        self, *, knowledge_base_id: str, file_name: str, content: bytes
    ) -> None:
        await self._request_media(
            "POST",
            f"/knowledgebase/{quote(knowledge_base_id, safe='')}/items/upload-media",
            file_name=file_name,
            content=content,
            content_type="application/pdf",
        )

    async def get_agent(
        self,
        agent_id: str,
        *,
        version_id: str | None = None,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the agent with the provider-resolved version configuration."""
        if version_id and draft_id:
            raise ValueError("version_id and draft_id are mutually exclusive")
        params: dict[str, str] = {}
        if version_id:
            params["versionId"] = version_id
        if draft_id:
            params["draftId"] = draft_id
        response = await self._request(
            "GET",
            f"/agent/{agent_id}",
            params=params or None,
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise SmallestAIError("Smallest.ai returned an invalid agent configuration.")
        return data

    async def get_agent_knowledge_base_id(self, agent_id: str) -> str | None:
        """Return the active provider KB binding without confusing absence with an empty ID."""
        provider_agent = await self.get_agent(agent_id)
        resolved = provider_agent.get("_resolvedConfig")
        configurations = [provider_agent]
        if isinstance(resolved, dict):
            configurations.append(resolved)
        for configuration in configurations:
            for key in ("globalKnowledgeBaseId", "global_knowledge_base_id"):
                if key not in configuration:
                    continue
                value = configuration[key]
                if value is None or value == "":
                    return None
                if isinstance(value, str):
                    return value
                raise SmallestAIError("Smallest.ai returned an invalid knowledge-base binding.")
        return None

    async def get_default_branch_id(self, agent_id: str) -> str:
        response = await self._request("GET", f"/agent/{agent_id}/branches")
        data = response.get("data") or {}
        branches = data.get("branches", []) if isinstance(data, dict) else []
        for summary in branches:
            branch = summary.get("branch", {}) if isinstance(summary, dict) else {}
            if branch.get("isDefault"):
                branch_id = branch.get("_id") or branch.get("id")
                if branch_id:
                    return str(branch_id)
        raise SmallestAIError("Smallest.ai agent has no default branch.")

    async def set_agent_webhook_subscriptions(
        self,
        *,
        agent_id: str,
        webhook_id: str,
    ) -> dict[str, Any]:
        """Replace an agent's subscriptions with the required lifecycle events."""
        return await self._request(
            "POST",
            f"/agent/{agent_id}/webhook-subscriptions",
            json={
                "eventTypes": [
                    "pre-conversation",
                    "post-conversation",
                    "analytics-completed",
                ],
                "webhookId": webhook_id,
            },
        )

    async def update_agent_draft(
        self,
        *,
        agent_id: str,
        branch_id: str,
        global_prompt: str,
        first_message: str | None,
        slm_model: str,
        language: str,
        supported_languages: list[str] | None,
        language_switching_enabled: bool = False,
        language_switching_mode: str = "disabled",
        timezone: str,
        voice_id: str | None = None,
        speech_rate: float = 1.0,
        synthesizer_model: str | None = None,
        max_call_duration_seconds: int = 600,
        global_knowledge_base_id: str | None | object = _KNOWLEDGE_BINDING_UNSET,
    ) -> dict[str, Any]:
        resolved_languages = list(dict.fromkeys(supported_languages or [language]))
        if language not in resolved_languages:
            raise SmallestAIError(
                "Primary language must be included in supported languages.", status_code=422
            )
        _validate_language_switching(
            resolved_languages,
            enabled=language_switching_enabled,
            mode=language_switching_mode,
        )
        _validate_timezone_name(timezone)

        payload: dict[str, Any] = {
            "globalPrompt": global_prompt,
            "firstMessage": first_message or "",
            "slmModel": slm_model,
            "language": {
                "default": language,
                "supported": resolved_languages,
                "switching": {"isEnabled": language_switching_enabled},
            },
            # The current versioned-draft endpoint accepts an IANA timezone
            # string. The create-agent DTO still documents an offset object,
            # but this adapter writes only through the draft endpoint.
            "timezone": timezone,
            "allowInterruptions": True,
            "sessionTimeoutConfig": {
                "timeoutTimeInSecs": max_call_duration_seconds,
            },
        }
        if global_knowledge_base_id is not _KNOWLEDGE_BINDING_UNSET:
            payload["globalKnowledgeBaseId"] = global_knowledge_base_id
        if voice_id:
            if not synthesizer_model:
                raise SmallestAIError(
                    "Selected voice has no verified Smallest.ai synthesizer model.",
                    status_code=422,
                )
            payload["synthesizer"] = {
                "voiceConfig": {
                    "model": synthesizer_model,
                    "voiceId": voice_id,
                },
                "speed": speech_rate,
            }
        return await self._request(
            "PUT", f"/agent/{agent_id}/branches/{branch_id}/draft", json=payload
        )

    async def publish_draft(self, *, agent_id: str, branch_id: str, label: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/agent/{agent_id}/branches/{branch_id}/draft/publish",
            json={"label": label},
        )
        data = response.get("data")
        result = data if isinstance(data, dict) else {"state": data}
        state = str(result.get("state") or "").lower()
        if state not in {"committed", "scanning"}:
            raise SmallestAIError(
                "Smallest.ai accepted the publish request but returned an unknown state.",
                details=result,
                ambiguous=True,
            )
        return {**result, "state": state}

    async def get_open_branch_draft(
        self,
        *,
        agent_id: str,
        branch_id: str,
    ) -> dict[str, Any] | None:
        """Return the currently open draft, or ``None`` when none exists."""
        try:
            response = await self._request(
                "GET",
                f"/agent/{agent_id}/branches/{branch_id}/draft",
            )
        except SmallestAIError as exc:
            if exc.upstream_status_code == 404:
                return None
            raise

        data = response.get("data")
        draft = data.get("latest") if isinstance(data, dict) else None
        if not isinstance(draft, dict):
            raise SmallestAIError("Smallest.ai returned an invalid open draft.")
        return draft

    async def get_latest_branch_revision(
        self,
        *,
        agent_id: str,
        branch_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest revision summary for a branch, if one exists."""
        response = await self._request(
            "GET",
            f"/agent/{agent_id}/branches/{branch_id}/revisions",
            params={"limit": 1},
        )
        data = response.get("data")
        revisions = data.get("revisions") if isinstance(data, dict) else None
        if revisions is None and isinstance(data, list):
            revisions = data
        if not isinstance(revisions, list) or not revisions:
            return None
        latest = revisions[0]
        if not isinstance(latest, dict):
            raise SmallestAIError("Smallest.ai returned an invalid revision summary.")
        revision = latest.get("revision")
        return revision if isinstance(revision, dict) else latest

    async def get_branch_revision(
        self,
        *,
        agent_id: str,
        branch_id: str,
        revision_id: str,
    ) -> dict[str, Any]:
        """Return a revision including publish and security-check status."""
        response = await self._request(
            "GET",
            f"/agent/{agent_id}/branches/{branch_id}/revisions/{revision_id}",
        )
        data = response.get("data")
        revision = data.get("revision") if isinstance(data, dict) else None
        if revision is None and isinstance(data, dict):
            revision = data
        if not isinstance(revision, dict):
            raise SmallestAIError("Smallest.ai returned an invalid revision.")
        return revision

    async def create_browser_session(
        self,
        *,
        agent_id: str,
        variables: dict[str, str | int | float | bool] | None = None,
    ) -> BrowserSession:
        response = await self._request(
            "POST",
            "/conversation/register-call",
            json={"agent_id": agent_id, "mode": "webcall", "variables": variables or {}},
        )
        data = response.get("data") or {}
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SmallestAIError("Smallest.ai did not return a browser access token.")
        return BrowserSession(
            access_token=str(data["access_token"]),
            expires_in=int(data.get("expires_in", 30)),
            sample_rate=int(data.get("sample_rate", 24000)),
        )

    async def start_outbound_call(
        self,
        *,
        agent_id: str,
        phone_number: str,
        variables: dict[str, str | int | float | bool] | None = None,
        from_product_id: str | None = None,
        version_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "agentId": agent_id,
            "phoneNumber": phone_number,
            "variables": variables or {},
        }
        if from_product_id:
            payload["fromProductId"] = from_product_id
        if version_id:
            payload["versionId"] = version_id
        response = await self._request("POST", "/conversation/outbound", json=payload)
        data = response.get("data") or {}
        conversation_id = (data.get("conversationId") if isinstance(data, dict) else None) or (
            data.get("conversation_id") if isinstance(data, dict) else None
        )
        if not conversation_id:
            raise SmallestAIError(
                "Smallest.ai accepted the call request but did not return a conversation ID.",
                ambiguous=True,
            )
        return str(conversation_id)

    async def get_recording_download_url(self, *, call_id: str) -> str:
        """Request a fresh, time-limited recording URL for one conversation.

        Provider contract: Smallest documents
        ``GET /conversation/{callId}/recording/download-url`` with
        ``data.presignedUrl``. The returned URL is deliberately kept inside the
        backend and must not be cached or returned to browser clients.
        """
        encoded_call_id = quote(call_id, safe="")
        response = await self._request(
            "GET",
            f"/conversation/{encoded_call_id}/recording/download-url",
        )
        data = response.get("data")
        presigned_url = data.get("presignedUrl") if isinstance(data, dict) else None
        if not isinstance(presigned_url, str) or not presigned_url.strip():
            raise SmallestAIError("Smallest.ai did not return a recording download URL.")
        return presigned_url.strip()


def get_smallest_client() -> SmallestAIClient:
    return SmallestAIClient()
