# Step 2: bounded unmatched-question repair

This supplements explicit company scope (Step 1). It does not replace the
knowledge compiler, grant access to subsidiary facts, or change the voice stack.

## Retrieval correctness

Structured fact ranking uses its subject, predicate, value and compiled search
phrases. A shared evidence quotation cannot make an unrelated fact satisfy the
requested topic. Excerpts preserve whole fact blocks; detached quotations are
not ranked as independent facts. This correction applies to structured retrieval
regardless of the optional AI setting.

## Optional AI retry

`knowledge_company_scope.semantic_retrieval_enabled` defaults to false. In the
Inworld single-pass lane with explicit company scope, a normal unmatched search
may request one GPT-4o mini question reformulation using the configured tenant
OpenAI key (existing platform-key fallback only when no tenant configuration
exists). Clear exact/general matches, company selection, social turns and repeat
memory do not need this extra request.

The response is a strict search-or-clarify schema, not an answer. A search must
run through the existing approved, tenant/company/revision-bound retrieval again.
The model cannot switch active company. Stale company epochs are rejected. Prices,
availability, eligibility, cancellation, role and numeric constraints have an
additional deterministic loss check. This is not a proof of semantic equivalence;
source coverage and QA still matter. An ambiguous plan asks for clarification.
An unavailable or timed-out planner asks for a brief rephrase rather than claiming
the source lacks the information. A second genuine retrieval miss remains a miss.

To resolve synonyms, the planner receives at most 40 catalogue terms / 1,400
characters from the same approved, bound exact-fact index and company. Terms are
predicates and service labels, not raw documents or other companies' facts. The
catalogue is partial, so it is never proof of absence. Vague leadership wording
cannot silently become a specific chairman/president question.

There is no recursive model retry. The model deadline is two seconds, SDK retries
are disabled, input is capped at 800 characters, and output at 120 tokens. Each
call caches up to 16 plans keyed by company, question and same-company previous
committed answer. Cached plans still retrieve evidence anew. The deadline bounds
the model wait, not the whole audio turn (credentials, DB, generation and playback
are separate). No promise of unchanged latency is made on a repair turn.

## Metering

Runtime records the model, attempted request count, provider-reported input/output
tokens, last status/time and per-turn interpretation time/status. Missing usage,
including interrupted/time-out requests, stays unknown and marks reconciliation
incomplete. The cost report adds separate OpenAI interpretation components using
its existing public rate book; these are estimates, not the Inworld invoice or a
claim of zero cost. Voice answer model usage remains separately accounted for.

## Rollout and acceptance

Ship disabled, then validate against the real QA-bound published revision. Enable
only on the QA agent after positive and negative retrieval checks. Re-run phone,
repeat, company correction, leadership, healthcare paraphrase, unsupported-price,
ambiguous-role, thank-you and goodbye tests in a real browser call. Unit tests do
not establish audible quality or end-to-end latency. Broader rollout is an
explicit decision, not automatic. Disable the checkbox to roll back AI repair;
Step 1 company scoping and committed speech memory remain in effect.
