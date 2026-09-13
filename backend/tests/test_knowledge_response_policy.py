"""Policy installation tests; provider replay must separately check compliance."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

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
