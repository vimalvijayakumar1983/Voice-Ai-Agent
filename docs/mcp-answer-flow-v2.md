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
