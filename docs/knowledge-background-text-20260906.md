# Background text ingestion hotfix

## Incident

The production text-source POST on 2026-09-06 failed at the frontend proxy after
30,103 ms. The API proxy recorded 499/client disconnected at 30,011 ms. Text
compilation awaited OpenAI inside the request before saving the source. Raising
the proxy timeout alone would retain this failure mode for longer jobs.

## Change

- AI/automatic text submissions persist the original and a queued compilation
  generation atomically, then return the knowledge-base response immediately.
- The existing knowledge Celery queue drains the durable source-row outbox every
  10 seconds. No new database schema or infrastructure is required.
- Worker claim, inference and publication are separate transactions. Duplicate
  deliveries cannot repeat inference for an already claimed generation. A stale
  generation cannot overwrite a deleted source or a newer retry.
- Existing PDF/text **Structure with AI** uses the same queue. Initial PDF
  upload/OCR and remote provider upload are unchanged by this narrow hotfix.
- Queued/processing/failed drafts cannot become approved merely because a remote
  provider reports indexed. The previous immutable live release is unchanged.
- Original-only sources have preview and retry controls. The selected knowledge
  base polls for progress; no reload is required during extraction.
- Broker failures are retried by the outbox. A queued job older than 10 minutes
  or processing job older than 5 minutes becomes a visible retryable failure.
  There is no automatic repeated paid inference after a worker crash.
- Fast deterministic text mode remains synchronous; it makes no AI request.

## Verification

Regression coverage includes saved-original preview, no inline inference,
duplicate requests/deliveries, early approval blocked, stale completion fenced,
lost broker dispatch recovery, worker timeout, editor revocation, wrong tenant,
source deletion, existing release preservation, and failure/retry in place.
Browser checks use synthetic queued/failed responses and exercise automatic
status refresh, retry visibility and disabled approval. No customer calls occur.

Production rollout requires both API and worker on this commit. Verify an actual
text submission saves promptly, reaches completed/failed through the worker, and
preserves exact source text. Approval/binding remain explicit user decisions.

## Production follow-up

PR #24 passed 1,662 PostgreSQL-backed tests and deployed at main 34b2e1d.
The source retry returned HTTP 200 in 138 ms, with visible queued/processing and
disabled approval. The previous request had eventually saved the original after
the browser disconnected; the retry reused that source instead of duplicating it.
The 2,614-character persisted original exactly matches the trimmed user input.

The background run then exposed the compiler's separate 45-second provider
timeout: two short attempts expired. Background jobs now request one 120-second
attempt, retaining the task deadline and watchdog. Synchronous callers retain
their original timeout/retry policy. Timeout failures get a specific safe message
instead of implying incorrect credentials. Regression tests verify the default
and background client budgets, client cleanup, one attempt, and timeout handling.
