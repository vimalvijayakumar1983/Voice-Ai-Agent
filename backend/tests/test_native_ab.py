from types import SimpleNamespace

import pytest

from app.livekit_runtime.native_ab import NATIVE_AB_FLAG, native_ab_enabled


def test_default_production_unchanged():
    assert not native_ab_enabled({}, {}, voice_runtime="inworld_realtime")


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_flag_requires_literal_boolean(value):
    assert not native_ab_enabled({NATIVE_AB_FLAG: value}, {}, voice_runtime="inworld_realtime")


def test_only_explicit_qa_native_lane():
    assert native_ab_enabled(
        {NATIVE_AB_FLAG: True, "inworld_single_pass": False},
        {"qa_ab_run": "native-ab-20260906", "qa_ab_lane": "B"},
        voice_runtime="inworld_realtime",
    )


@pytest.mark.parametrize(
    "metadata,runtime,single",
    [
        ({}, "inworld_realtime", False),
        ({"qa_ab_run": "native-ab-20260906", "qa_ab_lane": "A"}, "inworld_realtime", False),
        ({"qa_ab_run": "native-ab-20260906", "qa_ab_lane": "B"}, "pipeline", False),
        ({"qa_ab_run": "native-ab-20260906", "qa_ab_lane": "B"}, "inworld_realtime", True),
    ],
)
def test_rejects_other_agents_and_conflicting_modes(metadata, runtime, single):
    with pytest.raises(ValueError):
        native_ab_enabled(
            {NATIVE_AB_FLAG: True, "inworld_single_pass": single}, metadata, voice_runtime=runtime
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_native_hook_leaves_hold_to_provider_only_for_qa(native):
    from livekit.agents import llm

    from app.livekit_runtime.worker import VAVInworldRealtimeAgent

    agent = SimpleNamespace(_provider_native_turns_qa=native)
    message = SimpleNamespace(text_content="Stop.")
    if native:
        await VAVInworldRealtimeAgent.on_user_turn_completed(agent, None, message)
    else:
        with pytest.raises(llm.StopResponse):
            await VAVInworldRealtimeAgent.on_user_turn_completed(agent, None, message)
