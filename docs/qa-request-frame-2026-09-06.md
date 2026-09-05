# QA request-frame correction

Scope: `conversation_foundation_v1` opt-in, using the existing provider and pinned
knowledge revision. No main-agent flag, knowledge content, model, credential, or
booking integration changes.

## Corrected contracts

- Routing-copy contraction normalization leaves stored transcription untouched.
- Approved directory identities resolve role and open affiliation requests without
  treating a shared surname as an ambiguous company. Explicit company constraints
  still win. Caller claims remain questions to verify, not evidence.
- A request for a role excluding a phone number is not a negated company selection.
  Salary, dates, positive contact requests and other extra constraints are not
  discarded by the bounded slot fast path.
- Multiple requested companies are stored separately from alternative company
  choices. A short detail clarification retains all targets. An implied “numbers”
  uses a phone slot only when the previous requested detail was phone.
- Interpretation failure preserves the accepted person/company/detail and keeps
  the unparsed request for recovery. Existing attempt limits are unchanged.
- Imperative appointment requests use the current runtime's truthful capability
  response, not knowledge repair. This runtime does not register a booking tool.
- A late partial-output callback cannot reopen an already completed ledger item.

## Verification

`tests/test_conversation_foundation.py` contains a sequential counterpart of the
failed human call using unrelated fixture companies and people, with semantic
routing enabled and optional interpretation budget exhausted. It also covers
forced interpretation timeout, multi-company detail clarification, straight and
curly apostrophes, action commands, retained constraints, and late callbacks.

A read-only candidate-process replay against the QA Al Zaabi serving revision
`8c734ec3-e4ca-5ff9-b5c1-ec7c59713b39` passed 17/17 text expectations before
deployment. This is retrieval/response verification, not an audio-quality claim.

Do not promote to the main receptionist based on text tests alone. Verify the
deployed commit and run the same sequence through a real QA LiveKit room. Review
recognized text, company selection, complete answers, interruptions, end-of-call
response, and latency separately. A synthetic caller cannot establish robustness
to every accent or background speaker.
