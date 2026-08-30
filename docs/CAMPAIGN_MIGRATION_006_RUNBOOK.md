# Campaign attempt ledger migration runbook

Migration `20260827_006` adds the durable campaign dispatch ledger and a
per-campaign canonical phone-number uniqueness constraint. The migration never
silently deletes or merges contact/call history.

## Preflight

1. Put the API in maintenance mode (or otherwise freeze campaign contact
   writes), pause every running campaign, and drain the `campaigns` Celery
   queue. Keep the old API and campaign workers stopped until the migration and
   same-release API/worker deploy are complete. The migration also takes a
   transactional `SHARE ROW EXCLUSIVE` lock on `campaign_contacts` before its
   duplicate checks as a final race-prevention guard.
2. Export the affected campaign, contact, call, and outcome rows before making
   any reconciliation change.
3. Run this PostgreSQL query. It applies the same formatting normalization used
   by the application and identifies contacts that would become duplicates:

```sql
WITH normalized AS (
    SELECT
        id,
        campaign_id,
        phone_number,
        CASE
            WHEN left(
                regexp_replace(btrim(phone_number), '[[:space:]().-]', '', 'g'),
                2
            ) = '00'
            THEN '+' || substr(
                regexp_replace(btrim(phone_number), '[[:space:]().-]', '', 'g'),
                3
            )
            ELSE regexp_replace(
                btrim(phone_number), '[[:space:]().-]', '', 'g'
            )
        END AS canonical_number
    FROM campaign_contacts
)
SELECT
    campaign_id,
    canonical_number,
    array_agg(id ORDER BY id) AS contact_ids,
    array_agg(phone_number ORDER BY id) AS stored_numbers
FROM normalized
WHERE canonical_number ~ '^\+[1-9][0-9]{7,14}$'
GROUP BY campaign_id, canonical_number
HAVING count(*) > 1;
```

If the query returns rows, do not run the migration yet. Export those rows,
choose the surviving contact for each campaign and number, and move or archive
the duplicate's call/outcome history according to the workspace retention
policy. Also run the following guard for exact duplicate stored values,
including invalid legacy numbers that cannot be canonicalized:

```sql
SELECT
    campaign_id,
    phone_number,
    array_agg(id ORDER BY id) AS contact_ids
FROM campaign_contacts
GROUP BY campaign_id, phone_number
HAVING count(*) > 1;
```

Re-run both queries until they return no rows. The migration will then
canonicalize valid legacy numbers before creating the uniqueness constraint.

## Deploy and rollback

Run `alembic upgrade head`, deploy the API and worker from the same release,
then restart campaign workers. A downgrade is deliberately refused while any
attempt is `claimed`, `dispatching`, `accepted`, or `unknown`; pause campaigns
and reconcile those provider outcomes first. Drain the provider callback outbox
until every row is `dispatched` as well. Removing an active idempotency ledger
or undelivered callback record can otherwise permit a duplicate paid call or
lose terminal billing and campaign reconciliation.
