"""LiveKit room audio configuration shared by browser and SIP sessions."""

from livekit.agents import room_io
from livekit.plugins import noise_cancellation


def production_room_options() -> room_io.RoomOptions:
    """Enable one agent-side noise filter while retaining LiveKit input defaults."""
    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            noise_cancellation=noise_cancellation.NC(),
        )
    )
