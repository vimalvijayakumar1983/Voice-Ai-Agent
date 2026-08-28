# Campaign dispatch reconciliation

An ambiguous provider dispatch is deliberately never retried automatically.
The worker marks its attempt, contact, and local call as `unknown`,
`dispatch_unknown`, and pauses the campaign so a retry cannot place a second
paid call.

Only a workspace owner or admin may reconcile one of these attempts:

1. List the campaign attempts with
   `GET /api/v1/campaigns/{campaign_id}/attempts?state=unknown`.
2. Verify the call in the provider's authoritative console or API. Record the
   provider call identity, final outcome, and operator evidence outside the
   application according to the workspace incident-retention policy.
3. If a call exists, keep the campaign paused. A signed provider callback will
   bind and reconcile the durable local attempt automatically. If no callback
   arrives, escalate through the trusted platform-operator incident process;
   workspace users cannot attach shared-account provider identifiers because
   those identifiers do not prove tenant ownership.
4. Only when the provider proves that no call was created, send
   `POST /api/v1/campaigns/{campaign_id}/attempts/{attempt_id}/reconcile` with
   `action=release_for_retry` with the reason. This marks the attempt as a
   definitive rejection and releases the contact according to the campaign's
   retry budget.
5. Review the append-only `campaign_attempt.*` audit event. Resume the paused
   campaign explicitly if pending contacts remain.

Never release an attempt merely because it cannot be found quickly. Keep the
campaign paused and escalate to the provider; an incorrect release can dial the
same contact twice.
