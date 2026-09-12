"""Focused coverage for the production LiveKit audio-input boundary."""

from unittest.mock import Mock

import pytest

from app.livekit_runtime import audio


@pytest.mark.parametrize(
    "telephony,model",
    [(False, "BVC"), (True, "BVCTelephony")],
)
def test_production_room_options_applies_background_voice_cancellation_once(
    monkeypatch, telephony, model
):
    """Background speech in the room must not reach speech recognition.

    A test call on 12 September picked up other people talking nearby. The
    browser path uses the wideband background-voice model and phone calls the
    telephony-tuned one; the plain noise model is never used.
    """
    noise_filter = object()
    chosen = Mock(return_value=noise_filter)
    other = Mock(side_effect=AssertionError("wrong filter"))
    plain = Mock(side_effect=AssertionError("NC lets background voices through"))
    monkeypatch.setattr(audio.noise_cancellation, model, chosen)
    monkeypatch.setattr(
        audio.noise_cancellation, "BVCTelephony" if model == "BVC" else "BVC", other
    )
    monkeypatch.setattr(audio.noise_cancellation, "NC", plain)

    options = audio.production_room_options(telephony=telephony)

    chosen.assert_called_once_with()
    assert options.audio_input.noise_cancellation is noise_filter
    assert options.audio_input.sample_rate == 48_000
    assert options.audio_input.auto_gain_control is True
    assert options.audio_input.pre_connect_audio is True
    assert options.audio_input.pre_connect_audio_timeout == 3.0
