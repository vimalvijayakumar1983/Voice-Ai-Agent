import base64
import json

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_credential import ProviderCredential
from app.providers.sarvam import SarvamAIClient, SarvamAIError, sarvam_voice_catalog


@pytest.mark.asyncio
async def test_sarvam_bulbul_preview_contract_is_bounded_and_namespaced():
    requests: list[httpx.Request] = []
    wav = b"RIFF\x04\x00\x00\x00WAVE"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"request_id": "req-1", "audios": [base64.b64encode(wav).decode()]},
        )

    client = SarvamAIClient(
        api_key="sk_sarvam_test_key_123456",
        base_url="https://api.sarvam.ai",
        transport=httpx.MockTransport(handler),
    )
    audio = await client.synthesize_voice_preview(
        speaker="ishita",
        language="en",
    )

    assert audio == wav
    assert requests[0].url.path == "/text-to-speech"
    assert requests[0].headers["api-subscription-key"] == "sk_sarvam_test_key_123456"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "bulbul:v3"
    assert payload["language_code"] == "en-IN"
    assert payload["speaker"] == "ishita"
    assert any(voice["id"] == "sarvam:ishita" for voice in sarvam_voice_catalog())


@pytest.mark.asyncio
async def test_sarvam_preview_fails_closed_without_server_credential():
    client = SarvamAIClient(api_key="")

    with pytest.raises(SarvamAIError) as caught:
        await client.synthesize_voice_preview(speaker="ishita", language="en")

    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_workspace_sarvam_key_is_write_only_and_can_be_removed(
    client,
    auth_headers,
    db,
):
    api_key = "sk_workspace_sarvam_123456789"
    saved = await client.put(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
        json={"api_key": api_key},
    )

    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert saved.json()["source"] == "workspace"
    assert api_key not in saved.text

    credential = await db.scalar(select(ProviderCredential))
    assert credential is not None
    assert api_key not in credential.encrypted_config

    status = await client.get("/api/v1/agents/provider/status", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["providers"]["sarvam"]["source"] == "workspace"
    assert api_key not in status.text

    deleted = await client.delete(
        "/api/v1/agents/provider/sarvam/credential",
        headers=auth_headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["source"] in {"none", "platform"}


@pytest.mark.asyncio
async def test_sarvam_agent_is_created_locally_without_touching_smallest(
    client,
    auth_headers,
):
    response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Indian English concierge",
            "system_prompt": "Answer customer questions in concise Indian English.",
            "voice_provider": "sarvam",
            "voice_id": "sarvam:ishita",
            "language": "en",
            "supported_languages": ["en", "hi", "ml"],
        },
    )

    assert response.status_code == 201
    assert response.json()["voice_provider"] == "sarvam"
    assert response.json()["voice_id"] == "sarvam:ishita"
    assert response.json()["provider_agent_id"] is None
    assert response.json()["sync_status"] == "local_only"
