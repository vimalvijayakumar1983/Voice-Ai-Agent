# First-answer transport latency

## Evidence and implementation

Installed LiveKit Inworld plugin 1.6.10: `TTS.prewarm()` allocates its pool but
does not open a socket. Streaming TTS obtains a connection/context before its
first text send; the SDK TTFB clock starts at that send. Connection setup is
therefore absent from that metric. A cached PCM greeting does not open this
streaming connection either.

Use the public `TTS.stream()` lifecycle to open and close a context with no text.
This uses the SDK's existing connection pooling, not a replacement transport or
private pool access. The task starts after durable admission, alongside greeting
preparation. It never blocks session startup or a caller reply, has no retries,
has a three-second deadline, closes its stream and is cancelled at call shutdown.
Errors are content-free diagnostics and do not change the normal response path.

Applies to single-pass Inworld sessions; a server-controlled runtime profile can
opt out with `tts_transport_preconnect_enabled: false`. No model, voice, speech
rate, endpointing, answer text, knowledge content or semantic recovery change.
No dummy text or audio generation is sent by preconnect. Provider billing is
still reconciled externally; this does not claim that the entire call is free.

Provider references:
- https://docs.inworld.ai/api-reference/ttsAPI/texttospeech/synthesize-speech-websocket
- https://docs.livekit.io/reference/python/livekit/plugins/inworld/index.html

## Bounded transport probe

Three paired fresh-engine tests in the production API environment, using the QA
voice and six short synthetic outputs. Cold and warm use the same model/rate.
No caller recording or credentials are emitted by the probe.

| Pair | Cold first frame ms | Prepared first frame ms | Empty-context setup ms |
| --- | ---: | ---: | ---: |
| 1 | 606 | 307 | 1191 |
| 2 | 1276 | 330 | 491 |
| 3 | 1262 | 315 | 1184 |

These isolate TTS transport and are not end-to-end call latency. Each prepared
first-frame value equalled its reported TTFB; the cold values included up to
948 ms outside the reported TTFB. The empty context produced no audio in all
three probes. Full-call before/after results must be evaluated separately.
