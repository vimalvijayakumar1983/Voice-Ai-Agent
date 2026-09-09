# MCP financial speech and comparison repair — 9 September 2026

Scope: shared checked-MCP answer flow (`mcp_answer_flow_v2`), currently enabled on
the Trading ERP Assistant. No provider, model, reasoning, recording, telephony,
tenant grants or public-access settings change. Luna remains the configured model.

## Defects reproduced

- Literal full monetary figures bypassed the previous report formatter.
- The verifier received only the last user fragment, not the preceding request.
- Comparisons refused differing group membership (71 versus 72 salesperson rows
  in the inspected call, with 66 names common to both periods).
- Generic error replies incorrectly invited the caller to narrow clear questions.
- A live verifier returned a true boolean despite a rejection in its explanation.
- A candidate call selected a broader financial tool for a grouping already supported
  by the successful sales report tool, then stopped after that tool failed. Tool
  selection now prioritizes the narrow matching schema and one authorized alternative.

## Contracts

- Money formatting is presentation-only and occurs BEFORE semantic verification.
  Currency-labelled draft amounts must match typed source monetary values. Millions
  use two decimal places; thousands use up to two. Source units are explicit;
  an unscaled value of 450 stays 450. Original source/draft and spoken bindings are
  retained separately. Dates, quantities, IDs and names are not number-scrubbed.
- Exact requests and invoice/payment amounts are never summary-rounded. Unknown
  currencies, units and monetary schemas are not guessed. This change does not
  claim universal support for arbitrary report schemas.
- The verifier sees the latest utterance, bounded actual user/assistant dialogue,
  previous completed answer, and source-backed speech bindings. Rejected drafts
  and raw tool payloads are excluded from dialogue context. Conversation history
  is NOT evidence for business facts and cannot replace renewed authorization.
- Matching scope, currency, filters, basis and ordered periods are still required.
  Group intersection supports per-person changes. Unmatched groups retain known
  values and null missing values, deltas and percentages; no invented zeros.
  Totals explicitly describe returned groups. Matching lists alone do not prove
  full-company coverage. Nonpositive baselines have no fabricated percentage.
- Verification returns reason, violations and supported, in that order. Missing
  violations or any violation denies speech even if supported is true. The check
  remains probabilistic; this is not a guarantee of perfect semantic validation.
- Missing evidence, expired evidence, access failures and incompatible reports
  have safe distinct codes. Provider exception details are never exposed.
  Permission rechecks, expiry, interruption and native-audio gating stay enabled.

## Release gates

Run the financial speech, MCP answer, retrieval, forecast, delivery, private-MCP,
staff-browser and report-presentation tests. Replay the failed conversation with
real Luna verification, including an intentionally wrong entity/amount association.
Then run an isolated live tool/speech call and a deployed-worker browser smoke test.
Synthetic tests are not a human pronunciation/accent assessment or a latency claim.

Regression prompts: total -> previous month -> clarification -> salesperson
declines -> summarize -> exact amount -> goodbye. Include changed group membership,
missing/zero/negative baselines, other currencies, explicit units and revoked access.
Production readiness requires CI and deployed revision verification, not just tests.

## Validation before deployment

- Full local backend suite: 1,914 passed, 33 skipped (9 September 2026).
- Focused financial formatting and answer-flow suite: 68 passed.
- Historical-call evidence with actual Luna verification: 6/6 cases passed, including
  rejecting a deliberately incorrect Shops/total association. Salesperson comparison
  retained 66 matched and 11 unmatched names rather than inventing missing values.
- Candidate live calls are separate from normal deployed-worker verification. A first
  candidate call exposed the broader-tool selection failure above; do not count a
  completed call or finished audio as proof that its requested reports succeeded.
