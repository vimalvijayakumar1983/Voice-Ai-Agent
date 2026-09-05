# Natural list requests and pending-response recovery

## Scope

Enabled only by `conversation_foundation_v1`. The legacy collection parser and
main receptionist configuration remain unchanged. The shared implementation uses
the agent's authorized company scope and pinned knowledge revision; no business
names, roles or answers are hardcoded into routing.

## Contracts

- Natural request prefixes (`I need`, `I want`, `I would like to see`) are request
  framing, not filters. `in <selected company>` identifies ownership.
- Locations, dates, demographics, exclusions and requested extra attributes remain
  constraints. Unsupported filters must not silently produce an unfiltered list.
- A validated unfiltered list is remembered as a canonical collection request.
  Company-only corrections retain the collection category, not the old company's facts.
- `Okay, give me` accepts an unfinished information request. It retains genuine
  filters and unresolved company/person choices. After interrupted list speech it
  resumes from confirmed playback progress. After explicit stop it does not revive
  the cancelled request. It cannot authorize an appointment action.
- A standalone past-tense appointment statement receives a targeted clarification,
  not knowledge search or a claim that booking succeeded.
- The QA U3 transcription prompt includes provider-recommended language guidance
  for a supported explicit language. No recognizer, TTS model, on-wire language,
  auto-language policy or unexpected-script safeguard is changed.
- U3's full transcription prompt (language guidance, agent scope and whole
  vocabulary terms) is capped at 1,750 characters. Budgeting vocabulary alone
  is insufficient and can cause a provider `stt_unavailable` error.

## Speech evidence and limits

The live read-only readiness probe confirmed an Inworld session echo of
`assemblyai/u3-rt-pro` and `en-GB`. This does not prove what the downstream
recognizer honours or establish accuracy for human speech. The production call's
serialization-only acknowledgement field is not a record of this separate probe.

AssemblyAI documents native U3 code switching and recommends a `Transcribe
<language>` prompt for guidance; it is not a guaranteed hard language lock:
[U3 streaming migration guide](https://www.assemblyai.com/docs/streaming/migration-guides/universal-to-u3-pro-streaming).

## Verification

Tests use unrelated fixture companies and cover request variations, meaningful
filters, interrupted output, explicit stop, unresolved company choices, appointment
clarification, legacy behavior, and serialized QA language guidance. A candidate
process replay passed 11/11 expectations against the actual QA Al Zaabi revision
`8c734ec3-e4ca-5ff9-b5c1-ec7c59713b39`. The deliberately unsupported Dubai filter
remained unresolved rather than being counted as a successful answer.

Production browser-audio verification and human accented-speech testing are
separate gates; text tests alone must not be called a complete voice-quality pass.
