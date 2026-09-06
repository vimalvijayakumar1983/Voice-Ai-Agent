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

## Initial production integration checks

QA agent only, same nine synthetic questions and identical cached PCM hashes.
Ten response samples per call because the goodbye fixture finalized in two parts.
The baseline was rerun because the previous API deployment had cleared its
ephemeral synthetic-fixture cache; do not claim byte identity with older reports.

| Call | First reply request to server audio ms | First response total ms | Greeting ms | P50 / P95 ms |
| --- | ---: | ---: | ---: | --- |
| Baseline 7481b9c2-c8d0-5a9a-a4f4-0ffe6a8d6154 | 729 | 2504 | 450 | 870 / 2504 |
| Initial ac53c417-ee08-536a-914e-f73330818957 | 332 | 2178 | 610 | 968 / 2973 |
| Repeat bde408df-e05a-5310-8a0e-fa781a1f0f6d | 351 | 2225 | 632 | 869 / 2225 |

Both candidates confirmed transport preconnect completed with zero text sent.
They retained chairman/year answers, interrupted phone lookup, slow digits,
paused question handling, company correction, unsupported-revenue refusal and
goodbye. Both retained the same seven answered / one unresolved ledger counters
as the baseline; this patch does not claim all conversation-quality issues solved.
No live customer calls, PSTN dialing, bookings or production recording were used.

The transport boundary improved consistently, but the overall P50/P95 did not
show a consistent improvement. Final-transcript waits and semantic recovery
still dominate slow turns. The initial candidates also had slower greetings;
the final scheduling adjustment starts preconnect only after the first greeting
audio frame, avoiding startup competition. That adjustment is checked separately
below. Do not silently drop the initial results or treat this sample as an SLA.

## Final production validation

Runtime `01f36cb`; deployment `17cfc7fd-f408-4e3e-970f-052b49db3e3d` SUCCESS.
Worker and preconnect module SHA-256 matched the committed archive before replay.
Final call `0a548e98-db4c-57f9-9aeb-597d735e63a4`, completed, 100 seconds,
ten response samples. Same cached PCM hashes as the baseline and initial trials.

| Server-side metric | Fresh baseline | Final |
| --- | ---: | ---: |
| First reply request to server audio ms | 729 | 349 |
| First response total ms | 2504 | 2261 |
| Greeting ms | 450 | 580 |
| P50 ms | 870 | 959 |
| P90 ms | 2012 | 2134 |
| P95 ms | 2504 | 2261 |

The target transport interval improved by 52%, while median and greeting latency
did not improve in this sample. These are server observation points, not a
browser mouth-to-ear measurement. Sub-600ms overall latency has NOT been achieved.
The first response still includes 1685 ms before final transcription and 224 ms
of controller retrieval/preparation, followed by 349 ms to server audio. The
leadership final transcript waited 1666 ms. Unsupported revenue was safely refused
but still used 1193 ms of retrieval/recovery. No recovery timeout occurred in this
final replay; the timeout mechanism itself was not changed by this patch.

Preconnect completed in 511 ms during greeting/first-question time and sent zero
text. All tested answer, correction, paused-name, interrupted-number, slow-repeat
and farewell behaviors were preserved. Request counters remain seven answered /
one unresolved, matching baseline; no claim that the entire product is error-free.

Final-code validation: 1520 passed, 31 skipped. Ruff check and formatting passed
for app/tests/migrations, 246 files. Only synthetic audio was received; recordings
remain disabled, so subjective listening quality and production SIP were not
audited. Existing knowledge, voice/model/rate, endpointing, and repair rules remain
unchanged. Next work is the provider finalization delay and the meaning-preserving
recovery path, not more broad exact-fact retrieval optimization.
