# Authentication migration 008 runbook

Migration `20260827_008` canonicalizes email identity, reserves one unresolved
invitation per canonical email, and introduces rotating refresh-session
families. Run these checks against the production database before Railway's
pre-deploy `alembic upgrade head` step:

```sql
SELECT
    lower(btrim(email)) AS canonical_email,
    count(*) AS account_count,
    array_agg(id::text ORDER BY id::text) AS user_ids
FROM users
GROUP BY lower(btrim(email))
HAVING count(*) > 1;

SELECT
    lower(btrim(email)) AS canonical_email,
    count(*) AS invitation_count,
    array_agg(id::text ORDER BY id::text) AS invitation_ids
FROM user_invitations
WHERE accepted_at IS NULL
  AND revoked_at IS NULL
  AND expires_at > now()
GROUP BY lower(btrim(email))
HAVING count(*) > 1;
```

Both queries must return zero rows. Also correct any blank user email before the
upgrade. If canonical user duplicates exist, stop and identify the real account
owners and tenant history. Correct a proven typo or follow an approved account
deletion/migration procedure; never auto-merge or discard distinct accounts.
The migration intentionally aborts rather than guess.

For duplicate active invitations, retain the intended invitation and explicitly
revoke each superseded row by setting `revoked_at` to the current time. Preserve
the invitation and audit records. Expired unresolved invitations need no manual
cleanup: the migration marks them resolved with reason `expired`, and the API
continues to report their status as expired while allowing a replacement.

Before the application cutover, tell users that refresh tokens issued before
008 lack the required `jti` and `family_id` claims and will require a new login
after their existing access token expires.

Rollback is deliberately guarded. Stop authentication traffic first. If any
`refresh_sessions` rows exist, globally invalidate the corresponding signed
JWTs by rotating `SECRET_KEY` (or wait through the maximum token lifetime), back
up and explicitly clear the ledger, and only then downgrade. Starting the old
stateless refresh endpoint while a new-format signed token remains valid can
revive a session that 008 had revoked.
