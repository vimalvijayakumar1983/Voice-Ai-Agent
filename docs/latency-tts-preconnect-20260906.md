# First-answer transport latency

## Evidence and implementation

Installed LiveKit Inworld plugin 1.6.10: `TTS.prewarm()` allocates its pool but
does not open a socket. Streaming TTS obtains a connection/context before its
first text send; the SDK TTFB clock starts at that send. Connection setup is
therefore absent from that metric. A cached PCM greeting does not open this
streaming connection either.

Use the public `TTS.stream()` lifecycle to open and close a context with no text.
This uses the SDK's existing connection pooling, not a replacement transport or
private pool access. After durable admission, the task starts when the first
greeting audio frame has been forwarded, overlapping greeting playout and the
first question. It never blocks session startup or a caller reply, has no retries,
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

## Endpointing experiments: not deployed

An isolated provider-only probe used the same cached caller PCM, same STT model,
language and bounded recognition terminology. No caller recording, manual audio
commit or automatic model response. Requested settings were verified against the
provider's session.updated echo. This is NOT a LiveKit/SIP end-to-end benchmark.

The clock below starts at the end of the sent synthetic PCM fixture (including
its trailing silence), not at a human-measured mouth-to-ear boundary.

| Mode | Chairman final after fixture ms | Leadership question |
| --- | ---: | --- |
| Semantic medium | 1736 | Complete, 1820 ms |
| Semantic high | 892 | Split into three separate final utterances |
| Server VAD 350 ms | 1639 | Complete, 1724 ms |
| Server VAD 500 ms | 1695 | Complete, 1756 ms |

High eagerness split the identical leadership audio into “Can you give me the
list?”, “leadership team?” and “Al Zaabi Group?” in two independent probes.
It is rejected as a production default: speed alone is not a quality pass.
Both server-VAD settings preserved the phrase but did not demonstrate the desired
latency improvement. The deliberate 700 ms pause produced two transcripts in all
modes, so VAV's fragment assembly remains necessary.

Provider speech_stopped and final-transcript events were 0-1 ms apart in these
probes; most delay occurred before that provider event. Do not equate that event
with the earlier LiveKit user-state transition used by the call dashboard.
Production endpointing remains unchanged. Lower-level provider tuning or a
separately evaluated recognition path is needed; blindly raising eagerness is
not a safe fix. Reference: https://dev.docs.inworld.ai/realtime/usage/using-realtime-models
