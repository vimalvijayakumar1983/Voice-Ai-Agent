# Latency: interruption recovery and stage visibility

## Scope

Preserve the existing voice models, endpointing and approved knowledge. Fix the
unnecessary recovery path exposed by the paired QA call; do not promise sub-600ms
latency from a retrieval-only change.

## Changes

- Normalize leading conversational request framing only in the routing copy.
  Keep branch constraints, company names, negation and the original transcript.
- A typed repeat (phone/address/hours/year) cannot replay an unrelated list.
  Reuse only same-company, matching-request evidence; retry the original lookup
  when needed, preserving branch qualifiers. No extra semantic repair pass.
- Record sequence-bound, content-free controller offsets from scheduling through
  retrieval, speech gate and reply dispatch. Record reply-request to server
  speaking separately. These are NOT browser playback or mouth-to-ear timings.
- Expose only allowlisted numeric timing fields in public call metadata.

## Verification

Regression fixtures cover compound and split stop requests, partial lists,
company changes, failed phone lookups, stale head-office numbers after failed
branch lookups, slow repeats, and preservation of meaningful qualifiers.
Telemetry tests cover stage ordering, sequence isolation, invalid values,
diagnostic callback failure and metadata privacy.

The existing nine-question production QA audio fixture is the paired integration
check. Model/endpointing changes remain deferred until these traces identify the
remaining delay. Production findings are appended after the replay.

## Production replay result

Runtime commit `942bbbb`, deployment `ee1151f1-2060-433c-95b7-b76b5273ac5e`
(SUCCESS). SHA-256 checks of the three changed runtime/routing files matched the
committed archive before calling. QA call `b04e00fa-dcfc-547c-bef1-d22569d1042a`
completed in 102 seconds with nine measured responses. Used the same cached PCM
fixtures as the two preceding comparisons, not newly synthesized questions.

The interrupted phone lookup now returns the approved company number; the slow
repeat speaks its digits, not the abandoned leadership list. The paused question,
company correction and goodbye also execute. An unsupported revenue question
still entered semantic recovery and timed out; the full call is NOT a quality pass.
Disposition correctly remains partially resolved / needs review.

| Server-side measurement | Before this fix | After |
| --- | ---: | ---: |
| Interrupted phone request, ms | 3108 (failed) | 950 (answered) |
| Phone controller retrieval, ms | 2231 | 20 |
| Median response, ms | 1015 | 971 |
| P90 / P95 response, ms | 3156 | 3244 |
| Greeting, ms | 494 | 518 |

Small paired sample, not an SLA or proof of a population-wide median improvement.
Tail latency has not improved in this replay.

The new stage traces identify three remaining delays:

1. First reply: 1731 ms from received speech-stop to final transcript; controller
   reply dispatched at 93 ms; reply-request to server speaking 1420 ms, of which
   the reported TTS first-byte metric accounts for 474 ms. The residual is inside
   or after reply dispatch, not the VAV retrieval/gate. Do not yet label it TTS
   generation: SDK scheduling, transport initialization and playout need isolation.
2. Leadership list: 1666 ms before final transcript, then 341 ms to speaking.
   Most other final-transcript waits were 452-605 ms. Pinpoint the STT/provider
   boundary before changing endpointing; retain the deliberate 700 ms pause test.
3. Unsupported revenue: 221 ms knowledge tool work, then a recovery timeout made
   total controller retrieval 2256 ms. This same recovery mechanism existed in
   both earlier calls (983 and 1793 ms total respectively), but this run timed out.

No new LLM stage, provider/model, endpointing or knowledge-content change was
introduced. Existing semantic recovery can still incur usage and timeouts. The
next latency experiment should address the measured cold first-reply path and
transcript-finalization delay separately; generic prefetch cannot fix these exact
lookups, which already complete in roughly 20-34 ms.

Validation on final code: 1513 passed, 31 skipped; Ruff check and formatting passed
for app/tests/migrations (244 files). Recording remains disabled; synthetic audio
receipt and transcripts were checked, not subjective human listening quality.
