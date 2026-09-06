# Native Inworld versus current VAV — controlled QA comparison

## Scope

Authorized on 6 September 2026. Original production and QA agent settings are unchanged.
The shared worker was deployed with a default-off, explicitly QA-gated branch;
this was not a deployment-free experiment.
Two browser-only QA copies share the same tenant, approved knowledge binding,
source prompt, recognition policy, voice (Anjali), rate (1.0), LLM
(openai/gpt-4o-mini), and English configuration. No phone assignments, transfers,
booking tools or external messaging tools are attached to either copy.

- A: VAV Latency QA A - Current — ae2a1477-709d-41f7-a150-f952a657d1e5
- B: VAV Latency QA B - Native Inworld — 5730e252-002b-42be-ad13-554358c788e4
- Source QA: 0d7747ef-6d2a-448a-b7d0-4f9335ea178f
- Knowledge: 51bdebb8-b8a2-52bf-905e-2e4c95d78966
- Expected serving revision: 8c734ec3-e4ca-5ff9-b5c1-ec7c59713b39

All copies were created with provider voice validation, explicit system audit
events, and normal VAV readiness, capacity and knowledge admission for calls.
No end-user identity was fabricated. Synthetic caller audio only.

## Important preflight findings

The existing tool_loop mode was not a fully provider-native reference: it still
invoked VAV's force-cancel callback on caller speech and custom transcript
handling. Commit 57052ef adds a QA-only switch that leaves those decisions to
Inworld/LiveKit. The switch rejects non-B agents, other experiments, pipeline
mode and simultaneous single-pass mode. Absent the flag, behaviour is unchanged.
Knowledge authorization, retrieval tools, grounding instructions and telemetry
remain enabled. It does not implement a new turn detector.

The first three smoke calls predate that isolation and explicit TTS alignment:

| Call | Lane | Observation |
| --- | --- | --- |
| 54c89a8a-5f78-5aaf-ad73-4ce8362237cc | B, pre-isolation | Said Hello but no completed answer to Can you hear me; replay timed out |
| e779274a-a1c4-5a2d-b423-668df8838c54 | A | Irrelevant clarification for Can you hear me; retrieved and answered chairman |
| 7ba46a3f-6492-5490-81df-ca79086cd539 | B, pre-isolation | Knowledge tool worked; chairman answer used same approved evidence ID as A |

These are diagnostic failures, not final benchmark results. Native speech
defaulted to TTS 1.5 Max while deterministic speech used TTS-2. The main QA pair
explicitly sets realtime output to TTS-2 to remove that model confound. A generic
tts_model label in call metadata is not sufficient proof of the wire model;
use realtime_tts_session_update_serialized_model as well.

## Main comparison protocol

Twelve question turns, three rounds per version, grouped into six calls to retain
follow-up context (72 turns, not 72 fresh sessions). Alternate A/B order across
rounds. The earlier suggestion of one fresh session per question would not test
conversation memory and company switching adequately.

Scenarios: basic audio check, chairman, founding date, leadership list, interruption
to request phone number, paused person/company question, corrected company/role
follow-up, cosmetic-centre switch, specialized-centre correction, unsupported
revenue claim, Monday-to-Tuesday correction without booking, and goodbye.

Both versions replay identical cached PCM; each result carries its fixture hash.
No per-case prompt patching or knowledge edits during comparison. Any changes
require a new labelled run, not pooling results across configurations.

## Measurement limitations

The automated client is a Python LiveKit subscriber in the API environment,
not an instrumented browser in the user's UAE location. It measures receipt of
voiced audio after the final voiced fixture frame using an RMS threshold of 200.
This includes transport to that subscriber but not browser audio rendering,
device output or the user's WAN. It is not mouth-to-ear latency.

Server-side latency is reported separately. Its speech-end timestamps may occur
at different points in provider versus manual modes; do not use server P50 alone
to declare a winner. Greeting/filler audio is not proof of a useful answer.
Overlapping interruption turns need separate inspection, not naive inclusion in
the first-audio percentile. Missing replies count as failures, not zero latency.

Paid synthesis, transcription, model and infrastructure usage may occur. Record
reported units separately from inferred costs; do not equate missing units with
zero cost. Final browser listening and a wider multilingual test remain separate
acceptance gates.

## Additional interpretation limits discovered during execution

- Use chairman, established and leadership for the clean first-audio comparison.
  Leadership is deliberately interrupted after audio begins: its latency is
  measurable but completeness is not scored. Nine observations per lane are a
  small diagnostic sample, not a production SLA.
- Do not pool overlapping audio, acknowledgements, or fragmented questions into
  useful-answer latency. Useful-answer audio onset after a filler was not measured.
- On native company-correction turns, the replay completion observer could accept
  an early fragment (for example, "I apologize") and advance before the subsequent
  response. Those results are inconclusive, not proof of a failed retrieval.
- Native barge-in telemetry remains sticky after the first interruption because
  the QA callback skips the custom consumer that clears it. Do not use that flag
  to count real interruptions or score cancellation success in this cohort.
- A native server P50 below 600 ms does not establish sub-600-ms complete-question
  to useful-answer performance: its event clock and inclusion of short responses
  differ from the matched client measurement.

## Implementation verification

Runtime commit: `57052efec60448d32ff9462f6bbbd59178bb8abd`.
Worker deployment: `c88e80ce-d0e7-46f9-88bf-099b69e49ad2`, successful.
Full backend suite after the metadata compatibility correction: 1,540 passed,
31 skipped. Targeted browser/provider/native tests: 157 passed. Ruff and diff
whitespace checks passed. No production agent was switched to the native lane.

Reproduction scripts are in `docs/experiments/`: `setup_native_ab.py`,
`configure_native_ab.py`, `replay_native_ab.py`, and `audit_native_ab.py`.
Setup/configuration and replay are explicit mutations/paid test operations;
the audit script is read-only. These are scoped experiment scripts, not a
general-purpose customer QA harness.

## Matched factual first-audio results

Milliseconds from last voiced input fixture frame to first received voiced output
frame, measured on the same test client. Percentiles use linear interpolation.

| Metric | A: current VAV | B: native Inworld |
| --- | ---: | ---: |
| Samples (three factual scenarios, three rounds) | 9 | 9 |
| P50 | 2,536 | 3,997 |
| P90 | 2,595 | 4,206 |
| P95 | 2,612 | 4,241 |
| Chairman question median | 2,586 | 4,188 |
| Established question median | 1,498 | 3,030 |
| Leadership response onset median | 2,536 | 3,997 |

This run does not demonstrate sub-600-ms factual answers for either lane.
The result compares these VAV integrations, not the maximum possible performance
of either vendor. It is not evidence that Inworld TTS alone caused the delay.
The source prompt is shared, but native tool-loop versus single-pass orchestration
deliberately changes the effective response path.

## Decision and bounded next step

### Completed cohort and quality observations

All six calls closed with completed status, and all 72 scenario observations were
collected. Completed status is not a quality pass. One native audio-check scenario
timed out after only "Hello"; the remaining scenarios continued under the same
protocol. All six durable call records confirm the same serving revision and
serialized STT `assemblyai/u3-rt-pro`, language `en-GB`, and realtime TTS-2.

| Scenario | A (three rounds) | B (three rounds) |
| --- | --- | --- |
| Can you hear me? | 0/3 relevant answers; asked for clarification | 2/3 relevant answers; third only Hello and observation timeout |
| Chairman / established | Approved facts answered in all rounds | Approved facts answered in all rounds |
| Interrupt to ask group phone | Correct number 3/3 | Refused/missed number 3/3 |
| Paused Trading affiliation | Declined unsupported affiliation 3/3 | Declined unsupported affiliation 3/3 |
| Corrected Group role | President identified 3/3 | President identified 2/3; refusal in first round |
| Cosmetic centre phone | Correct number 3/3 | Missed/refused number 3/3 |
| Specialized centre correction | Correct number 3/3 | Inconclusive due replay observer limitation |
| Unsupported billion-dirham revenue | Not verified, appropriately 3/3 | Not verified, appropriately 3/3 |
| Monday corrected to Tuesday | 0/3; asked caller to finish an already complete question | Recognized Tuesday, but added confirmation/scheduling prompts; not a clean acceptance pass |
| Goodbye | Appropriate reply 3/3 | Appropriate reply 3/3 |

Leadership was intentionally cut short to test interruption, not full list recall.
Phone/role/founding answers were checked against the pinned approved source,
not independently certified as current real-world corporate information.
No actual appointment, PSTN call, or external message was made.

| Lane / round | Call ID | Duration (seconds) |
| --- | --- | ---: |
| A / 1 | dabbe2f7-9b4d-5944-9cff-2152db3503af | 152 |
| B / 1 | 8e638916-798c-5aee-98e6-f2a50c55c35f | 194 |
| B / 2 | 88495dbe-0178-5ef0-a8ec-6f1308eb3862 | 204 |
| A / 2 | 1023a520-6e0a-5beb-90ad-597db6f577bf | 151 |
| A / 3 | 4fb0f357-e0ef-5c83-8d6a-fcebcc9cb8ae | 152 |
| B / 3 | d83535ac-6b34-549e-ae6c-2a34821ddd97 | 202 |

Full synthetic observations and scoped runtime audits are preserved locally in
`../../native-ab-20260906-evidence.json` (relative to this report), outside the
repository. Reproduction scripts
were formatted after the run; no behavior/configuration change was made during
the six-call cohort. Costs are not presented as reconciled invoices: for example,
A reports zero main-session LLM tokens but has separately recorded knowledge
interpretation tokens. Zero main-session tokens does not mean a free call.

### Recommendation

Do not replace the production path with B based on this experiment. Retain A as
the baseline, not as a fully accepted product. Native mode is still an isolated
QA experiment. Do not add another STT/TTS provider based on this result.

The next diagnostic should inspect B's missing/unsuccessful knowledge-tool calls
for the same approved phone facts that A retrieves, without weakening grounding
or changing the company data. Separately, inspect A's routing of conversational
and date-correction turns into knowledge fallback. These are distinct quality
issues, not evidence of slow PDF indexing.

Before another latency experiment, fix the replay observer's premature completion
and native observer-only barge-in reset, then use a new cohort label. Keep the
current 72-turn cohort immutable. Evaluate endpointing, tool planning, retrieval,
model response and audio delivery on correlated turns; do not infer a TTS tail
from a single server timestamp. Browser playback and UAE-network measurement
remain required before making a customer-facing latency claim.
