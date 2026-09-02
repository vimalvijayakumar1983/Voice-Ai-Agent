"""LiveKit room audio configuration shared by browser and SIP sessions."""

from livekit.agents import room_io
from livekit.plugins import noise_cancellation


def production_room_options() -> room_io.RoomOptions:
    """Enable one agent-side noise filter at LiveKit's native track rate.

    The bundled NC processor requires its input and output rates to match. LiveKit
    decodes browser and SIP microphone tracks at 48 kHz, while ``AudioInputOptions``
    otherwise asks ``AudioStream`` to resample to 24 kHz before the processor. Keep
    NC at 48 kHz here; AgentSession still adapts the cleaned stream to the STT
    provider's declared rate.
    """
    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            sample_rate=48_000,
            noise_cancellation=noise_cancellation.NC(),
        )
    )
