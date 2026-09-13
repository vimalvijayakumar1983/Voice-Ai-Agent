# Soniox pause-safe candidate — 13 September 2026

## Scope

This is an **off-by-default canary**, not a global production change. It does not
include the rejected audio turn-detector experiment. Based on PR #56 so baseline
and candidate calls use the same approved doctor-directory retrieval behavior.

`runtime_config.soniox_pause_safe_v1: true` selects 48 kHz STT input plus native
LiveKit fixed endpointing at 0.8 seconds. The accepted default stays at 16 kHz,
0.3/0.8 seconds. Native Soniox max endpoint delay remains 1,000 ms; sensitivity
and latency-adjustment parameters are unchanged. Language hints, terminology,
LLM, TTS, interruption controls, knowledge, and preemptive generation are unchanged.
No additional inference model or transcript rewriting is introduced.

Only the explicit boolean `true` enables the candidate, and only for a Soniox
pipeline profile. Strings, numbers, malformed settings and other provider/runtime
profiles retain baseline behavior. Per-call diagnostics record the effective mode,
sample rate, and configured delays. Disabling the flag restores the old route for
subsequent sessions; do not change an active session's sample rate.

## Separated experiments

First, 48 SDK-only observations compared 16/48 kHz input and native STT/audio turn
detection independently, using identical generated PCM within the experiment.
The 48 kHz-only arm was faster on some questions but committed parts of corrections
and numbers prematurely. The audio detector prevented those early commits in that
batch but sometimes waited about 2.5 seconds. Neither was an accepted solution.

Second, 36 observations compared native 48 kHz STT with three pause budgets on
identical PCM (six cases, two orders per configuration). This was roomless
AgentSession + real Soniox + Silero, stopped before retrieval/LLM. Synthetic input
was resampled to 48 kHz. It does not include WebRTC, room noise cancellation, or a
human microphone and must not be described as production response latency.

| Native pause budget | Split utterances / 12 | Premature-commit cases / 12 | Final commit after audio end |
| --- | ---: | ---: | --- |
| 0.3/0.8 seconds | 7 | 5 | 395–881 ms; splits are not valid fast answers |
| 0.8/0.8 seconds | 0 | 0 | 810–862 ms |
| 1.0/1.0 seconds | 0 | 0 | 1,006–1,059 ms |

The six cases covered a clear question, short pause, paused doctor name, month
correction with VAT qualifier, six-digit invoice number, and a replacement request.
Manual transcript inspection found the expected words preserved in this batch;
abbreviations/punctuation and grouped digits were normalized. Keyword presence
alone was not the pass criterion. These are synthetic inputs, not proof for every
speaker, accent, language, longer pause, background voice, or telephony codec.

Raw evidence is retained in the parent workspace:

- `outputs/soniox-separated-factorial-20260913.json`
- `outputs/soniox-pause-budget-20260913.json`

## Automated checks

72 targeted tests passed, including default behavior, strict opt-in, provider
isolation, actual worker session options, effective STT configuration and cleanup
of failed browser starts. Ruff check/format passed for the changed Python files.
Existing unrelated `test_agent_catalog.py` changes are excluded.

## Browser-call gate

**Timing improved; full quality gate did not pass. Do not enable globally.**

The same worker image served both arms through normal VAV browser admission and
a real LiveKit room, with normal 48 kHz/BVC room input. An SDK caller published
identical cached synthetic clips. This is not a human browser test from the UAE.
Baseline ran first, candidate second; model variability and cache warming can
affect the comparison. Both used the same approved Royal Medical revision,
gpt-4o-mini, Soniox STT/TTS and Daniel voice. No model or knowledge changes.

- Baseline call: `e3e9a877-e901-501b-a87b-dec52cfd5c9f`
- Candidate call: `c0b9d891-892b-5213-b0a9-95e5f3d81e88`
- Isolated deployment: `57fb5388-9794-41dc-9a45-1a738cc04127`
- Verified release marker: `soniox-pause-safe-800-qa-20260913`
- Evidence: parent workspace `outputs/soniox-pause-safe-browser-20260913.json`

| Stored runtime metric | Baseline | Candidate |
| --- | ---: | ---: |
| Reported response P50 | 4,080 ms | 3,304 ms |
| Reported response P90/P95 | 5,904 ms | 4,203 ms |
| Latency samples | 6 | 8 |
| Call opening to greeting | 1,190 ms | 1,135 ms |

Ordinary candidate turns reported end-of-utterance at 800–801 ms; the existing
barge-in override still used 1,350 ms. Transcription delays on the candidate were
328–613 ms. These stage numbers are not full answer latency. Unequal turn/sample
counts, missing/zero fields, and synthetic fixtures prevent a production SLA claim.

The candidate answered the department, paused doctor name, correction to urology,
replacement phone request and goodbye. The additional false doctor name from the
earlier experiment did not recur; that does not establish its cause or guarantee
ASR accuracy. No assistant audio before the caller finished was observed on the
ordinary questions in either arm. Both overlap tests confirmed the agent was
speaking when interrupted. Do not compare the harness's barge-in first-audio value
as exact replacement-answer latency: residual earlier audio can contaminate it.

**Remaining blocker:** the complete spoken reference was `429154`. Baseline
treated `4291` and `54` separately, then repeated only `4291`. Candidate received
all groups but refused to remember/repeat the reference as personal information.
Neither passed the number-readback requirement. A speech timing fix does not by
itself fix that response behavior, and a generic recall refusal is not a pass.
Repeated evaluation of complete identifier readback/confirmation is required;
do not insert a one-off rule for this particular number or doctor.

## Cleanup and rollout state

Both calls completed. The QA clone/profile were disabled and the test flag removed.
The isolated worker is REMOVED and automatic QA builds disabled. Production worker
remains on `d18a55749a569782062f3d15d357ad2a3405bba8`. No production agent, knowledge
base, provider credential, phone route or global setting was changed. Keep the
change off by default and in draft review until the readback quality gate passes.

## Provider references

- [LiveKit turn detection](https://docs.livekit.io/agents/logic/turns/turn-detector/)
- [Soniox endpoint detection](https://soniox.com/docs/stt/rt/endpoint-detection)
