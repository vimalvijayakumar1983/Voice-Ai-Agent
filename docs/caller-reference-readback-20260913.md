# Caller-reference readback QA

## Scope and design

Separate agent-metadata flag `caller_readback_v1`, disabled by default. The numeric
handler is enabled only for Soniox agents; it does not bypass the checked MCP lane.
No production settings or database schema change is required by this patch.

Two policy-only candidates were rejected: gpt-4o-mini still pronounced punctuation
as a decimal separator and guessed which repeated digit a correction referred to.
These failures are preserved in the parent workspace's
`outputs/caller-readback-model-initial-20260913.json` and
`outputs/caller-readback-model-policy-v2-20260913.json`.

The final candidate uses call-local typed numeric-reference state for a bounded
set of explicit capture, recall, correction and forget requests. It accepts all
digit groups in the completed caller utterance, preserves leading zeros, speaks
digits rather than financial amounts, and asks when a correction is ambiguous.
The original transcript is unchanged. It does not infer missing digits or turn
the caller's identifier into a verified business fact or authorization.

The handler replaces only the text-generation node for a recognized readback.
LiveKit continues to own normal utterance finalization, transcript history, audio
generation, turn timing and interruptions. No second model request is introduced.
Recognized numeric readbacks skip model generation; other turns use the existing
model with the readback policy appended. That policy adds input tokens, not a new
model round trip. Actual costs remain usage-dependent.

State belongs to one runtime agent instance; no shared cache or persistent-memory
store is added. Normal transcript/recording retention is not changed. Explicit
forget removes the active reference only, not an existing recording or transcript.

Mixed business/action requests, non-English requests and alphanumeric references
remain on the existing model/tool path. This is not a claim of universal reference
recognition. Ambiguous punctuation inside a numeric token is clarified instead
of being silently stripped. Unsupported new references invalidate the old numeric
state so subsequent readback cannot return the stale numeric value.

## Automated verification

255 tests passed across caller readback, Soniox, pause-safe settings, browser
admission, LiveKit provider/runtime and voice knowledge query contracts. Ruff
passed. Tests cover strict opt-in, full grouped digits, zeros, corrections,
ambiguity, retry idempotency, state isolation, raw transcript preservation,
secret/action exclusion and the checked-MCP boundary. They do not by themselves
prove recognition accuracy, voice quality or broad LLM instruction compliance.

## Voice comparison

Both arms used the same 48 kHz / 800 ms pause-safe settings, gpt-4o-mini, Soniox
Daniel voice, approved Royal Medical revision and synthetic PCM. Only the
readback flag changed. SDK caller through normal VAV browser admission and a real
LiveKit room; this is not human audio from a UAE browser or SIP call.

- Baseline: `d076a7e2-c753-58d0-b47e-3139a6b4a09d`.
- Candidate: `3da6c22d-bbf4-5d64-8bb4-df9cec23b3c2`.
- QA deployment: `043f5b09-6006-4e0b-8be6-5d62ccc57bea`.
- Verified release marker: `caller-readback-node-qa-20260913`.
- Full evidence: parent workspace `outputs/caller-readback-browser-ab-20260913.json`.

| Check | Baseline | Candidate |
| --- | --- | --- |
| Paused reference 429154 | Refused conversational recall | All six digits, with confirmation |
| Recall in same call | Included transcript punctuation | Exact digit-by-digit readback |
| Ambiguous replacement of repeated digit | Guessed first occurrence | Asked which occurrence |
| Replace with 00739 | Included punctuation between groups | Both zeros and all digits preserved |
| Recall corrected value | Spoke `dot, space` | Exact `zero zero seven three nine` |
| Previous-call reference | Did not claim access | Did not claim access |
| Reference as proof of payment | Did not confirm payment | Did not confirm payment |
| Doctor, department and goodbye | Answered | Answered |

For the five reference-related replies, measured caller-audio-end to first
received assistant audio was median **2,530 ms -> 1,799 ms** (about 29% lower).
Candidate range: 1,709–1,979 ms. This is a small, baseline-first synthetic sample,
not a production percentile/SLA. The worker recorded five deterministic readbacks
and no LLM first-token metric for those turns, while retaining EOU and TTS metrics.
No synthetic zero LLM usage was inserted. General knowledge questions still took
4,353–4,452 ms in this candidate replay; this fix does not solve their latency.

The comparison also recorded fewer total LLM tokens (28,255 -> 21,121) and spoken
characters (1,371 -> 1,046), but those are usage observations, not a normalized
cost guarantee. The appended policy can increase input tokens on remaining LLM
turns, while deterministic readback skips that model request entirely.

After the voice comparison, short `first`/`last` replies to the handler's own
clarification were added and verified in unit tests, including expiry on topic
change. Those follow-ups were not separately audio-replayed. The complete-value
replacement path above was audio-replayed. No one-off doctor/number replacement
is encoded in the product policy or parser.

## Cleanup, limitations and release

Both calls finished and emitted their results. The SDK replay client then aborted
in native teardown after cleanup; this is retained as a test-tool defect, not
hidden as a clean process exit. Independent database inspection confirmed both
calls completed, QA agent/profile disabled, and both candidate flags removed.
The isolated worker was stopped. Production remains at `d18a557`, unchanged.

No independent human listening assessment was performed: conclusions above use
the received audio timing and published speech transcripts. Recognition under
other accents, noise, long pauses and untested paraphrases remains to be validated.
This is a bounded opt-in fix, not a claim of 100% speech or conversational accuracy.
The branch includes the prior pause-safe candidate and is based on unmerged
doctor-retrieval PR #56. Keep it in review; do not enable globally or merge that
dependency implicitly as part of this test.
# Follow-up release gate (13 September, 10:17 UTC)

The broader synthetic call `282445ce-2b0e-5120-8632-8ee2049e067f`, on
`fe2ddae`, **failed acceptance**. The previous successful A/B is not sufficient
to enable the feature in production.

- STT committed `Please remember my reference number: 4291.` and `54.` as two
  adjacent user items 576 ms apart, before an assistant answer. The last-item
  handler lost the suffix. Neither the 800 ms endpoint nor policy alone
  guaranteed a complete reference.
- `No. Replace that reference with 00739.` fell through to the model because
  the correction parser accepted a comma after No but not a period. The model
  acknowledged 00739, but the next deterministic recall returned the old value.
- Doctor, department, payment-boundary and goodbye cases answered. Doctor and
  department first received audio were 5,229 and 3,475 ms respectively.
- The replay process exited normally (0) after explicitly unpublishing the
  caller track, closing readers/source/room and DB resources, and collecting
  callback cycles before event-loop shutdown. This fixes the observed test
  cleanup abort in this run; not a claim about every SDK teardown.
- QA agent/profile disabled and flags removed after the call. Production flags
  remain off.

Review fixes invalidate delegated natural corrections, expire bare correction
context on a topic change, and preserve payload-free confirmation requests.
The follow-up patch also accepts sentence punctuation after No and recovers
an adjacent strictly numeric suffix before any assistant message, bounded to
two seconds. It does not rewrite stored transcripts, wait on another model,
join arbitrary questions, or resolve general speech-finalization failures.
These changes require another voice replay before rollout.
