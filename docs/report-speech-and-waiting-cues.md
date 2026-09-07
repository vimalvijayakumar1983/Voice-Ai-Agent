# Financial speech and MCP waiting cues

## Scope

Shared LiveKit agent financial speech instructions apply to existing and future agents
without editing individual company prompts. Private MCP reports additionally receive an
analytical response contract: headline, supported highlights, interpretation, suggestions,
and contextual reformulation. Simple lookups remain short. This is instruction-based
behaviour in the existing model, not an extra verifier/model call or a guarantee of reasoning
accuracy. Existing access checks, source payloads and company boundaries are unchanged.

Summaries use approximate thousands/millions with sensible precision. Exact invoices,
payments, collections and explicit exact-value requests retain decimals. Source currency
and explicit unit multipliers are authoritative; 450 USD is not 450,000 USD. Source numeric
values are never rewritten. There is no new exact-value report panel in this change, and
the agent must not claim it has displayed/exported one. A governed structured report UI
is separate work, not an existing capability.

## Native waiting cues

LiveKit 1.6.10 `RunContext.with_filler` handles the 1.5-second continuous-idle dwell,
user speech, interruption and timer teardown. VAV supplies one neutral phrase per turn
(shared across all MCP tools), without customer names, figures or untrusted tool descriptions.
It starts only after authorisation and only while the upstream request remains pending.
No LLM pass is added; actual cue synthesis uses the session's existing TTS and is billable
like other generated speech. Fast requests do not synthesize a cue.

Scope exit explicitly interrupts any already-scheduled cue: native scope cleanup alone
only stops its timer. Cue speech is not added to model history and server speaking events
while it is active do not finish the user's response-latency trace. Scheduled-cue count is
not proof of audible playout. MCP traces separately include `remote_duration_ms` and total
adapter duration, contain no arguments/results, and do not replace end-to-end timings.

Static cues cover fixed English, Arabic, Hindi and Malayalam agents. Unknown languages and
language-switching agents stay silent rather than use the wrong language. This integration
covers MCP lookups only; other API adapters must explicitly reuse the helper. It does not
mask a slow model before tool selection or generate an endless sequence of reassurance.

## Verification and release

Offline tests exercise the actual installed LiveKit scheduler with simulated session/audio
handles, including fast/slow/error/cancelled requests, caller speech, parallel tools,
per-turn suppression, raw-tool context injection and financial policy retention.
Permission revocation and private content-free audit tests must continue passing.

Before production acceptance, run a fresh authorised Trading ERP browser call:

1. Ask August sales, channel breakdown, and 'explain that more professionally'.
2. Check natural units, source currency, grounded highlights and relevant suggestions.
3. Ask for an exact figure and confirm it against the authorised source report.
4. Test a slow lookup, interrupt the cue, then ask a different question.
5. Confirm fast lookups have no cue and latency measures the answer, not the cue.
6. Check a missing report, failed lookup and mixed-currency request without fabricated data.

These offline tests do not certify actual Inworld voice, model quality, browser playout,
or provider latency. No paid provider evaluation or stronger-model change is included.
