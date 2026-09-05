# Knowledge serving revisions

VAV publishes knowledge with a blue/green release boundary. Mutable source rows
are the next draft; calls read only the immutable `KnowledgeServingRevision`
referenced by `KnowledgeBase.serving_revision_id`.

## Publication contract

1. Every draft source must contain searchable compiled text and pass the normal
   Knowledge Studio approval checks.
2. Approval creates or reuses an immutable speech lexicon.
3. VAV snapshots the approved compiled source text and structured facts into an
   immutable serving revision. The release manifest separately hashes sources,
   retrieval chunks, structured facts, entities and the lexicon.
4. In the same database transaction, VAV moves the serving and lexicon pointers
   to the new release. A failed transaction leaves the prior release active.
5. Later source or metadata edits return the mutable draft to review but retain
   the active serving pointer. Existing agents therefore continue to use the
   last approved release until the replacement is approved.
6. Explicitly removing approval is a revocation: under the knowledge-base lock
   it increments `serving_revocation_generation` and clears both publication
   pointers. Historical immutable rows remain for audit while the knowledge
   base exists.

Explicit knowledge-base deletion is the separate, irreversible data-erasure
operation promised by Knowledge Studio. It is blocked while any nonterminal
call is pinned to the knowledge base; once every pinned call is terminal, the
mutable draft, immutable releases, and lexicons are purged together. Do not use
DELETE as release cleanup or rollback.

Every caller-facing runtime must stamp `knowledge_serving_revision_id`,
`knowledge_serving_knowledge_base_id`, `knowledge_serving_content_sha256`,
`knowledge_source_revision_sha256`, and
`knowledge_serving_revocation_generation` in the call runtime metadata.
Exact-fact caches use the aggregate immutable release hash, so compiler-only
fact changes cannot reuse a stale cache entry.

## Historical release reactivation

Knowledge Studio exposes retained VAV releases to owners and administrators
for every knowledge base that has not been explicitly permanently deleted.
An incident restore must use the release-history action rather than changing
database pointers manually. The request includes the revision the operator
currently sees and a mandatory reason. Under the knowledge-base row lock, VAV:

1. rejects a stale expected revision with `409`;
2. verifies the target release, its immutable source snapshots, and its speech
   lexicon belong to the same tenant and knowledge base;
3. moves the serving and lexicon pointers together without decrementing the
   explicit-revocation generation; and
4. records the actor, prior revision, target revision, and reason in the audit
   log.

The mutable working copy returns to draft review. Calls already in progress
keep their original revision pin, while new calls receive the reactivated
release. This action controls VAV-native retrieval only; it intentionally
rejects a knowledge base bound to a provider-native Smallest.ai agent because
that remote collection needs its own verified rollback mechanism.

## Per-call pinning contract

The serving pointer and explicit-revocation generation are resolved together
when a browser or SIP call is reserved and persisted in
`call_metadata.runtime`. A worker must recover both; malformed persisted values
are hard errors rather than permission to fall forward to the latest release.
An ordinary later publication leaves the generation unchanged and does not
waste the reservation. An explicit pre-admission revocation changes the
generation and blocks it.

Outbound SIP crosses its knowledge-admission boundary before VAV starts the
paid carrier side effect. The same transaction creates the dispatching call
and records `knowledge_admission_state=admitted_before_dispatch` plus a
timezone-stamped `knowledge_admitted_at`. Once committed, a later unapproval or
agent rebind cannot make an answered call disconnect: every retrieval path
continues to use the admitted tenant, knowledge-base, revision, hashes, and
lexicon. Browser and inbound SIP calls instead admit at participant join, so a
revocation that wins before join blocks speech without incurring an outbound
carrier charge.

Pass the immutable revision UUID and knowledge-base UUID together for the
whole call to every retrieval path:

- `load_agent_serving_revision(..., serving_revision_id=revision_pin, knowledge_base_id=kb_pin)`
- `load_agent_speech_lexicon(..., serving_revision_id=revision_pin, knowledge_base_id=kb_pin)`
- `load_agent_knowledge_terminology(..., serving_revision_id=revision_pin, knowledge_base_id=kb_pin)`
- `retrieve_exact_fact(..., serving_revision_id=revision_pin, knowledge_base_id=kb_pin)`
- `retrieve_knowledge_context(..., serving_revision_id=revision_pin, knowledge_base_id=kb_pin)`

The admitted revision-plus-knowledge-base identity can address a historical
revision after a new approval or later agent rebind. Every loader still verifies
the exact tenant, knowledge base, and revision; it never follows the mutable
binding after admission. Revision-only lookups retain current-binding
enforcement for compatibility. Invalid or foreign IDs yield no knowledge, and
no loader silently substitutes mutable content or the newest revision.

## Deployment and backfill

The complete release sequence, QA gates, canary stages, and rollback procedure
are in [Realtime quality production rollout](REALTIME_QUALITY_ROLLOUT.md).

1. First deploy the release's readiness-compatibility commit as the temporary
   pre-schema bridge described in the rollout runbook. It keeps the existing
   application healthy at the single linear revision `021`, `022`, `023`, or
   `024`.
   Freeze knowledge writes while it runs and do not retain it as a post-feature
   rollback image.
2. Apply migrations `20260904_022`, `20260904_023`, and `20260904_024`, in that
   order, from a dedicated migration job while the compatibility bridge remains
   deployed. Revision `024` promotes legacy provider-cleanup markers into a
   durable outbox.
3. Backfill approved legacy knowledge in bounded batches before deploying the
   exact-`024` application or worker. Publishing serving
   revisions can create a missing lexicon, but running both jobs makes each
   artifact class independently observable:

   ```text
   cd backend
   python scripts/backfill_speech_lexicons.py --batch-size 100
   python scripts/backfill_knowledge_serving_revisions.py --batch-size 100
   ```

   Use `--tenant-id <uuid>` for a canary tenant. The command is idempotent and
   never changes draft content. It reports published and quarantined totals and
   exits non-zero after committing the valid rows when any legacy knowledge
   base was quarantined. Repair, review, and reapprove every quarantined row;
   retain that failure in the release evidence even if a later rerun has no
   work.
4. Repair every quarantine, rerun both jobs, and verify each production-bound
   knowledge base shows a live revision in Knowledge Studio before allowing
   source changes or deploying the full release.
5. Deploy the same full release SHA to the API, Celery, frontend, and worker,
   then confirm `/ready` reports exactly `20260904_024`.
6. Run a browser call and a SIP fixture call against the canary agent. Confirm
   call diagnostics contain the expected serving revision ID and that answers
   come from that revision.

Do not delete legacy source rows or historical revisions during rollout. This
rollout rule is distinct from the guarded Knowledge Studio permanent-delete
operation described above. Normal
application rollback keeps the new schema and uses a tested pointer-aware build
with experimental features disabled. Never run the pre-schema bridge after new
knowledge writes. Returning to older code requires frozen writes, drained calls,
a verified backup, full mutable/live reconciliation, a completely drained
`knowledge_provider_cleanups` outbox, and schema downgrade in the order
documented by the rollout runbook. Before downgrading `024`, stop every API,
Beat scheduler, and LiveKit worker that can enqueue provider cleanup; keep the
dedicated Celery worker running until the knowledge queue and cleanup outbox
are drained, then stop it and confirm no task remains active. The migration's
exclusive table lock closes the final database race but does not replace the
operational stop/drain.
