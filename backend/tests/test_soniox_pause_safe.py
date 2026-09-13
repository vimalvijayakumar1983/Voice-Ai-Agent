"""Default-preserving native Soniox input configuration."""

from types import SimpleNamespace

import pytest

from app.livekit_runtime import soniox_pipeline as speech


def profile(flag=False, provider="soniox", runtime="pipeline"):
    return SimpleNamespace(
        primary_speech_provider=provider,
        stt_language="en",
        runtime_config={speech.PAUSE_SAFE_FLAG: flag, "voice_runtime": runtime},
    )


@pytest.mark.parametrize("flag", [False, None, "true", "false", 0, 1, {}, []])
def test_default_and_non_boolean_flags_preserve_baseline(flag):
    config = profile(flag)
    assert not speech.pause_safe_enabled(config)
    assert speech.input_sample_rate(config) == 16000
    assert speech.endpointing(config) == {"mode": "fixed", "min_delay": 0.3, "max_delay": 0.8}


@pytest.mark.parametrize("config", [None, [], "bad", 1])
def test_malformed_config_stays_off(config):
    value = profile()
    value.runtime_config = config
    assert not speech.pause_safe_enabled(value)


@pytest.mark.parametrize(
    "provider,runtime", [("inworld", "pipeline"), ("soniox", "inworld_realtime")]
)
def test_other_routes_are_not_opted_in(provider, runtime):
    assert not speech.pause_safe_enabled(profile(True, provider, runtime))


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,rate,delay", [(False, 16000, 0.3), (True, 48000, 0.8)])
async def test_configured_stt_and_metrics_match(enabled, rate, delay, monkeypatch):
    config = profile(enabled)
    model = SimpleNamespace(
        language="en", supported_languages=["en"], language_switching_enabled=False
    )
    captured = {}

    def stt(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(speech.soniox, "STT", stt)
    speech.build_stt(model, config, "private-test-key", ["approved-term"])
    params = captured["params"]
    diagnostic = speech.input_diagnostics(config)
    assert params.sample_rate == rate == diagnostic["stt_sample_rate_configured"]
    assert params.max_endpoint_delay_ms == 1000
    assert params.endpoint_sensitivity is None
    assert params.endpoint_latency_adjustment_level is None
    assert params.context.terms == ["approved-term"]
    assert params.language_hints == ["en"] and params.language_hints_strict is True
    assert speech.endpointing(config) == {"mode": "fixed", "min_delay": delay, "max_delay": 0.8}
    assert diagnostic["soniox_pause_safe_enabled"] is enabled
    assert "private-test-key" not in str(diagnostic)
    diagnostic["turn_endpointing_configured"]["min_delay"] = 0
    assert speech.endpointing(config)["min_delay"] == delay
