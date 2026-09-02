"""Bounded OpenAI capability checks for the realtime voice runtime."""

from __future__ import annotations

import json
from typing import Any

import httpx

OPENAI_BASE_URL = "https://api.openai.com"
OPENAI_PROBE_TIMEOUT_SECONDS = 10.0
MAX_OPENAI_PROBE_BYTES = 256 * 1024
OPENAI_TOOL_NAME = "vav_readiness_check"


class OpenAIProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _probe_value(value: str, label: str, *, max_length: int = 100) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > max_length
        or any(ord(character) < 32 for character in normalized)
    ):
        raise OpenAIProviderError(f"OpenAI {label} is invalid.", status_code=422)
    return normalized


class OpenAIProviderClient:
    """Minimal server-side client that proves the selected model can call tools."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = OPENAI_BASE_URL,
        timeout: float = OPENAI_PROBE_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def tool_readiness_probe(self, *, model_id: str) -> None:
        """Require one named tool call using a tiny, non-streaming request."""
        if not self.api_key:
            raise OpenAIProviderError(
                "OpenAI is not configured. Add an API key in Settings.", status_code=503
            )
        model = _probe_value(model_id, "model")
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": f"Call {OPENAI_TOOL_NAME} now. Do not reply with text.",
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": OPENAI_TOOL_NAME,
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
                "function": {"name": OPENAI_TOOL_NAME},
            },
            "max_tokens": 64,
            "temperature": 0,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_OPENAI_PROBE_BYTES:
                            raise OpenAIProviderError(
                                "OpenAI tool readiness returned an unexpectedly large response."
                            )
        except httpx.TimeoutException as exc:
            raise OpenAIProviderError(
                "OpenAI tool readiness timed out.", status_code=504
            ) from exc
        except httpx.RequestError as exc:
            raise OpenAIProviderError("OpenAI could not be reached.") from exc

        if not 200 <= response.status_code < 300:
            # Do not include provider response bodies: they are unnecessary for
            # an operator-facing gate and may echo request/account information.
            code = response.status_code if response.status_code < 500 else 502
            raise OpenAIProviderError(
                f"OpenAI rejected the tool-calling readiness check (HTTP {response.status_code}).",
                status_code=code,
            )
        try:
            payload: Any = json.loads(bytes(content))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OpenAIProviderError("OpenAI tool readiness returned invalid JSON.") from exc
        choices = payload.get("choices") if isinstance(payload, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(first, dict) and first.get("finish_reason") == "length":
            raise OpenAIProviderError(
                "OpenAI tool readiness exhausted its bounded output token budget."
            )
        message = first.get("message") if isinstance(first, dict) else None
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        matching_calls = [
            call
            for call in tool_calls or []
            if isinstance(call, dict)
            and isinstance(call.get("function"), dict)
            and call["function"].get("name") == OPENAI_TOOL_NAME
        ]
        if not matching_calls:
            raise OpenAIProviderError(
                "OpenAI model did not return the required tool call.", status_code=422
            )
        try:
            arguments = json.loads(matching_calls[0]["function"].get("arguments") or "")
        except (TypeError, ValueError) as exc:
            raise OpenAIProviderError(
                "OpenAI model returned invalid tool-call arguments.", status_code=422
            ) from exc
        if not isinstance(arguments, dict):
            raise OpenAIProviderError(
                "OpenAI model returned invalid tool-call arguments.", status_code=422
            )
