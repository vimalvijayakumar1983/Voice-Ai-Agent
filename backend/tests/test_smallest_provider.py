import json

import httpx
import pytest

from app.providers.smallest import SmallestAIClient, SmallestAIError


@pytest.mark.asyncio
async def test_create_agent_and_browser_session_contract():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/agent"):
            return httpx.Response(200, json={"status": True, "data": "agent_123"})
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"access_token": "wct_test", "expires_in": 30, "sample_rate": 24000},
            },
        )

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )
    agent_id = await client.create_agent(
        name="Receptionist",
        description="Handles customer calls.",
    )
    session = await client.create_browser_session(
        agent_id=agent_id, variables={"customer_name": "Vimal"}
    )

    assert agent_id == "agent_123"
    assert session.access_token == "wct_test"
    assert requests[0].headers["authorization"] == "Bearer sk_test"
    assert json.loads(requests[0].content) == {
        "name": "Receptionist",
        "description": "Handles customer calls.",
    }
    assert json.loads(requests[1].content)["variables"] == {"customer_name": "Vimal"}


@pytest.mark.asyncio
async def test_outbound_call_uses_production_endpoint_and_scalar_variables():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"conversationId": "CALL-123"}})

    client = SmallestAIClient(
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )
    conversation_id = await client.start_outbound_call(
        agent_id="agent_123",
        phone_number="+971501234567",
        variables={"lead_name": "Aisha", "priority": 1},
        version_id="version_1",
    )

    assert conversation_id == "CALL-123"
    assert captured["path"].endswith("/conversation/outbound")
    assert captured["body"]["agentId"] == "agent_123"
    assert captured["body"]["phoneNumber"] == "+971501234567"
    assert captured["body"]["versionId"] == "version_1"


@pytest.mark.asyncio
async def test_missing_api_key_is_a_service_configuration_error():
    client = SmallestAIClient(api_key="")
    with pytest.raises(SmallestAIError) as error:
        await client.create_agent(name="Test")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_versioned_draft_contains_runtime_configuration():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": True, "data": {"status": "draft"}})

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))
    await client.update_agent_draft(
        agent_id="agent_123",
        branch_id="branch_123",
        global_prompt="Be concise and helpful.",
        first_message="Welcome.",
        slm_model="electron",
        language="en",
        timezone="Asia/Dubai",
        voice_id="nyah",
        speech_rate=1.1,
    )

    assert captured["globalPrompt"] == "Be concise and helpful."
    assert captured["slmModel"] == "electron"
    assert captured["language"] == {"default": "en", "supported": ["en"]}
    assert captured["synthesizer"]["voiceConfig"]["voiceId"] == "nyah"
    assert captured["timezone"] == {
        "label": "(GMT+4:00) Asia/Dubai",
        "offset": 4,
    }


@pytest.mark.asyncio
async def test_versioned_draft_rejects_unknown_timezone_before_request():
    client = SmallestAIClient(
        api_key="sk_test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    with pytest.raises(SmallestAIError) as error:
        await client.update_agent_draft(
            agent_id="agent_123",
            branch_id="branch_123",
            global_prompt="Be concise and helpful.",
            first_message="Welcome.",
            slm_model="electron",
            language="en",
            timezone="Not/A_Timezone",
        )

    assert error.value.status_code == 422
