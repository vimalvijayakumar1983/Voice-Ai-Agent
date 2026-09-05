"""Fail-closed runtime metadata for the not-yet-enabled LiveKit recorder.

Saving an operator preference is not consent and is not evidence that Egress,
storage, retention, or playback exists. Calls persist the requested and
effective states separately so the UI and audits can explain why no recording
was created without ever implying that capture occurred.
"""

from __future__ import annotations

from typing import Any

DIAGNOSTIC_RECORDING_OFF = "off"
DIAGNOSTIC_RECORDING_LIVEKIT_EGRESS = "livekit_egress_explicit_consent"
RECORDING_GOVERNANCE_BLOCKER = "call_bound_consent_egress_storage_retention_not_configured"


def diagnostic_recording_mode(profile: Any | None) -> str:
    """Resolve only the two typed modes supported by the control plane."""

    runtime_config = (
        profile.runtime_config
        if profile is not None and isinstance(getattr(profile, "runtime_config", None), dict)
        else {}
    )
    mode = str(runtime_config.get("diagnostic_recording_mode") or "").strip().lower()
    if mode == DIAGNOSTIC_RECORDING_LIVEKIT_EGRESS:
        return mode
    return DIAGNOSTIC_RECORDING_OFF


def recording_runtime_metadata(profile: Any | None, *, transport: str) -> dict[str, Any]:
    """Return truthful per-call recording intent and effective state.

    This remains deliberately non-activating. A later Egress implementation
    must replace the blocker only after a call-bound affirmative grant and all
    storage/lifecycle controls succeed.
    """

    requested = diagnostic_recording_mode(profile)
    metadata: dict[str, Any] = {
        "recording_requested_mode": requested,
        "recording_effective_mode": DIAGNOSTIC_RECORDING_OFF,
        "recording_enabled": False,
    }
    if requested == DIAGNOSTIC_RECORDING_OFF:
        metadata["recording_state"] = "off"
        return metadata

    metadata.update(
        {
            "recording_state": "blocked",
            "recording_blocker": (
                "livekit_sip_transport_required"
                if transport != "livekit_sip"
                else RECORDING_GOVERNANCE_BLOCKER
            ),
            "recording_consent_observed": False,
            "recording_artifact_available": False,
        }
    )
    return metadata
