# MCP faithful source delivery

Supersedes the default rounding and automatic report-analysis behaviour in PR 28.

## Contract

- The LLM selects a permitted read-only tool and arguments. Existing company/user
  permissions, pre/post-call checks, lookup limits and private audit remain in force.
- The successful MCP text response goes directly to the existing external TTS engine.
  No LLM summariser, arithmetic, rounding, translation or name replacement runs on it.
- Multiple text blocks are separated by blank lines. If text and structured results
  coexist, text is the spoken representation; they are not merged or double-counted.
- Structured-only results are rendered as labelled JSON with unchanged parsed values.
  This is intentionally not a generated prose report. Upstream should provide a clear
  text response when natural report narration is required.
- LiveKit StopResponse suppresses automatic model continuation after direct delivery.
  Source audio is interruptible and is not inserted into the model chat history.
- Repeats/challenges must request the scoped report again; instructions prohibit
  inventing figures or certifying a result merely because a tool succeeded.
- No unsolicited analysis. An explicit request can be routed to an approved upstream
  analysis/report tool; this implementation does not add a local analysis model.

## Display and privacy

The original returned text is recorded as a `source` entry in the call transcript,
with tool name, timestamp, hash and delivery state. Calls displays it literally as
"Original MCP response", not HTML/Markdown execution. It is distinct from the spoken
transcript: a long report may have been interrupted before its end. Existing
`require_call_access` protections apply. Private source payloads are not exposed in
call-list metadata or metrics. There is no new recording, export or third-party
destination, and the private-session AI summary remains disabled.

## Limits and verification

This prevents VAV's post-tool LLM rewrite from changing source figures. It does not
certify the ERP's data, ensure the routing LLM always chooses correct tool arguments,
or guarantee pronunciation by the TTS provider. It does not gate every generative
utterance in the realtime session. Validate actual browser playback before rollout.

Offline tests cover literal source delivery, names/units/negative/zero/null values,
duplicate text/structured output, no model continuation using the SDK output contract,
cancellation, concurrent responses, missing TTS, and revoked authorisation. Fixtures
are synthetic, and no production financial payload is sent to a provider by tests.

The source delivery boundary checks SpeechHandle.exception() and independently
observes the configured external TTS stream through public SDK APIs. LiveKit 1.6
can log a say() task failure without surfacing it on the handle; therefore finished
also requires generated frames and normal stream completion. No private SDK task
internals are inspected. Empty output and partial stream failures cannot count as
finished, while caller interruption remains a distinct state.
Failed speech is labelled failed and does not increment the
delivery count; a generic retry message is attempted without releasing provider
error text. Interruption is rechecked after asynchronous authorisation so a stale
report cannot start after the caller has moved to a new turn. Regression tests
reproduce both failures and pass with these checks in place.

Production canary: request a scoped report, compare source text against upstream,
listen to exact figures, challenge/repeat without changing period, interrupt while
reading, ask a different period, then finish with thanks/goodbye. Do not mark a call
passed solely from unit tests or successful HTTP/tool statuses.
