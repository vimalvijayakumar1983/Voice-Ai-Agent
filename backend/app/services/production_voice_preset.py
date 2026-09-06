"""Accepted browser voice baseline, without granting knowledge or phone access."""

from app.models.agent import Agent, AgentRuntimeProfile

PRODUCTION_VOICE_PRESET = "vav-grounded-20260906"


def apply_conversation_baseline(agent: Agent) -> None:
    """Install the accepted generic conversation features, not QA experiments."""
    agent.agent_metadata = {
        **(agent.agent_metadata or {}),
        "conversation_routing_v2": True,
        "knowledge_collections_v1": True,
        "conversation_state_v3": True,
        "conversation_foundation_v1": True,
        "production_voice_preset": PRODUCTION_VOICE_PRESET,
    }


def new_inworld_profile(agent: Agent) -> AgentRuntimeProfile:
    """New agents start in draft; normal credential/KB admission still applies."""
    apply_conversation_baseline(agent)
    return AgentRuntimeProfile(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        enabled=False,
        status="draft",
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
        stt_language="auto",
        assigned_numbers=[],
        runtime_config={
            "production_voice_preset": PRODUCTION_VOICE_PRESET,
            "voice_runtime": "inworld_realtime",
            "inworld_single_pass": True,
            "knowledge_repair_transport_enabled": True,
            "provider_native_turns_qa": False,
            "stt_model": "auto",
            "inworld_realtime_tts_model": "inworld-tts-1.5-max",
            "tts_delivery_mode": "balanced",
            "diagnostic_recording_mode": "off",
        },
    )
