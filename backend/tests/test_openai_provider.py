"""Bounded OpenAI realtime capability checks."""

import json

import httpx
import pytest

from app.providers.openai import OpenAIProviderClient, OpenAIProviderError


@pytest.mark.asyncio
async def test_openai_readiness_requires_named_tool_call_without_exposing_key():
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(await request.aread())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "vav_readiness_check",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    client = OpenAIProviderClient(
        api_key="tenant-openai-secret",
        transport=httpx.MockTransport(handler),
    )
    await client.tool_readiness_probe(model_id="gpt-4o-mini")

    assert captured["authorization"] == "Bearer tenant-openai-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "gpt-4o-mini"
    assert body["max_tokens"] == 64
    assert body["tool_choice"]["function"]["name"] == "vav_readiness_check"


@pytest.mark.asyncio
async def test_openai_readiness_fails_closed_without_echoing_provider_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "echo tenant-openai-secret"}})

    client = OpenAIProviderClient(
        api_key="tenant-openai-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAIProviderError, match="HTTP 400") as caught:
        await client.tool_readiness_probe(model_id="gpt-4o-mini")

    assert "tenant-openai-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_openai_readiness_rejects_plain_text_success():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    client = OpenAIProviderClient(
        api_key="tenant-openai-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAIProviderError, match="required tool call"):
        await client.tool_readiness_probe(model_id="gpt-4o-mini")
