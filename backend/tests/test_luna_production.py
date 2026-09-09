"""Keep the promoted Luna route explicit, compatible and reversible."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.agent import Agent, AgentRuntimeProfile
from app.providers import inworld as inworld_module
from app.providers.inworld import InworldClient, InworldError
from app.schemas.runtime import RuntimeProfileUpdate
from tests.test_inworld_provider import _RealtimeProbeClientSession, _RealtimeProbeWebSocket


@pytest.mark.asyncio
async def test_browser_preflight_preserves_explicit_reasoning():
    from app.api.v1.endpoints.agents import _verify_native_browser_capability

    provider = SimpleNamespace(realtime_readiness_probe=AsyncMock())
    await _verify_native_browser_capability(
        inworld=provider,
        model_id="openai/gpt-5.6-luna",
        voice_id="Anjali",
        stt_model_id="assemblyai/u3-rt-pro",
        stt_language="en",
        reasoning_effort="none",
    )
    assert provider.realtime_readiness_probe.await_args.kwargs["reasoning_effort"] == "none"


def test_luna_is_limited_to_tested_native_tool_loop_route():
    route = dict(
        llm_provider="inworld", llm_model="openai/gpt-5.6-luna", voice_runtime="inworld_realtime"
    )
    assert RuntimeProfileUpdate(**route).llm_model == "openai/gpt-5.6-luna"
    for overrides in (
        {"voice_runtime": "pipeline"},
        {"knowledge_turn_mode": "single_pass_experimental"},
        {"llm_provider": "openai"},
    ):
        with pytest.raises(ValueError):
            RuntimeProfileUpdate(**{**route, **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_luna_readiness_requires_reasoning_acknowledgement(monkeypatch, acknowledged):
    effective = {"audio": {"output": {"model": "inworld-tts-2"}}}
    if acknowledged:
        effective["text_generation_config"] = {"reasoning": {"effort": "NONE"}}
    websocket = _RealtimeProbeWebSocket(
        [
            {"type": "session.created"},
            {"type": "session.updated", "session": effective},
            {
                "type": "response.function_call_arguments.done",
                "name": "vav_readiness_check",
                "arguments": "{}",
            },
        ]
    )
    monkeypatch.setattr(
        inworld_module.aiohttp, "ClientSession", lambda: _RealtimeProbeClientSession(websocket, {})
    )
    probe = InworldClient(api_key="test-inworld-key").realtime_readiness_probe(
        model_id="openai/gpt-5.6-luna",
        voice_id="Anjali",
        stt_model_id="assemblyai/u3-rt-pro",
        stt_language="en",
        reasoning_effort="none",
    )
    if acknowledged:
        await probe
    else:
        with pytest.raises(InworldError, match="did not acknowledge reasoning disabled"):
            await probe
    assert websocket.sent[0]["session"]["text_generation_config"] == {
        "reasoning": {"effort": "NONE", "exclude": True}
    }


@pytest.mark.asyncio
async def test_saving_luna_pins_reasoning_and_switching_back_removes_it(
    client, auth_headers, tenant, db, monkeypatch
):
    from app.api.v1.endpoints import runtime as endpoint

    monkeypatch.setattr(endpoint, "runtime_readiness", AsyncMock(return_value=([], {})))
    agent = Agent(
        tenant_id=tenant.id,
        name="Luna route test",
        system_prompt="Read approved data.",
        voice_provider="inworld",
        voice_id="inworld:Anjali",
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id, agent_id=agent.id, runtime_config={"mcp_answer_flow_v2": True}
    )
    db.add(profile)
    await db.commit()
    payload = dict(
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-5.6-luna",
        voice_runtime="inworld_realtime",
    )
    response = await client.put(
        f"/api/v1/runtime/agents/{agent.id}", json=payload, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    await db.refresh(profile)
    assert profile.runtime_config["llm_reasoning_effort"] == "none"
    assert profile.runtime_config["mcp_answer_flow_v2"] is True
    response = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        json={**payload, "llm_model": "openai/gpt-4o-mini"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    await db.refresh(profile)
    assert "llm_reasoning_effort" not in profile.runtime_config
