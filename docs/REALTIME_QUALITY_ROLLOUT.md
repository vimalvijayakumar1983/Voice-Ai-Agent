# Realtime quality production rollout

This is the operator runbook for releasing immutable knowledge, speech-quality
artifacts, greeting prewarm, truthful call diagnostics, and the experimental
native Inworld single-pass path. The existing `tool_loop` path remains the
production control. Do not enable single-pass broadly merely because the code
has deployed or **Test readiness** is green.

## Release invariants

- A knowledge draft is mutable; a serving revision is immutable.
- Approval atomically publishes a serving revision and its speech lexicon. A
  failed publication leaves the previous release active.
- Editing a source returns the draft to review but does not alter the active
  release. Explicit approval revocation is the only normal action that clears
  the serving pointers.
- Browser, LiveKit SIP, and VAV-native Twilio calls resolve a serving revision
  when the call is reserved. The revision ID and hashes remain fixed for that
  call, even if another release is approved while it is running.
- Each native Twilio call grants exactly one durable media-session claim. A
  duplicate WebSocket must fail closed without terminalizing the winning
  session or writing its metrics.
- A tenant Twilio DID is routable only after VAV proves that the tenant-owned
  account owns it and that Twilio sends voice requests directly by `POST` to
  the current VAV inbound URL. Shared platform credentials are not routing
  authority for tenant-owned Sarvam or ElevenLabs agents.
- An invalid, foreign, or unbound call pin fails closed. It must never fall
  forward to mutable content or the newest release.
- `single_pass_experimental` is enabled per agent. VAV does not randomly split
  calls within one agent, so percentages below mean explicit agent/DID/customer
  cohorts.
- Diagnostic recording remains off. The current setting records policy intent
  only; it does not authorize or start capture.

## Go/no-go evidence

Record the Git SHA, Railway deployment IDs, database backup/restore reference,
canary tenant ID, control agent ID, QA clone ID, knowledge serving revision ID,
and operator in the release ticket. Do not place credentials, phone numbers,
transcript text, or audio paths in the ticket.

The same audited Git SHA must run on the API, Celery, and `livekit-agent`.
Before deployment, from `backend`, require:

```text
python -m pytest tests/test_tier1_quality.py tests/test_exact_fact_retrieval.py tests/test_speech_lexicon.py tests/test_knowledge_serving.py -q
python -m pytest tests/test_inworld_single_pass.py tests/test_greeting_cache.py tests/test_audio_replay_canary.py -q
python -m pytest tests/test_inworld_provider.py tests/test_livekit_provider.py tests/test_livekit_browser_session.py tests/test_call_metadata.py -q
python -m pytest tests/test_native_realtime_knowledge.py tests/test_native_twilio_inbound_limits.py tests/test_native_knowledge_admission_postgres.py tests/test_calls.py tests/test_workspace_provider_credentials.py -q
python -m pytest tests/test_twilio_route_security.py tests/test_twilio_route_security_postgres.py tests/test_twilio_webhooks.py tests/test_realtime_session_finalization.py tests/test_cost_reporting.py -q
ruff check app tests scripts migrations
alembic heads
```

`alembic heads` must show exactly `20260904_024`. The complete backend,
frontend, lint, and build suites remain mandatory CI gates; the focused commands
above make failures in this release boundary easier to diagnose.

## Schema and legacy backfill

Alembic applies these additive migrations in order:

| Revision | Adds | Mutable pointer |
| --- | --- | --- |
| `20260904_022` | Immutable, versioned speech lexicon artifacts | `knowledge_bases.speech_lexicon_artifact_id` |
| `20260904_023` | Immutable knowledge releases and compiled source snapshots | `knowledge_bases.serving_revision_id` |
| `20260904_024` | Durable, leased provider-artifact cleanup outbox and monotonic explicit-revocation generation | Provider bind/publish safety gate and per-call revocation fence |

Both pointers are nullable for rolling-deploy compatibility. That compatibility
window is not the desired steady state.

1. Build and deploy the PR's first commit, **Make readiness compatible with
   staged schema rollout**, across the existing application. It contains no
   feature or schema dependency and accepts the single linear Alembic revision
   `021`, `022`, `023`, or `024`. This is a **pre-schema transition bridge only**:
   freeze knowledge edits/approvals while it is deployed, and never use it as a
   post-feature rollback target because it does not understand immutable
   serving pointers.
2. Take and verify a production database backup. Keep knowledge edits and
   approvals frozen until the full release is deployed.
3. From the audited full-release image, run one dedicated migration job with
   `alembic upgrade head`. The compatibility bridge remains ready while Alembic
   advances. Confirm the database is stamped only at `20260904_024`; do not let
   ordinary web-pod startup race to apply migrations.
4. With the compatibility bridge still serving traffic, backfill one canary
   tenant from the audited full-release image in bounded, idempotent batches:

   ```text
   cd backend
   python scripts/backfill_speech_lexicons.py --tenant-id <canary-tenant-uuid> --batch-size 100
   python scripts/backfill_knowledge_serving_revisions.py --tenant-id <canary-tenant-uuid> --batch-size 100
   ```

5. Treat either command's non-zero exit as a quarantined legacy row, even
   though valid rows in the same batch were committed. Each command reports
   both its published and quarantined totals. Repair every quarantined
   knowledge base, explicitly review and approve it again, and rerun the
   command. Do not use a later zero-work rerun to erase the earlier failure
   from the release evidence.
6. Re-run both commands. A clean canary should report zero new artifacts or
   revisions and zero quarantines on the second run.
7. Backfill the remaining tenants in the same bounded batches:

   ```text
   python scripts/backfill_speech_lexicons.py --batch-size 100
   python scripts/backfill_knowledge_serving_revisions.py --batch-size 100
   ```

8. Re-run both commands and retain their totals in the release evidence. Repair
   every remaining quarantine and repeat until both jobs report zero new work
   and zero quarantines. Do not permit edits to an approved legacy knowledge
   base until its live serving revision is visible in Knowledge Studio.
9. Before deploying the exact-`024` application or worker, verify every
   production-bound knowledge base has both immutable pointers and a readable
   serving revision with searchable revision sources. This includes every base
   bound to an active Inworld, Sarvam, or ElevenLabs agent, and every base whose
   agent owns an assigned production route. An inactive, pending-review, or
   non-searchable base must be deliberately removed from production routing or
   repaired and approved; it must not be counted as successfully backfilled.
10. Before replacing the compatibility bridge, pause native Twilio outbound
    dispatch and move every Sarvam/ElevenLabs production DID to the approved
    maintenance TwiML route. The bridge cannot stamp immutable pins on those
    calls. Drain its existing native Twilio calls, respecting the largest
    configured call-duration limit, and require this PostgreSQL check to return
    zero rows:

    ```sql
    SELECT id, direction, status, started_at
    FROM calls
    WHERE provider = 'twilio'
      AND status NOT IN (
            'completed', 'failed', 'busy', 'no_answer', 'canceled', 'cancelled'
          )
      AND COALESCE(
            metadata->'runtime'->>'speech_provider',
            metadata->>'speech_provider'
          ) IN ('sarvam', 'elevenlabs');
    ```

    Do not waive this gate by adopting mutable legacy knowledge. Retain the
    maintenance route until the exact-`024` API is healthy and one canary for
    each enabled native provider has produced a durable call pin.
11. Only after the knowledge verification and native-call drain both succeed,
    deploy the same full release SHA to API, Celery, frontend, and
    `livekit-agent`. Its readiness probe requires exactly `20260904_024`,
    closing the compatibility window after all new-call paths can obtain
    immutable pins. Restore each production DID and outbound dispatch only
    after its canary passes.
12. Complete the QA gates below for the canary tenant before promoting traffic.

The serving-revision backfill can create a missing lexicon itself, but running
the lexicon job first makes each artifact class independently observable and
keeps failures easier to repair.

## Knowledge publication and call pins

For a new company, compile and review every source, run the Tier-1 quality suite,
and approve only when all required sources are searchable. Approval snapshots
compiled text, structured facts, chunks, entities, metadata, and the immutable
lexicon; it then moves the live pointers in the same transaction.

For an update, leave the previous release serving while the replacement is
compiled and reviewed. Calls reserved before the pointer swap use the previous
revision; calls reserved after it use the new revision. Confirm each call shows:

- `knowledge_serving_revision_id`
- `knowledge_serving_knowledge_base_id`
- `knowledge_serving_content_sha256`
- `knowledge_source_revision_sha256`
- `knowledge_serving_revocation_generation`
- `speech_lexicon_artifact_id`
- `speech_lexicon_content_sha256`

The revision ID and revocation generation loaded by the worker must equal the
values stored when the call was reserved. An ordinary blue/green publication
does not change the generation, so a reservation may safely start on its
historical immutable revision. Explicit unapproval increments the generation
under the knowledge-base lock and rejects reservations that have not yet been
admitted. A mismatched fence, missing pin on a newly published knowledge base,
or changed hash is a release blocker. Historical releases must be retained for
audit and in-flight calls while the knowledge base exists. The explicit
permanent-delete operation is a separate data-erasure boundary and cannot run
until every call pinned to that knowledge base is terminal.

For LiveKit SIP and VAV-native Twilio outbound calls, confirm the reservation also contains
`knowledge_admission_state=admitted_before_dispatch` and
`knowledge_admitted_at` before the telephony provider request begins. That
durable boundary prevents a later revoke or rebind from turning an already
dialed/answered call into silence; the call continues only on its admitted
immutable release. Browser and inbound LiveKit SIP calls admit when the verified
participant joins and therefore remain fail-closed to an earlier revoke.
Inbound Sarvam/ElevenLabs calls instead persist
`knowledge_admission_state=admitted_before_media_stream` before VAV returns
Twilio's streaming TwiML. A duplicate signed `CallSid` may reuse that media URL
only when its provider, direction, numbers, agent, transport, speech provider,
and inbound admission marker all match the original reservation.
The same transaction reserves the agent's daily-call, concurrent-call, and
monthly-budget capacity before inserting an active call. Limit exhaustion,
missing knowledge, or a corrupt release must create at most one terminal audit
row per `CallSid` and return the controlled unavailable-and-hang-up TwiML; none
may escape as a provider-visible HTTP 500. The local per-agent guard gives
single-process SQLite environments the same distinct-`CallSid` limit behavior;
PostgreSQL's transaction advisory lock remains the production cross-replica
authority.

Readiness checks the release and retained lexicon identity without loading
compiled source bodies; aggregate source checks keep the operation bounded for
large websites. If an authenticated Twilio media stream cannot load its pinned
runtime configuration, VAV row-locks and terminalizes only that nonterminal
inbound reservation so it cannot consume capacity indefinitely. Delayed voice
or nonterminal status callbacks cannot reopen `terminal_unknown`; only a
definitive provider outcome may reconcile it. A rejected inbound call is marked
answered with a one-second minimum estimate and
`cost_state=pending_provider_billing_sync` until the provider callback supplies
the exact billable duration.

LiveKit SIP inbound follows the same accounting principle. The worker first
persists an answered, pending-reconciliation Call reservation and only then
loads credentials and immutable knowledge. A dependency, capacity, revocation,
or session-start failure terminalizes that exact reservation, emits the normal
completion outbox action, and removes the LiveKit room under cancellation
shielding. A duplicate SIP job cannot fail or delete the legitimate owner.

Greeting prewarm may make a paid TTS request before `session.start()` succeeds.
When that occurs, cost reporting includes the observed direct TTS characters
and keeps provider reconciliation pending, while it does not invent STT or LLM
usage. `media_stream_started=false` means the conversational speech pipeline
never opened; it does not mean a separately evidenced prewarm request was free.

## Provider and route readiness

**Test readiness** is a paid, rate-limited production-boundary test, not a
credential-presence check. Before Sarvam or ElevenLabs activation it must prove:

- ownership and direct-POST configuration of every assigned Twilio DID;
- the selected primary TTS voice;
- the exact Sarvam Saaras realtime STT language and WebSocket handshake;
- Sarvam emergency TTS when ElevenLabs is primary; and
- the selected OpenAI model's required tool-calling contract.

Each result is reported separately and bounded so one stalled provider cannot
hold the request indefinitely. An absent workspace speech/LLM credential may
use the explicitly configured platform key; an existing but unreadable
workspace credential always fails closed and must never fall through to that
key.

Saving, rotating, or deleting Twilio, LiveKit SIP, Sarvam, ElevenLabs, Inworld,
or OpenAI credentials returns every dependent active profile to draft. Re-run
**Test readiness** and explicitly activate each affected agent. Provider
boundary locks, agent-runtime locks, and credential-row locks are acquired in
that order so rotation cannot race activation.

After this release, every existing tenant-owned Twilio profile lacks the new
route proof until it is re-verified. Do not bulk-edit database status fields or
blindly deactivate routes with a migration. For each production agent, confirm
the number's Twilio Voice Configuration uses the exact current VAV URL and
`POST`, run **Test readiness**, then activate it. Changing the credential,
assigned DID set, public `BASE_URL`, callback URL, or method invalidates the
proof and requires the same process.

Outbound calls persist whether their callback authority is tenant-owned or the
explicit legacy platform account, along with a non-reversible credential
fingerprint. Voice and status callbacks accept only that bound account and
credential; a missing or unreadable tenant credential cannot be replaced by a
platform signature.

## Two-tier quality gates

### Tier 1: deterministic CI

Run the deterministic harness before any provider or audio test:

```text
cd backend
python -m pytest tests/test_tier1_quality.py tests/test_exact_fact_retrieval.py -q
```

It covers paraphrases, unsupported questions, expected exact-fact intents,
expected internal evidence IDs, and forbidden-value leakage without an LLM,
audio, or provider charge. Add a fixture whenever a production failure reveals
a reusable phrasing or fact class. A tenant-specific incident must produce a
generic regression case where possible; do not hard-code one caller sentence
into the runtime.

### Tier 2: browser and SIP audio replay

Create a QA/test/canary agent clone bound to the same real knowledge base and
serving revision as the production control. A toy knowledge base is not a valid
canary for production retrieval or lexicon behavior.

First validate each manifest without starting a live session:

```text
cd backend
python scripts/replay_audio_canary.py tests/quality/manifests/real_kb_qa_replay.example.json
python scripts/replay_audio_canary.py tests/quality/manifests/real_kb_qa_interruption_replay.example.json
python scripts/replay_audio_canary.py tests/quality/manifests/real_kb_qa_sip_replay.example.json
```

Supply the environment values named by the dry run. The agent must be explicitly
allowlisted in `VAV_QA_REPLAY_ALLOWED_AGENT_IDS`; the SIP fixture must also be
allowlisted in `VAV_QA_REPLAY_ALLOWED_SIP_FIXTURE_IDS`. Then run one bounded
replay at a time with `--confirm-live`.

The browser replay tests the same VAV worker, Inworld session, knowledge release,
recognition, response, voice, and interruption logic without carrier media. The
SIP replay publishes audio into a pre-created, allowlisted test fixture; the
tool never originates a phone call. Both gates are required because WebRTC does
not prove e&/SIP/RTP behavior.

An exit code of `0` is a pass, `1` is a quality-gate failure, and `2` is a setup
or execution failure. Keep the sanitized JSON report. It intentionally excludes
transcript text, audio paths, tokens, telephone numbers, and internal evidence
identifiers.

## Greeting prewarm gate

Greeting synthesis begins while the realtime session connects. Static greetings
can use a bounded process-local cache; personalized templates and oversized
audio are streamed but never retained. A cache key changes with agent, greeting
hash, voice, model, language, speech rate, delivery mode, and cache schema
version.

Evaluate cold and warm starts separately using:

- `greeting_cache_status`: `hit`, `miss_cached`, `miss_oversize`,
  `bypassed_personalized`, or `failed`
- `greeting_preparation_overlap_ms`
- `greeting_synthesis_first_frame_ms`
- `greeting_preparation_lead_ms`
- `greeting_synthesis_total_ms`
- `greeting_first_frame_ready_before_session`
- `participant_active_to_first_server_speaking_ms`
- `worker_job_entry_to_first_server_speaking_ms`

The supplied browser replay requires
`participant_active_to_first_server_speaking_ms <= 1200`; the SIP fixture allows
`<= 1500`. `call_open_to_greeting_ms` remains only as a legacy runtime-admission
measurement and must not be used as a launch gate because it excludes earlier
worker, participant, credential, and knowledge-loading time. A cache hit is not
itself a pass, and a cache miss is not itself a failure. These server-side fields
still stop at LiveKit's speaking-state boundary; audio replay and a listening
check remain the caller-audible gate. Investigate `failed` and repeated
`miss_oversize` states before promotion.

## Telemetry limits

Interpret diagnostics as measured evidence, not as broader claims:

- `stt_session_update_serialized_*` proves the model and language VAV serialized
  immediately before send. It does not prove Inworld received or honored them.
  `stt_session_update_provider_acknowledgement_observed=false` is an explicit
  evidence gap, not a failed readiness probe.
- `stt_provider_reported_language` is meaningful only when
  `stt_provider_language_reported=true`; otherwise the provider did not supply
  a usable language field for that transcript event.
- Response latency ends at the LiveKit server response/speaking boundary.
  Downstream network transport, browser rendering, SIP/RTP delivery, and sound
  heard by the caller are unobserved. Audio replay plus a human listening check
  remains necessary.
- Always read latency percentiles with `turn_latency_sample_count`. A percentile
  from a very small call is diagnostic, not a population SLO.
- `runtime_usage_components_complete` means all components expected for that
  runtime emitted some usage. It does not mean the provider invoice, Railway
  allocation, plan fees, taxes, or e& carrier cost has been reconciled. Missing
  provider usage stays `null`, never zero.
- Public call metadata contains bounded counts and outcomes, not raw source or
  evidence IDs. Use the governed internal audit path for those identifiers.

## Agent-level single-pass canary

The control agent remains `tool_loop`. Configure a QA clone with Native Inworld
Realtime and `single_pass_experimental`, save it, run **Test readiness**, and
activate it only after both quality tiers pass. Readiness must prove the live
single-pass provider contract, including tool lockout and disabled automatic
provider responses; it does not replace conversation QA.

Promote by explicit cohorts:

| Stage | Scope | Minimum gate before advancing |
| --- | --- | --- |
| Lab | QA clone only | Tier 1, browser proper-name replay, interruption replay, SIP fixture replay, and human audio review pass |
| 5% | Selected low-risk agents/DIDs representing about 5% of planned traffic | No tenant-routing breach, no invented regulated fact, no unexplained pin/hash mismatch, and latency/recognition within the approved manifest |
| 25% | A larger named agent/DID cohort | Same gates across every enabled language/accent; cost and provider-usage gaps reviewed |
| 100% | All approved agents | Pilot acceptance matrix in `LIVEKIT_INWORLD_RUNTIME.md` passes and rollback has been rehearsed |

Each canary agent sends all its new calls through its configured mode. Maintain
a written cohort map; do not describe the rollout as a statistical 5% per-call
split unless an independently audited traffic router is later added.

Stop promotion immediately for cross-tenant retrieval, invented medical,
price, doctor, or policy facts, repeated unexpected-script transcription,
incomplete interruption turns, pin mismatches, readiness drift, or an unexplained
tail-latency regression. A single safety breach overrides aggregate averages.

## Rollback

For a single-pass incident, stop new canary traffic, switch the affected canary
agents to `tool_loop`, run **Test readiness**, reactivate, and verify one browser
and one SIP fixture call. Alternatively route the affected DID/cohort back to
the already-tested control agent. Do not delete the failed profile, call rows,
or diagnostic evidence.

For a knowledge-release incident, stop approving or editing that knowledge base
and use Knowledge Studio's release history to reactivate the last verified VAV
release. The action requires the currently observed live revision and a reason;
the API compares and swaps both the serving-revision and speech-lexicon pointers
under the knowledge-base lock and records the actor, old revision, target
revision, and reason. A stale operator view fails with `409` instead of
overwriting a newer decision. Do not manually rewrite pointer columns. Calls
already running keep their stored revision pin; historical releases must remain
present unless an operator later uses the guarded permanent-delete operation
after every pinned call is terminal. A provider-native Smallest.ai collection needs its separate verified
provider rollback route and cannot use this VAV pointer action.

For an application incident, move traffic off the new lane and deploy a tested
**pointer-aware rollback build** from this full release with experimental
single-pass disabled and all affected agents on `tool_loop`. Keep migrations
`022`, `023`, and `024` in place, then verify API, Celery, frontend, worker, and the
approved-release invariant: every approved native knowledge base has a live
pointer whose manifest exactly matches its current approved draft.

Never deploy the pre-schema readiness bridge after any pointer-aware knowledge
write. If an emergency requires returning to code older than this release,
first stop or scale to zero every API process, Celery Beat scheduler, and
LiveKit worker that can create knowledge cleanup or publication work. Keep the
dedicated Celery worker running long enough to drain the knowledge queue and
every unfinished row from `knowledge_provider_cleanups`; verify the remote
deletions, then stop the worker and confirm no knowledge task is running. Freeze
all remaining knowledge writes, drain calls, take and verify a backup, and
reconcile or restore every mutable knowledge row to its live release. Only then
downgrade `024`, `023`, and `022` in that order and start the old image.
Migration `024` takes an exclusive table lock and refuses to downgrade while
unfinished cleanup work exists; the operational stop/drain is still required
so a producer cannot race the migration boundary. A schema
downgrade destroys the new pointers and immutable artifact tables; an old binary
on the new schema can silently approve mutable content without publishing a
matching release.

Record the reason, time, affected cohort, last successful call ID, failed call
IDs, knowledge revision, application SHA, and validation results in the incident
log.

## Recording remains blocked

Keep `diagnostic_recording_mode=off`. Selecting
`livekit_egress_explicit_consent` is intentionally fail-closed and blocks
activation because VAV does not yet implement or verify:

1. affirmative per-call recording consent before capture;
2. a governed LiveKit Egress start/stop lifecycle;
3. an encrypted, tenant-approved regional destination;
4. retention, deletion, playback authorization, and access auditing.

The current LiveKit lane therefore provides transcript and operational
diagnostics, not a call recording. Provider-hosted recording URLs and policy
intent are not substitutes for the controls above. Recording can become a
separate governed release only after legal approval, regional storage design,
threat review, lifecycle tests, and an explicit production acceptance gate.
