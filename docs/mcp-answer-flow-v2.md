# MCP answer flow v2 — opt-in candidate

Status: candidate, **not enabled for production agents**. Keep the flag off until
the live multi-turn canary, audio review, and PostgreSQL CI pass. No model upgrade
or production-agent configuration is included in this code change.

## What changes

An explicit boolean `runtime_config.mcp_answer_flow_v2: true` enables a call-local
evidence flow in the native Inworld tool-loop lane. The initial target is a
tools-only, authorized staff browser agent. Knowledge-only agents and the accepted
single-pass lane are not switched into this flow.

1. Existing approved read-only MCP tools collect evidence, without speaking each
   result or stopping the model before it has completed the user's request.
2. The model resolves the question from its conversation history and selects
   missing tools. Current/latest requests fetch fresh data; valid existing evidence
   can serve follow-ups without duplicate calls.
3. `vav_compare_reports` performs Decimal arithmetic for validated sales schemas,
   producing total and per-group changes. It rejects incompatible company scope,
   currencies, units/basis, filters, groups, or overlapping/reversed periods.
4. `vav_answer` validates a concise draft and its evidence IDs. Numeric checks are
   necessary but not sufficient; a bounded semantic verifier checks associations,
   relevance, causes and suggestions. It uses the configured Inworld LLM, not a
   silently substituted model. Social replies have a separate non-factual check.
5. Required tool choice and a native-audio output gate prevent direct generated
   audio from bypassing the checked-answer path. Only approved direct TTS plays.
   Delivered speech is added to LiveKit conversation context; unplayed drafts are
   not represented as completed answers in the answer-flow memory.

## Safety and bounds

- No new ERP permissions, tools, writes, phone calls, or cross-company grants.
- Authorization is repeated before lookup, evidence reuse, and delivery. Derived
  evidence retains parents' revocation and expiry checks.
- Evidence is call-local: 180-second TTL, 24 records, 24 KB per packet, 48 KB per
  verification, at most four cited IDs. Oversized evidence fails closed rather
  than being silently truncated. Unknown report formats remain bounded source
  text; the typed comparison adapter does not claim to cover every ERP schema.
- Six primary lookups per turn, existing 50-per-call ceiling, eight SDK tool
  steps, two answer attempts then one non-factual clarification. Optional forecast
  baseline reads remain governed by the existing call lookup ceiling.
- Interrupted/superseded turns cannot release late data. Evidence IDs and
  sequential markdown list markers are not spoken as business content.
- Original sources and calculations remain private transcript records. Rejected
  drafts are separate audit records, hidden from the conversation UI and excluded
  from generated summary text. Do not treat these audit drafts as spoken facts.
- Verifier calls have a 3-second HTTP timeout/3.5-second outer deadline. Usage is
  counted in the existing presentation-token counters. Semantic validation is
  probabilistic, **not a guarantee**; numeric checks and source isolation remain
  independent controls.

## Verification and rollout gate

Focused tests cover default-off flags, legacy delivery, private grants, expiry,
matching comparisons, stale-turn cancellation, wrong-number rejection, failed
audio, social replies, bounded correction, and native-audio suppression.

Isolated live tests use the real Trading agent's 20 approved remote tools plus two
internal tools, in separate QA call records and LiveKit rooms. They do not change
the production agent's settings. Text-driven tests prove tool routing and audible
delivery, not speech recognition, pronunciation, or microphone interruption quality.

The initial prompt-only attempt failed: the native model bypassed `vav_answer`.
The audio gate/required tool choice were added after observing this, not merely
assuming a prompt would enforce grounding. Subsequent tests caught list-number
false rejections, empty-evidence social checks, and internal ID narration.

Latest six-turn isolated call: `e8a0b97e-f83b-4e1b-bb32-01844697750e`.
August total, acceptance of a previous-month comparison, all six declining
channels, exact total, and goodbye were delivered with only two ERP lookups.
The recommendation turn was rejected for approximate percentages. Its exact
captured draft subsequently passed the corrected numeric guard and live GPT-4.1
mini verifier (2,344 ms, 1,011 input/91 output tokens). This was a text/evidence
replay, **not** a fresh six-turn audio pass or a production deployment.

The verifier output ceiling is 320 tokens, while the time deadline remains 3.5
seconds. An earlier 180-token ceiling sometimes cut off otherwise useful verdicts;
truncated verdicts still fail closed. Fresh whole-call audio and CI remain gates.

Captured-evidence model comparison (10 cases, one run each) scored GPT-4o mini
8/10 and GPT-4.1 mini 9/10 on semantic judgments alone. GPT-4.1 mini's remaining
wrong-total verdict was caught by the independent numeric guard. This small test
is not a production accuracy claim or authorization to upgrade all agents.

Before promotion: rerun the accepted comparison conversation on this exact build,
check all declining groups (not only two highlights), unknown causes versus
suggestions, exact amounts, goodbye, source-ID suppression, audio interruption,
current-month/forecast data and denied scope. Verify configured model cost and
served code revision. Deploy default-off first; enable only a reviewed canary
agent. Rollback is disabling the flag for new sessions, not removing safeguards
from an already active private call.

## Follow-up QA on September 8

Isolated call `bf2625e9-7912-4c32-9700-b6acff40b1be` completed all six text-driven
turns with audible checked delivery and no reported session errors. The same
GPT-4.1 mini QA override was used; production configuration was not changed.
The room was deleted at completion. The checks covered August sales, a contextual
July comparison, all six declining channels, unknown causes versus suggestions,
the exact August amount, and goodbye. No business lookup occurred for goodbye.

This is **not a release pass**. August was fetched twice (three remote lookups
instead of two). Answers were verbose and often narrated exact amounts rather
than compact summaries. The comparison described Shops and Bulk Cement growth
as approximately 10% each despite different exact percentages. A semantic pass
does not establish numeric association or presentation quality. One valid
'over 73%' draft was rejected by the conservative numeric gate and corrected to
exact figures. These are explicit remaining evaluation cases, not hidden passes.

Verifier duration was 1,130–1,915 ms per accepted answer; remote ERP lookups took
2,904–6,757 ms. The QA script's whole-turn seconds include full speech playback
and must not be reported as first-audio latency. No microphone ASR, pronunciation
review or real caller interruption was exercised by this text-driven test.

Review also fixed a draft-length branch that logged rejection without returning.
New regression tests reject empty, over-150-word and over-1,800-character drafts
before a paid verifier or speech. Call UI labels now distinguish original MCP
reports, derived comparisons and prepared answers. After those fixes, 267 focused
backend tests and 101 frontend tests passed locally. Full CI and production audio
canary remain separate gates; the feature remains default-off.

## Sol without reasoning QA on September 9

At the user's request, isolated call `629fbaec-0870-42a7-b8a3-32545666e6b9`
used `openai/gpt-5.6-sol` for the native tool loop and answer verifier with explicit
`none` reasoning. Production settings were not changed. The native session sends
`text_generation_config.reasoning = {"effort": "NONE", "exclude": true}`;
the Inworld chat-compatible verifier sends `reasoning_effort: "none"`.
Omitting this per-agent runtime setting preserves existing provider defaults.
Only explicit `none` is currently supported by this adapter. A separate synthetic
native probe observed the provider echo `NONE` and emit a function call, resolving
the tool-compatibility error seen with Sol/high. Serialization telemetry alone
does not prove a provider honoured a setting.

All six text-driven turns produced checked answers and completed audio delivery,
with no reported session errors or rejected drafts. The captured sources support
the reported total, month comparison and all six declining channels. The answer
distinguished unverified causes from suggested investigations, preserved the exact
total on request and responded to goodbye. Only two remote ERP reads occurred;
the follow-up questions reused call-local evidence. The QA room was deleted.

This is not a latency or presentation release pass. Remote reads took 2,425 and
3,182 ms; observed verifier durations ranged from 1,518 to 2,495 ms. First detected
audio was 11,179 ms on the initial text request including session startup; captured
later requests included 5,074, 8,847, 4,779 and 4,113 ms. These measure text submission
to any received non-silent audio, potentially a filler, NOT caller end-of-speech
to the first meaningful answer. No comparable previous-run first-audio benchmark
exists. Do not claim a latency improvement from this run.

Answers still narrate long exact figures even in summaries, and the recommendations
turn suggests investigation without performing a deeper causal drill-down. Audio
frames prove delivery, not good pronunciation. Human speech input, pronunciation,
interruption, denied access, current-month and forecast scenarios remain untested
in this run. The runtime and verifier focused suites passed locally (151 tests).
CI, controlled browser-call listening and production promotion are separate gates.

## Luna without reasoning comparison on September 9

Isolated call `afc7720b-2a3e-4dad-8f5c-d3e5baca6bcf` repeated the same six questions
with `openai/gpt-5.6-luna` and explicit `none` in both the native loop and verifier.
Voice, tools, instructions, answer checks and production settings were unchanged.
Inworld's live model catalogue listed Luna with function calling and EFFORT_NONE.
The call completed with six delivered answers, two ERP lookups, no session errors
and room cleanup. Independent Decimal sums of captured ERP rows match both monthly
totals. Channel amounts and final recommendations remain grounded in the comparison.

There were two semantic-check rejections and eight verifier requests, versus no
rejections and six requests for Sol. One rejected 'partly offset' despite this
being a supported arithmetic description of the channel changes. The other
rejected ranking Gypsum and Export as the largest declines without naming the
metric, although they are the top two absolute declines in the captured data.
The regenerated answers succeeded; do not count unnecessary retries as successful
error detection. This exposes verifier consistency, not ERP availability, as an
evaluation concern. Recommendations did not perform further investigative reads.

Text-request-to-first-detected-audio measurements were 4,831 / 3,817 / 5,324 /
11,969 / 3,703 / 3,521 ms in question order. On the directly comparable exact-total
and goodbye prompts, Sol measured 4,779 / 4,113 ms; its recommendations prompt
measured 8,847 ms versus Luna's 11,969 ms. These are single runs with different
generated answers, not an A/B latency distribution. Any-audio can include filler;
initial-turn timing includes startup. Neither end-of-speech latency nor audible
pronunciation quality was measured. Luna is compatible and worth further testing,
but this mixed result does not justify production promotion or a speed guarantee.
No application code change was needed beyond the previously committed explicit
reasoning support. The QA harness now retains per-turn observations in QA metadata.

## GPT-4o mini comparison on September 9

The user's 'gpt mini 4' request was interpreted explicitly as GPT-4o mini. Isolated
call `4d39e1ac-a05d-49ce-9066-588039973d75` used `openai/gpt-4o-mini` for both the
native answer loop and semantic verifier, omitting reasoning parameters. The same
six prompts, voice, tools, checks and bounds were retained. Production was unchanged;
the QA room was deleted and no session errors were reported.

Five turns delivered checked answers; recommendations instead delivered the bounded
failure clarification. The channel-list answer omitted FEPY despite evidence of its
decline, and the checker accepted the incomplete list. Other spoken totals, channel
amounts and percentages matched the captured comparison. The first total answer
became a long full-channel readout after the verifier unnecessarily rejected a
correct shorter summary. Both recommendation drafts were rejected with inconsistent
reasons, including treating an explicit lack of causal evidence as contradicting
the evidence's own lack of causal explanation. Eight verifier requests produced
three rejected drafts. This is not five fully correct answers out of six: one of
the accepted answers was incomplete.

There were four remote ERP reads: August, September-to-date, August again, and July.
Sol and Luna needed only August and July. GPT-4o mini repeated the August fetch
while the earlier evidence was still within the 180-second TTL. Remote lookup
times varied from 1,360 to 7,140 ms, so total timing differences cannot all be
attributed to model choice. First detected audio measured 16,401 / 4,439 / 4,649 /
9,871 / 3,154 / 5,283 ms. The 9,871 ms turn delivered a failure clarification, not
recommendations, and must not be presented as a faster successful recommendation.
Exact-total and goodbye comparisons were respectively Sol 4,779 / 4,113 ms,
Luna 3,703 / 3,521 ms, and GPT-4o mini 3,154 / 5,283 ms.

These are one text-driven run per model with any-audio timing, not a statistical
voice-latency benchmark. Both generation and verification models changed together;
the results do not isolate each role. GPT-4o mini's faster exact-total turn does
not outweigh the observed answer completeness and verifier failures for promoting
it to the private ERP workflow. Sol was the most consistent of these three runs;
presentation, human audio testing and broader regression gates remain outstanding.
