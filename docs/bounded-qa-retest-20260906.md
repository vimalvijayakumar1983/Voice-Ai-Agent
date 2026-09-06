# Bounded QA fix and retest — 6 September 2026

## Scope

Keep the existing speech providers/models and QA agent settings. Do not switch
production agents to the native lane. Apply and test four narrowly scoped changes:

1. Optional synthetic caller-tail correlation in replay: a response to an earlier
   fragment cannot complete the whole question. Unrecognized tails fail observation,
   rather than being assigned zero latency. This is a test-only boundary, not a
   production speech detector.
2. Clear the consumed native QA barge-in telemetry latch after final transcription.
3. Route bounded audio checks and caller-memory questions outside business lookup.
   Recall is explicitly attributed to the caller, not verified company data or a
   confirmed booking. Mixed business/action questions do not match this bypass.
4. Native QA knowledge-tool calls use the same authorised company/reference router
   as current VAV, instead of directly querying under a potentially stale company.
   Native turn-taking and response generation remain with the provider.

No new LLM/STT/TTS provider, knowledge-source rewrite, phone assignment, booking,
customer call or external message is part of this test. Generic regression fixtures
use Harbour Group and Sun & Moon centres, not production company-specific facts.

## Validation protocol

Run all backend tests, then one focused browser-admitted synthetic call per existing
QA lane. The `--focused-v2` replay uses nine unchanged cached fixtures: audio check,
chairman, established, leadership, interruption requesting phone, company switch,
company correction, date correction, goodbye. Preserve the earlier cohort; never
pool its timings with this changed implementation/observer.

The Python subscriber still does not measure UAE browser speaker playback. First
voiced audio is not automatically a meaningful answer. `useful_answer_audio_ms`
remains null until a defensible audio/content alignment is available. Completion
of an observed response is not a semantic pass. Review the actual transcript.

The caller-memory bypass covers bounded explicit recollection and call-check
phrasing, not every possible conversational intent or language. Broader acceptance
requires paraphrase/multilingual and real microphone coverage.

## Verification and deployment

- Full backend suite for `163bb0d`: 1,558 passed, 31 skipped (190.38 seconds).
- Final native-ledger observer guard `5e21464`: all 18 targeted tests passed;
  the native resolution ledger is explicitly unavailable because no native
  spoken-completion callback settles the single-pass request ledger.
- Ruff checks and formatting passed.
- Worker deployment `2b2f685d-cf4f-40fc-ab86-f5a229e4a7a3` succeeded for
  `5e21464e3e9423fdec7d33ef5be4b6599fdc9a20` before either retest call started.
- The API service was not redeployed. The replay runner loads the new observer
  class into its own ephemeral QA process; it does not alter the running API
  server or its files. Browser session admission remains the normal API function.

## Live results

Both focused calls and one three-turn startup recheck completed and closed.
There were 21 scenario observations in total. No call or appointment was made to
a real customer. The deployment/settings stayed fixed throughout these calls.

| Call | Lane | Duration |
| --- | --- | ---: |
| f05d8d0c-f338-571d-a19a-f9b78ae2b63f | A, bounded-v2 | 132 s |
| 6dd4fbf7-615c-5cca-9c4a-054091822aae | B, bounded-v2 | 150 s |
| a3937fe8-9395-5843-8fd5-7c1739303f33 | A, startup recheck | 41 s |

- A audio check: first observation was invalid because synthetic speech overlapped
  the startup greeting and no caller transcript arrived. The harness now waits
  for the greeting transcript and listening state. The separately labelled recheck
  answered correctly: "Yes, I can hear you. Please go ahead."
- A still answered the chairman, founding year and all three requested business
  numbers correctly against the pinned approved knowledge.
- A date correction now produced Tuesday afternoon instead of the erroneous
  "finish your question" response. However, both checks added an unnecessary
  follow-up/capture offer. This is only a partial pass.
- B answered the cosmetic-centre and specialized-centre numbers correctly, with
  verified evidence for each. The corrected observer waited past the early
  "I apologize" fragment and captured the full specialized-centre answer.
- B still refused the group phone number after interruption. Its five recorded
  knowledge-tool calls cover chairman, founding year, leadership and both medical
  centres; there is no recorded tool invocation for the refused group phone turn.
  This remaining failure precedes retrieval, rather than proving absent KB data.
- B recalled Tuesday, but also offered to capture a message/contact details.
- All calls produced appropriate goodbye responses.
- Native barge-in flags now clear on subsequent non-interruption turns. Native
  request-resolution ledger availability is false, rather than exporting incomplete
  pending-request counts as accurate resolution metrics.

### Latency (diagnostic only)

First voiced audio received by the same Python client; one observation per cell.
No improvement claim or production percentile can be supported by this small run.

| Clean factual question | A | B |
| --- | ---: | ---: |
| Chairman | 2,976 ms | 4,246 ms |
| Established | 1,610 ms | 3,066 ms |
| Leadership response onset (then deliberately interrupted) | 2,637 ms | 4,149 ms |

B's date-correction audio-onset metric was 22 ms because earlier fragment audio
overlapped the later question. It is NOT a 22-ms meaningful answer. That scenario
is excluded from latency comparison and useful-answer onset remains unavailable.

Native successful tool timings were 38–57 ms in this run; the missed group-phone
answer cannot be fixed by making those lookups faster. A's factual traces include
roughly 1.67–1.79 s between server speech-end and final transcription on several
turns. Timing boundaries differ between lanes, so those server intervals must not
be subtracted from client latency as a precise cross-provider decomposition.

## Remaining decision

Do not switch production to B. Do not describe this as full acceptance or a
latency fix. The reusable scoped retrieval path and test observer improved, but
native tool invocation and unnecessary follow-up behaviour remain unresolved.

A read-only inspection found conflicting capability wording in the shared QA
agent prompt: it limits available company information to the prompt/call variables,
while runtime policy authorises knowledge-tool evidence. It also instructs contact
collection for follow-up and confirmation of dates/numbers. These are candidate
contributors, not a proven explanation of every model response. No agent prompt
was silently rewritten during this experiment.

The next bounded change should reconcile those capability/style instructions
with the actual runtime tools, test post-interruption tool invocation, and ensure
caller-memory replies do not start follow-up collection. Keep lookup authorisation
and business-fact grounding intact. Only after that should provider endpointing
and response-stage latency be tuned against the same quality checks.

Full synthetic transcripts/metrics are retained outside the repository in
`../../bounded-qa-v2-evidence.json` (relative to this report).
