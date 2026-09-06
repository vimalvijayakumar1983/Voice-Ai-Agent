"""Production defaults must not copy a QA company's data or enable phone calls."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent, AgentRuntimeProfile
from app.services.production_voice_preset import apply_conversation_baseline, new_inworld_profile


def test_preset_preserves_scope_and_does_not_invent_company_authority():
    agent = Agent(id=uuid4(), tenant_id=uuid4(), name="Unrelated company", system_prompt="Help")
    agent.agent_metadata = {"custom": "retained"}
    profile = new_inworld_profile(agent)
    assert agent.knowledge_company_scope is None
    assert agent.agent_metadata["custom"] == "retained"
    assert agent.agent_metadata["conversation_foundation_v1"] is True
    assert "conversation_intent_v1" not in agent.agent_metadata
    assert profile.enabled is False
    assert profile.status == "draft"
    assert profile.assigned_numbers == []
    assert profile.runtime_config["inworld_single_pass"] is True
    assert profile.runtime_config["provider_native_turns_qa"] is False
    assert profile.runtime_config["diagnostic_recording_mode"] == "off"
    assert profile.runtime_config["stt_model"] == "auto"
    assert profile.llm_model == "openai/gpt-4o-mini"
    apply_conversation_baseline(agent)
    assert agent.agent_metadata["custom"] == "retained"


@pytest.mark.asyncio
async def test_new_inworld_agent_gets_draft_production_profile(client, auth_headers, db):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "New independent business",
            "system_prompt": "Use only the attached approved business knowledge.",
            "voice_provider": "inworld",
        },
    )
    assert created.status_code == 201, created.text
    agent_id = UUID(created.json()["id"])
    profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent_id)
    )
    assert profile is not None
    assert profile.runtime_config["inworld_single_pass"] is True
    assert profile.status == "draft" and not profile.enabled
    agent = await db.get(Agent, agent_id)
    assert agent.agent_metadata["conversation_foundation_v1"] is True
    assert agent.knowledge_company_scope is None


@pytest.mark.asyncio
async def test_other_providers_do_not_get_inworld_profile(client, auth_headers, db):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Existing provider choice",
            "system_prompt": "A helpful agent.",
            "voice_provider": "sarvam",
        },
    )
    assert created.status_code == 201
    profile = await db.scalar(
        select(AgentRuntimeProfile).where(
            AgentRuntimeProfile.agent_id == UUID(created.json()["id"])
        )
    )
    assert profile is None
