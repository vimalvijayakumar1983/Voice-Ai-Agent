"""Policy installation tests; provider replay must separately check compliance."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from livekit.agents import llm

from app.livekit_runtime import worker
from app.livekit_runtime.worker import VAVInworldRealtimeAgent


@pytest.mark.parametrize("provider", ["soniox", "inworld"])
@pytest.mark.parametrize("single_pass", [False, True])
def test_partial_evidence_and_callback_truthfulness_are_shared(provider, single_pass):
    model = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_metadata={},
        voice_provider=provider,
        system_prompt="Use approved knowledge.",
    )
    text = VAVInworldRealtimeAgent(model=model, single_pass=single_pass).instructions
    assert "not a complete roster" in text
    assert "provide the supported information first" in text
    assert "authorized action tool confirms that specific result" in text
    assert "transcript or proposed follow-up is not confirmation" in text
    assert "rules override conflicting authored" in text
    assert "Do not offer to capture or arrange a callback" in text
    assert "current\n  validity needs confirmation" in text


@pytest.mark.parametrize("other_tool", [False, True])
def test_session_capabilities_use_actual_tools_without_mutating_history(monkeypatch, other_tool):
    model = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_metadata={},
        voice_provider="soniox",
        system_prompt="Capture a callback on no match.",
    )
    agent = VAVInworldRealtimeAgent(model=model)
    context = llm.ChatContext.empty()
    context.add_message(role="user", content="Can you arrange a callback?")
    seen = []
    monkeypatch.setattr(
        worker.VAVInworldAgent,
        "llm_node",
        lambda self, chat_ctx, tools, settings: seen.append(chat_ctx),
    )
    tools = [agent.search_approved_knowledge]
    if other_tool:
        tools.append(lambda: None)
    agent.llm_node(context, tools, {})
    assert len(context.items) == 1
    assert (
        any(m.text_content == worker.NO_ACTION_TOOLS_POLICY for m in seen[0].messages())
        is not other_tool
    )
