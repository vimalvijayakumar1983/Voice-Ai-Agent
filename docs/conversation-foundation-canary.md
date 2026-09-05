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
