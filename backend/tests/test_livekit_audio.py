"""Focused coverage for the production LiveKit audio-input boundary."""

from unittest.mock import Mock

from app.livekit_runtime import audio


def test_production_room_options_applies_nc_once_at_native_track_rate(monkeypatch):
    noise_filter = object()
    nc = Mock(return_value=noise_filter)
    monkeypatch.setattr(audio.noise_cancellation, "NC", nc)

    options = audio.production_room_options()

    nc.assert_called_once_with()
    assert options.audio_input.noise_cancellation is noise_filter
    assert options.audio_input.sample_rate == 48_000
    assert options.audio_input.auto_gain_control is True
    assert options.audio_input.pre_connect_audio is True
    assert options.audio_input.pre_connect_audio_timeout == 3.0
