# AI Dialer workspace — staged release

This workspace is `/dialer`. It is separate from legacy `/campaigns` and shares
the existing guarded direct outbound-call service. It does not replace the
accepted voice/knowledge runtime or change inbound routing.

## Calling modes

| Mode | Queue behavior |
| --- | --- |
| Preview | An operator approves each queued customer before dispatch; retries require new approval. |
| Progressive | At most one reserved/dispatching/active call per campaign. |
| Parallel AI | Independent AI sessions, bounded by configured channels and tenant capacity. |
| Predictive AI pacing | Uses observed answer rates to pace attempts toward a target live-call count. Every possible answer has reserved capacity. Warmup is one new attempt per sweep until 20 terminal samples exist. This is not a traditional human-agent over-dialer. |
| Scheduled callback | Requires an explicit timezone-aware due time. Actual dispatch also requires the permitted calling window and available capacity. |
| Event-triggered | An authenticated, idempotent queue API accepts an external event/customer reference. Connector setup and event subscription are separate. |

## Operator workflow

1. Owner/admin uploads a UTF-8 CSV or XLSX (2 MB / 5,000 rows maximum).
2. Validate, correct reported rows, then import. No partial import on invalid
   data. Duplicate company/phone records are skipped, not overwritten.
3. Create a draft campaign, select the matching company and an approved outbound
   agent, mode, timezone, calling window, channel capacity and attempt budget.
4. Select matching customers and queue them, with a callback time if applicable.
5. Simulate eligibility. This is a policy check, NOT a provider readiness test.
6. After controlled rollout approval, enable execution and explicitly start the
   campaign. Refresh status to see jobs, attempts, outcomes and export CSV.

Supported import columns: `name`, `phone_number`, `company`, `external_id`,
`language`, `timezone`, `notes`, `contact_allowed`, `consent_reference`.
The first three are required. Phone numbers require a country code. Permission
defaults to false; permission true requires a source reference. Re-import never
overwrites opt-out or consent. An opt-out also enters the workspace DNC registry.
This is not automatic national DNCR synchronization or regulatory certification.

Only small, approved call variables go to the provider: `customer_name`,
`company_name`, `preferred_language`, `call_purpose`, `approved_offer`.
Agents must be authored for the intended workflow and use these template fields
where appropriate. Customer notes/history are NOT added to a shared KB or sent
to providers automatically. Knowledge scope must match the campaign's company;
matching the customer company string alone does not prove an agent's KB scope.

## API

All `/api/v1/dialer` routes require an active owner/admin (existing bearer/API-key
authentication and tenant isolation). Interactive users use the normal session.

- `GET /capabilities`
- `GET /customers?offset=0&limit=100&company=...`
- `POST /customers`
- `POST /customers/import?commit=false|true` (multipart `file`)
- `PATCH /customers/{id}/permission` (opt-out only)
- `GET /customers/{id}/history` (last 200 jobs)
- `GET|POST /campaigns`
- `POST /campaigns/{id}/queue`
- `POST /campaigns/{id}/action` (`start`, `pause`, `cancel`)
- `GET /campaigns/{id}/jobs?offset=0` (200/page)
- `POST /jobs/{id}/approve|cancel`
- `GET /campaigns/{id}/simulation|report|export`

Example event/callback submission:

```json
{
  "customer_ids": ["<existing customer UUID>"],
  "event_key": "appointment-reminder-789-v1",
  "available_at": "2026-09-15T10:00:00+04:00"
}
```

Reusing an event key for the same customer returns the same job. A conflicting
explicit timestamp is rejected. A deliberately different key means a new job;
external systems must reuse their stable event ID on delivery retries. Never
embed sensitive customer data in event keys. Do not expose admin tokens publicly.

## Durable execution and safety

- `DIALER_LIVE_ENABLED=false` by default. Import/queue/simulation work without it.
- `DIALER_TENANT_CONCURRENCY=10` default; configured capacity is an upper bound,
  not a guarantee of purchased SIP, AI worker or provider capacity.
- Celery beat scans every 30 seconds; dialer tasks run on the `campaigns` queue.
- Tenant row lock serializes capacity reservations across campaigns.
- Queue reservations commit before broker publication. Undelivered reservations
  are recoverable after five minutes; late tasks recheck state before dispatch.
- Each attempt reserves a deterministic call ID before paid I/O. Worker crashes
  after dispatch starts are never grounds for blind redial. Ambiguous acceptance
  blocks capacity and pauses its campaign pending call/provider reconciliation.
- Pause/cancel, operator authority, customer permission and number are rechecked
  at the shared provider boundary. Existing DNC, consent revocation, provider
  credentials, runtime readiness, immutable knowledge admission, budgets and
  direct-call watchdogs remain in force.
- Busy/no-answer retries wait 24–168 hours and remain bounded by per-job and total
  attempt budgets. Failed or unknown attempts are not automatically retried.
- Pausing/cancelling does not hang up connected calls.
- Collections execution is blocked pending a verified private-data workflow.

## Explicitly not included / rollout gates

This change does not deliver HIS booking transactions, verified debt-collection
actions, payment processing, WhatsApp/email delivery, CRM writeback, automatic
callback extraction from speech, OAuth setup, or a staffed-agent predictive
dialer. Those need separately validated connectors and action contracts.
Reports distinguish completed calls from business results; they must not assert
an appointment is booked without an external booking ID.

Before enabling live execution:

1. Pass PostgreSQL migration and concurrency tests plus existing outbound-call
   regression tests. Local SQLite tests do not validate row locking.
2. Check agent/company knowledge alignment, outbound templates and provider
   variables using a QA agent, not arbitrary imported customer notes.
3. Verify SIP trunk capacity, destination authorization, caller ID, worker
   capacity and provider readiness. Use a controlled test number first.
4. Test answer, busy, no answer, pause, opt-out, interrupted worker and provider
   timeout on each enabled transport. Browser calls do not prove SIP dialing.
5. Configure approved contact lists and calling/compliance policies, then use a
   one-call budget and concurrency one for the first explicitly authorized call.
6. Reconcile billing and call outcomes before raising limits. No automatic live
   rollout is performed by this code change.

## Verification commands

```text
python -m pytest tests/test_dialer.py tests/test_calls.py -q
python -m ruff check app tests migrations
python -m ruff format --check app tests migrations
alembic upgrade head  # disposable/staging PostgreSQL first
npm run test:session-boundary
npm run lint
npm run build
```
