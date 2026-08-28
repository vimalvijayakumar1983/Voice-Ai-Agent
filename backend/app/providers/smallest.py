"""Async client for the Smallest.ai Atoms API.

The adapter keeps provider calls behind our backend so the raw Smallest API key
is never exposed to a browser or stored on an agent record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()


def _timezone_config(timezone_name: str) -> dict[str, str | int | float]:
    """Translate an IANA zone into the object currently accepted by Atoms."""
    try:
        offset = datetime.now(ZoneInfo(timezone_name)).utcoffset()
    except ZoneInfoNotFoundError as exc:
        raise SmallestAIError(f"Unknown IANA timezone: {timezone_name}", status_code=422) from exc

    total_minutes = int((offset.total_seconds() if offset else 0) / 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    offset_hours = total_minutes / 60
    return {
        "label": f"(GMT{sign}{hours}:{minutes:02d}) {timezone_name}",
        "offset": int(offset_hours) if offset_hours.is_integer() else offset_hours,
    }


class SmallestAIError(RuntimeError):
    """A normalized Smallest.ai API failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        details: Any = None,
        ambiguous: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
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
        parsed_base_url = urlsplit(self.base_url)
        self.waves_base_url = waves_base_url or (
            f"{parsed_base_url.scheme}://{parsed_base_url.netloc}/waves/v1"
        )

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
                "Content-Type": "application/json",
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
            message = "Smallest.ai rejected the request."
            if isinstance(details, dict):
                message = str(details.get("message") or details.get("error") or message)
            raise SmallestAIError(
                message,
                status_code=response.status_code,
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
        timezone: str,
        voice_id: str | None = None,
        speech_rate: float = 1.0,
        synthesizer_model: str | None = None,
    ) -> dict[str, Any]:
        resolved_languages = list(dict.fromkeys(supported_languages or [language]))
        if language not in resolved_languages:
            raise SmallestAIError(
                "Primary language must be included in supported languages.", status_code=422
            )
        if "ta" in resolved_languages and len(resolved_languages) > 1:
            raise SmallestAIError(
                "Tamil cannot be combined with other supported languages.", status_code=422
            )

        payload: dict[str, Any] = {
            "globalPrompt": global_prompt,
            "slmModel": slm_model,
            "language": {"default": language, "supported": resolved_languages},
            "timezone": _timezone_config(timezone),
            "allowInterruptions": True,
        }
        if first_message is not None:
            payload["firstMessage"] = first_message
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


def get_smallest_client() -> SmallestAIClient:
    return SmallestAIClient()
