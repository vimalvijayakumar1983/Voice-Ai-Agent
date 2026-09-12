"""LiveKit room audio configuration shared by browser and SIP sessions."""

from livekit.agents import room_io
from livekit.plugins import noise_cancellation


def production_room_options(*, telephony: bool = False) -> room_io.RoomOptions:
    """Enable one agent-side background-voice filter at LiveKit's native track rate.

    BVC (background voice cancellation) removes other speakers and ambient
    noise before the audio reaches speech recognition, so a conversation in
    the room or a television does not become a caller turn. The plain NC
    model only attenuates stationary noise and let background speech through.
    Phone calls use the telephony-tuned model for narrowband audio.

    The bundled processors require their input and output rates to match.
    LiveKit decodes browser and SIP microphone tracks at 48 kHz, while
    ``AudioInputOptions`` otherwise asks ``AudioStream`` to resample to 24 kHz
    before the processor. Keep the filter at 48 kHz here; AgentSession still
    adapts the cleaned stream to the STT provider's declared rate.
    """
    noise_filter = noise_cancellation.BVCTelephony() if telephony else noise_cancellation.BVC()
    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            sample_rate=48_000,
            noise_cancellation=noise_filter,
        )
    )
