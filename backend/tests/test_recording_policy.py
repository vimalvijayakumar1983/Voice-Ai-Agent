"""Fail-closed LiveKit recording intent tests."""

from types import SimpleNamespace

from app.services.recording_policy import (
    RECORDING_GOVERNANCE_BLOCKER,
    diagnostic_recording_mode,
    recording_runtime_metadata,
)


def test_recording_policy_defaults_off_and_never_infers_consent():
    assert diagnostic_recording_mode(None) == "off"
    assert recording_runtime_metadata(None, transport="livekit_sip") == {
        "recording_requested_mode": "off",
        "recording_effective_mode": "off",
        "recording_enabled": False,
        "recording_state": "off",
    }


def test_requested_sip_capture_is_truthfully_blocked_until_governance_exists():
    profile = SimpleNamespace(
        runtime_config={
            "diagnostic_recording_mode": "livekit_egress_explicit_consent",
        }
    )

    metadata = recording_runtime_metadata(profile, transport="livekit_sip")

    assert metadata == {
        "recording_requested_mode": "livekit_egress_explicit_consent",
        "recording_effective_mode": "off",
        "recording_enabled": False,
        "recording_state": "blocked",
        "recording_blocker": RECORDING_GOVERNANCE_BLOCKER,
        "recording_consent_observed": False,
        "recording_artifact_available": False,
    }


def test_requested_browser_capture_is_blocked_at_the_transport_boundary():
    profile = SimpleNamespace(
        runtime_config={
            "diagnostic_recording_mode": "livekit_egress_explicit_consent",
        }
    )

    metadata = recording_runtime_metadata(profile, transport="livekit_webrtc")

    assert metadata["recording_state"] == "blocked"
    assert metadata["recording_blocker"] == "livekit_sip_transport_required"
    assert metadata["recording_enabled"] is False
