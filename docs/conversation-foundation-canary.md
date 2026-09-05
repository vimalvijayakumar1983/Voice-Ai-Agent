# Conversation foundation canary

`agent_metadata.conversation_foundation_v1: true` is an explicit, per-agent
opt-in on the existing `conversation_state_v3` / Inworld single-pass lane.
It does not enable `conversation_intent_v1`, change providers, change knowledge
revisions, or grant action tools. Keep the main receptionist unchanged until
production audio acceptance passes.

## Contract

- Preserve the provider transcript. Only the retrieval input can assemble
  clearly incomplete final fragments. Complete questions have no extra delay.
- Wait 1.4 seconds for a continuation; preserve the buffer when speech has
  resumed. Bound the buffer to 800 characters / 8 seconds, discard it on a new
  question or explicit cancellation, and never retrieve an expired fragment.
- Resolve a person pronoun from the one approved person actually spoken about,
  not from a caller assertion or an arbitrary entry in a leadership list.
- Separate the company selection from the requested detail. Accept explicit
  positive corrections and retrieve a claimed role to check its actual holder.
- Repeat committed speech only. Retrieve each requested company's contact fact
  inside the agent's existing tenant / binding / revision fence; do not mix
  caches or silently omit missing companies.
- Current executable capability is information lookup, not appointment booking.
  Business appointment policies remain knowledge queries.
- A content-free request ledger records unfinished requests independently of
  truncated turn traces. A clarification resolves only when its continuation
  is answered. Courtesy and a new topic cannot erase the unfinished request.
  Unresolved requests cap a resolved disposition at partially resolved / review.

## Verification

### Natural correction recovery (September 5 follow-up)

- The scoped directory derives unique descriptive company suffixes at call
  startup. Shared suffixes require a choice; no new company authority is added.
- Possessives and centre/center spelling are normalized only in search input.
  Explicit negative company corrections are distinct from excluded detail slots.
- An unbound person-role pronoun asks whose role; it must not retrieve an
  arbitrary executive profile. Uncertain contextual turns use the existing
  validated intent interpreter and shared eight-request repair budget, without
  a second interpretation pass after retrieval.
- A provisional interim transcript does not cancel a still-pending lookup.
  A final replacement does. If VAD ends without transcription, after the bounded
  grace period the agent requests a repeat and keeps the original request
  unresolved. Continuous detected speech remains bounded to 30 seconds; no
  stale answer is spoken over it. Recovery counters are exposed in metadata.
- Multi-company contact speech uses human-readable phone/address labels.

The expanded regressions must include the failed correction as transcribed
(`I am in ...`), company-only follow-ups, possessive shortened clinic names,
negative corrections with fillers, ambiguous clinic suffixes, an unbound role
pronoun, and provisional speech without a final transcript. Synthetic audio
still does not establish robustness to every human accent or background speaker.

`backend/tests/test_conversation_foundation.py` exercises generic fictional
companies, constraints, false claims, correction/repeat/plural requests,
fragment continuation, cancellation, normal-turn latency, and disposition.

Production acceptance must repeat the context/correction and robustness audio
fixtures on **VAV Production Voice Quality QA**, pinned to its existing real
knowledge revision. Record exact call IDs, transcripts, fragment counters,
latency, and disposition. Passing text tests alone does not establish audio
quality or speech-recognition accuracy. Roll back the agent flag on failure.

## Deliberate limits

The bounded fragment grammar is not a replacement ASR engine. It does not
retranscribe audio. Multi-company exact contact answers are limited to four
companies/facts. Ambiguous or unsupported questions still need clarification or
an honest knowledge-gap response. No claim of universal scenario coverage is made.
