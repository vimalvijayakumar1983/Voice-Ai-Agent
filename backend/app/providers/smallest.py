"""Async client for the Smallest.ai Atoms API.

The adapter keeps provider calls behind our backend so the raw Smallest API key
is never exposed to a browser or stored on an agent record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()


class SmallestAIError(RuntimeError):
    """A normalized Smallest.ai API failure."""

    def __init__(self, message: str, *, status_code: int = 502, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


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
    ):
        self.api_key = api_key if api_key is not None else settings.smallest_api_key
        self.base_url = (base_url or settings.smallest_base_url).rstrip("/")
        self.timeout = timeout or settings.smallest_request_timeout_seconds
        self.transport = transport

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
    ) -> dict[str, Any]:
        if not self.api_key:
            raise SmallestAIError(
                "Smallest.ai is not configured. Add SMALLEST_API_KEY to the backend environment.",
                status_code=503,
            )

        async with httpx.AsyncClient(
            base_url=self.base_url,
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
                    "Smallest.ai timed out. Please retry.", status_code=504
                ) from exc
            except httpx.HTTPError as exc:
                raise SmallestAIError("Could not connect to Smallest.ai.") from exc

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
            raise SmallestAIError(message, status_code=response.status_code, details=details)

        if response.status_code == 204 or not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise SmallestAIError("Smallest.ai returned an unexpected response.")
        return payload

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
            raise SmallestAIError("Smallest.ai did not return an agent ID.")
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

    async def update_agent_draft(
        self,
        *,
        agent_id: str,
        branch_id: str,
        global_prompt: str,
        first_message: str | None,
        slm_model: str,
        language: str,
        timezone: str,
        voice_id: str | None = None,
        speech_rate: float = 1.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "globalPrompt": global_prompt,
            "slmModel": slm_model,
            "language": {"default": language, "supported": [language]},
            "timezone": timezone,
            "allowInterruptions": True,
        }
        if first_message is not None:
            payload["firstMessage"] = first_message
        if voice_id:
            payload["synthesizer"] = {
                "voiceConfig": {
                    "model": "waves_lightning_v3_1",
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
        return data if isinstance(data, dict) else {"state": data}

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
            raise SmallestAIError("Smallest.ai did not return a conversation ID.")
        return str(conversation_id)


def get_smallest_client() -> SmallestAIClient:
    return SmallestAIClient()
