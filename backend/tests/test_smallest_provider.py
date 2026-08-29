import json

import httpx
import pytest

from app.providers.smallest import VOICE_PREVIEW_TEXTS, SmallestAIClient, SmallestAIError


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
        global_knowledge_base_id="kb_123",
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
        "workflowType": "single_prompt",
        "globalKnowledgeBaseId": "kb_123",
    }
    assert json.loads(requests[1].content)["variables"] == {"customer_name": "Vimal"}


@pytest.mark.asyncio
async def test_delete_agent_uses_archive_endpoint_and_is_idempotent_when_absent():
    requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(204),
            httpx.Response(404, json={"message": "Agent not found"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )

    await client.delete_agent("agent/with spaces")
    await client.delete_agent("already_absent")

    assert [request.method for request in requests] == ["DELETE", "DELETE"]
    assert requests[0].url.raw_path == b"/atoms/v1/agent/agent%2Fwith%20spaces/archive"
    assert requests[1].url.path == "/atoms/v1/agent/already_absent/archive"


@pytest.mark.asyncio
async def test_knowledge_base_url_ingestion_contract():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/knowledgebase"):
            return httpx.Response(201, json={"status": True, "data": "kb_123"})
        if request.url.path.endswith("/scrape-urls"):
            return httpx.Response(200, json={"status": True, "data": {}})
        return httpx.Response(200, json={"status": True, "data": []})

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )
    knowledge_base_id = await client.create_knowledge_base(
        name="FEPY Support",
        description="Approved product and policy content",
    )
    await client.scrape_knowledge_urls(
        knowledge_base_id=knowledge_base_id,
        urls=["https://www.fepy.com/delivery"],
    )

    assert knowledge_base_id == "kb_123"
    assert requests[0].url.path == "/atoms/v1/knowledgebase"
    assert json.loads(requests[0].content) == {
        "name": "FEPY Support",
        "description": "Approved product and policy content",
    }
    assert requests[1].url.path == "/atoms/v1/knowledgebase/kb_123/scrape-urls"
    assert json.loads(requests[1].content) == {"urls": ["https://www.fepy.com/delivery"]}


@pytest.mark.asyncio
async def test_knowledge_source_deletion_contracts_are_idempotent():
    requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(200, json={"status": True}),
            httpx.Response(200, json={"status": True}),
            httpx.Response(404, json={"message": "Already deleted"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )

    await client.delete_scraped_knowledge_url(
        knowledge_base_id="kb/123",
        scraped_url_id="scrape/456",
    )
    await client.delete_knowledge_item(
        knowledge_base_id="kb/123",
        item_id="item/789",
    )
    await client.delete_knowledge_item(
        knowledge_base_id="kb/123",
        item_id="already-absent",
    )

    assert [request.method for request in requests] == ["DELETE", "DELETE", "DELETE"]
    assert requests[0].url.raw_path == (
        b"/atoms/v1/knowledgebase/kb%2F123/scraped-urls/scrape%2F456"
    )
    assert requests[1].url.raw_path == b"/atoms/v1/knowledgebase/kb%2F123/items/item%2F789"
    assert [json.loads(request.content) for request in requests] == [{}, {}, {}]
    assert all(request.headers["content-type"] == "application/json" for request in requests)


@pytest.mark.asyncio
async def test_get_knowledge_base_contract():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"_id": "kb_123", "processingStatus": "completed"},
            },
        )

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )

    knowledge_base = await client.get_knowledge_base("kb_123")

    assert requests[0].url.path == "/atoms/v1/knowledgebase/kb_123"
    assert knowledge_base["processingStatus"] == "completed"


@pytest.mark.asyncio
async def test_knowledge_base_description_is_normalized_to_provider_limit():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"status": True, "data": "kb_123"})

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )
    await client.create_knowledge_base(
        name="FEPY Support",
        description="  Live   product data must come from tools.  " * 10,
    )

    description = json.loads(requests[0].content)["description"]
    assert len(description) == 150
    assert "  " not in description


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
async def test_recording_download_url_uses_conversation_call_id_contract():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "presignedUrl": "https://recordings.s3.amazonaws.com/call.mp3?signature=test"
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    url = await client.get_recording_download_url(call_id="CALL-123")

    assert captured == {
        "path": "/atoms/v1/conversation/CALL-123/recording/download-url",
        "authorization": "Bearer sk_test",
    }
    assert url.startswith("https://recordings.s3.amazonaws.com/")


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
async def test_provider_string_detail_is_preserved_for_safe_diagnostics():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Unsupported catalog route"})

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    with pytest.raises(SmallestAIError) as error:
        await client.list_voices()

    assert error.value.status_code == 422
    assert str(error.value) == "Unsupported catalog route"


@pytest.mark.asyncio
async def test_provider_error_list_is_preserved_for_safe_diagnostics():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"errors": ["Voice ID is not compatible", {"message": "Choose another voice"}]},
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    with pytest.raises(SmallestAIError) as error:
        await client.get_agent("agent_123")

    assert error.value.status_code == 400
    assert str(error.value) == "Voice ID is not compatible; Choose another voice"


@pytest.mark.asyncio
async def test_active_agent_knowledge_binding_is_read_from_provider_config():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": True, "data": {"globalKnowledgeBaseId": "kb_123"}},
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    assert await client.get_agent_knowledge_base_id("agent_123") == "kb_123"


@pytest.mark.asyncio
async def test_active_agent_knowledge_binding_is_read_from_enabled_search_tool():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "_resolvedConfig": {
                        "tools": [
                            {
                                "type": "knowledge_base_search",
                                "enabled": True,
                                "knowledgeBaseId": "kb_123",
                            }
                        ]
                    }
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    assert await client.get_agent_knowledge_base_id("agent_123") == "kb_123"


@pytest.mark.asyncio
async def test_disabled_knowledge_tool_overrides_stale_global_binding():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "globalKnowledgeBaseId": "kb_stale",
                    "tools": [
                        {
                            "type": "knowledge_base_search",
                            "enabled": True,
                            "knowledgeBaseId": "kb_stale",
                        }
                    ],
                    "_resolvedConfig": {
                        "tools": [
                            {
                                "type": "knowledge_base_search",
                                "enabled": False,
                                "knowledgeBaseId": "kb_stale",
                            }
                        ]
                    },
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    assert await client.get_agent_knowledge_base_id("agent_123") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_tools",
    [[], [{"type": "webhook", "enabled": True}]],
)
async def test_authoritative_runtime_tools_do_not_fall_back_to_stale_global_binding(
    runtime_tools,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "globalKnowledgeBaseId": "kb_stale",
                    "_resolvedConfig": {"tools": runtime_tools},
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    assert await client.get_agent_knowledge_base_id("agent_123") is None


@pytest.mark.asyncio
async def test_enabled_knowledge_tool_overrides_conflicting_legacy_binding():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "globalKnowledgeBaseId": "kb_stale",
                    "_resolvedConfig": {
                        "tools": [
                            {
                                "type": "knowledge_base_search",
                                "enabled": True,
                                "knowledgeBaseId": "kb_active",
                            }
                        ]
                    },
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    assert await client.get_agent_knowledge_base_id("agent_123") == "kb_active"


@pytest.mark.asyncio
async def test_multiple_enabled_knowledge_tools_are_rejected():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "_resolvedConfig": {
                        "tools": [
                            {
                                "type": "knowledge_base_search",
                                "enabled": True,
                                "knowledgeBaseId": "kb_one",
                            },
                            {
                                "type": "knowledge_base_search",
                                "enabled": True,
                                "knowledgeBaseId": "kb_two",
                            },
                        ]
                    }
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    with pytest.raises(SmallestAIError, match="multiple active knowledge-base bindings"):
        await client.get_agent_knowledge_base_id("agent_123")


@pytest.mark.asyncio
async def test_conflicting_legacy_knowledge_bindings_are_rejected():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "globalKnowledgeBaseId": "kb_one",
                    "_resolvedConfig": {"globalKnowledgeBaseId": "kb_two"},
                },
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    with pytest.raises(SmallestAIError, match="multiple active knowledge-base bindings"):
        await client.get_agent_knowledge_base_id("agent_123")


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
        language_switching_enabled=True,
        language_switching_mode="automatic",
        timezone="Asia/Dubai",
        voice_id="nyah",
        speech_rate=1.1,
        synthesizer_model="waves_lightning_v3_1",
        global_knowledge_base_id="kb_123",
    )

    assert captured["globalPrompt"] == "Be concise and helpful."
    assert captured["singlePromptConfig"] == {
        "prompt": "Be concise and helpful.",
        "tools": [
            {
                "type": "end_call",
                "name": "end_call",
                "description": "Terminate the call when conversation is complete.",
                "enabled": True,
            },
            {
                "type": "knowledge_base_search",
                "name": "knowledge_base_search",
                "description": (
                    "Search the approved knowledge base for verified information before answering."
                ),
                "enabled": True,
                "knowledgeBaseId": "kb_123",
                "fillerPhrases": ["Let me check that for you."],
            },
        ],
    }
    assert captured["slmModel"] == "electron"
    assert captured["language"] == {
        "default": "en",
        "supported": ["en", "hi"],
        "switching": {"isEnabled": True},
    }
    assert captured["synthesizer"]["voiceConfig"]["voiceId"] == "nyah"
    assert captured["synthesizer"]["voiceConfig"]["model"] == "waves_lightning_v3_1"
    assert captured["timezone"] == {
        "label": "(GMT+4:00) Asia/Dubai",
        "offset": 4.0,
    }
    assert captured["sessionTimeoutConfig"] == {"timeoutTimeInSecs": 600}
    assert captured["globalKnowledgeBaseId"] == "kb_123"


@pytest.mark.asyncio
async def test_versioned_draft_can_explicitly_clear_knowledge_binding():
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
        supported_languages=["en"],
        timezone="Asia/Dubai",
        global_knowledge_base_id=None,
    )

    assert "globalKnowledgeBaseId" in captured
    assert captured["globalKnowledgeBaseId"] is None
    assert captured["singlePromptConfig"] == {
        "prompt": "Be concise and helpful.",
        "tools": [
            {
                "type": "end_call",
                "name": "end_call",
                "description": "Terminate the call when conversation is complete.",
                "enabled": True,
            }
        ],
    }


@pytest.mark.asyncio
async def test_versioned_draft_omits_tools_when_knowledge_binding_is_unchanged():
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
        supported_languages=["en"],
        timezone="Asia/Dubai",
    )

    assert captured["singlePromptConfig"] == {"prompt": "Be concise and helpful."}
    assert "globalKnowledgeBaseId" not in captured


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
async def test_versioned_draft_serializes_fractional_timezone_offset():
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
        supported_languages=["en"],
        timezone="Asia/Kathmandu",
    )

    assert captured["timezone"] == {
        "label": "(GMT+5:45) Asia/Kathmandu",
        "offset": 5.75,
    }


@pytest.mark.asyncio
async def test_waves_catalog_and_voice_clones_use_current_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/get_voices"):
            return httpx.Response(200, json={"voices": [{"voiceId": "emily"}]})
        return httpx.Response(200, json={"data": [{"voiceId": "my_clone"}]})

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://atoms-gateway.example.com/v1",
        waves_base_url="https://api.smallest.ai/waves/v1",
        transport=httpx.MockTransport(handler),
    )

    voices = await client.list_voices()
    clones = await client.list_voice_clones()

    assert voices == [{"voiceId": "emily"}]
    assert clones == [{"voiceId": "my_clone"}]
    assert [request.url.path for request in requests] == [
        "/waves/v1/lightning-v3.1/get_voices",
        "/waves/v1/voice-cloning",
    ]
    assert {request.url.host for request in requests} == {"api.smallest.ai"}
    assert all("content-type" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_voice_preview_uses_unified_tts_with_fixed_bounded_wav_contract():
    captured: dict = {}
    wav = b"RIFF\x04\x00\x00\x00WAVE"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client = SmallestAIClient(
        api_key="sk_test",
        base_url="https://api.smallest.ai/atoms/v1",
        transport=httpx.MockTransport(handler),
    )

    audio = await client.synthesize_voice_preview(
        voice_id="rhea",
        model="lightning_v3.1_pro",
        language="hi",
    )

    assert audio == wav
    assert captured["path"] == "/waves/v1/tts"
    assert captured["headers"]["authorization"] == "Bearer sk_test"
    assert captured["headers"]["accept"] == "audio/wav"
    assert captured["body"] == {
        "text": VOICE_PREVIEW_TEXTS["hi"],
        "voice_id": "rhea",
        "model": "lightning_v3.1_pro",
        "language": "hi",
        "output_format": "wav",
    }


@pytest.mark.asyncio
async def test_voice_preview_rejects_non_audio_success_responses():
    client = SmallestAIClient(
        api_key="sk_test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"audio": "not-binary-audio"},
            )
        ),
    )

    with pytest.raises(SmallestAIError, match="unexpected voice preview response"):
        await client.synthesize_voice_preview(
            voice_id="jordan",
            model="lightning_v3.1",
            language="en",
        )


@pytest.mark.asyncio
async def test_voice_preview_rejects_oversized_response_before_buffering():
    client = SmallestAIClient(
        api_key="sk_test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/wav",
                    "Content-Length": "2000001",
                },
                content=b"",
            )
        ),
    )

    with pytest.raises(SmallestAIError, match="oversized voice preview"):
        await client.synthesize_voice_preview(
            voice_id="jordan",
            model="lightning_v3.1",
            language="en",
        )


@pytest.mark.asyncio
async def test_versioned_draft_publishes_tamil_with_automatic_language_switching():
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
        language="ta",
        supported_languages=["ta", "en"],
        language_switching_enabled=True,
        language_switching_mode="automatic",
        timezone="Asia/Kolkata",
    )

    assert captured["language"] == {
        "default": "ta",
        "supported": ["ta", "en"],
        "switching": {"isEnabled": True},
    }


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
async def test_open_branch_draft_returns_latest_and_treats_404_as_absent():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/agent_missing/" in request.url.path:
            return httpx.Response(404, json={"message": "No open draft"})
        return httpx.Response(
            200,
            json={
                "data": {
                    "latest": {
                        "_id": "draft_9",
                        "status": "draft",
                        "pendingPublish": {"state": "active"},
                    }
                }
            },
        )

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))

    draft = await client.get_open_branch_draft(
        agent_id="agent_123",
        branch_id="branch_123",
    )
    missing = await client.get_open_branch_draft(
        agent_id="agent_missing",
        branch_id="branch_123",
    )

    assert draft == {
        "_id": "draft_9",
        "status": "draft",
        "pendingPublish": {"state": "active"},
    }
    assert missing is None
    assert paths == [
        "/atoms/v1/agent/agent_123/branches/branch_123/draft",
        "/atoms/v1/agent/agent_missing/branches/branch_123/draft",
    ]


@pytest.mark.asyncio
async def test_mutating_timeout_is_marked_as_ambiguous():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = SmallestAIClient(api_key="sk_test", transport=httpx.MockTransport(handler))
    with pytest.raises(SmallestAIError) as error:
        await client.create_agent(name="Uncertain create")

    assert error.value.status_code == 504
    assert error.value.ambiguous is True
