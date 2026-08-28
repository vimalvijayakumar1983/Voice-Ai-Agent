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
@pytest.mark.parametrize("provider_status", [401, 403])
async def test_provider_auth_failures_are_not_exposed_as_application_auth_failures(
    provider_status: int,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(provider_status, json={"message": "provider auth failed"})

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    with pytest.raises(SmallestAIError) as error:
        await client.create_agent(name="Provider auth boundary")

    assert error.value.status_code == 502
    assert error.value.upstream_status_code == provider_status
    assert str(error.value) == (
        "Smallest.ai rejected the configured server credentials or permissions."
    )
    assert error.value.ambiguous is False


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
        supported_languages=["en", "hi"],
        timezone="Asia/Dubai",
        voice_id="nyah",
        speech_rate=1.1,
        synthesizer_model="waves_lightning_v3_1",
    )

    assert captured["globalPrompt"] == "Be concise and helpful."
    assert captured["slmModel"] == "electron"
    assert captured["language"] == {"default": "en", "supported": ["en", "hi"]}
    assert captured["synthesizer"]["voiceConfig"]["voiceId"] == "nyah"
    assert captured["synthesizer"]["voiceConfig"]["model"] == "waves_lightning_v3_1"
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
            supported_languages=["en"],
            timezone="Not/A_Timezone",
        )

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_waves_catalog_and_voice_clones_use_current_endpoints():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/get_voices"):
            return httpx.Response(200, json={"voices": [{"voiceId": "emily"}]})
        return httpx.Response(200, json={"data": [{"voiceId": "my_clone"}]})

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )

    voices = await client.list_voices()
    clones = await client.list_voice_clones()

    assert voices == [{"voiceId": "emily"}]
    assert clones == [{"voiceId": "my_clone"}]
    assert paths == [
        "/waves/v1/lightning-v3.1/get_voices",
        "/waves/v1/voice-cloning",
    ]


@pytest.mark.asyncio
async def test_versioned_draft_rejects_tamil_in_multilingual_agent():
    client = SmallestAIClient(api_key="sk_test")

    with pytest.raises(SmallestAIError) as error:
        await client.update_agent_draft(
            agent_id="agent_123",
            branch_id="branch_123",
            global_prompt="Be concise and helpful.",
            first_message="Welcome.",
            slm_model="electron",
            language="ta",
            supported_languages=["ta", "en"],
            timezone="Asia/Kolkata",
        )

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_revision_lifecycle_and_webhook_subscription_contracts():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/webhook-subscriptions"):
            return httpx.Response(201, json={"status": True})
        if request.url.path.endswith("/revisions"):
            return httpx.Response(
                200,
                json={"data": {"revisions": [{"revision": {"_id": "revision_9"}}]}},
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "revision": {
                        "_id": "revision_9",
                        "status": "published",
                        "securityCheck": {"status": "passed"},
                    }
                }
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))
    await client.set_agent_webhook_subscriptions(
        agent_id="agent_123",
        webhook_id="webhook_123",
    )
    latest = await client.get_latest_branch_revision(
        agent_id="agent_123",
        branch_id="branch_123",
    )
    revision = await client.get_branch_revision(
        agent_id="agent_123",
        branch_id="branch_123",
        revision_id="revision_9",
    )

    assert json.loads(requests[0].content) == {
        "eventTypes": [
            "pre-conversation",
            "post-conversation",
            "analytics-completed",
        ],
        "webhookId": "webhook_123",
    }
    assert latest == {"_id": "revision_9"}
    assert revision["securityCheck"]["status"] == "passed"
    assert requests[1].url.params["limit"] == "1"


@pytest.mark.asyncio
async def test_mutating_timeout_is_marked_as_ambiguous():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))
    with pytest.raises(SmallestAIError) as error:
        await client.create_agent(name="Uncertain create")

    assert error.value.status_code == 504
    assert error.value.ambiguous is True
