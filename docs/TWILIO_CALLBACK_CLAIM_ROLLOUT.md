# Twilio callback-claim rollout

Direct Twilio dispatches include a high-entropy `vav_callback_claim` query
parameter in the voice and status callback URLs. VAV persists only a
domain-separated SHA-256 digest and uses the raw value only to bind the first
signed callback when it wins the race with Twilio's create-call response.

## Required log controls

The API installs process-wide redaction before it accepts traffic. Keep Uvicorn
access logging enabled and verify a callback access line contains
`vav_callback_claim=[REDACTED]`, never the raw value. The same filter covers VAV
structured log fields and rendered exceptions.

The application cannot control logs captured before a request reaches Uvicorn.
Before production rollout, configure every Railway edge/proxy, load balancer,
WAF, APM agent, tracing collector, and log-drain pipeline to redact the
`vav_callback_claim` query parameter. Where query-parameter redaction cannot be
guaranteed, disable query-string capture for `/api/v1/webhooks/twilio/voice/*`
and `/api/v1/webhooks/twilio/status/*` while retaining method, path, status,
duration, and request ID. Do not sample raw callback URLs into support tools.

After deployment:

1. Place a non-production direct Twilio call.
2. Confirm the callback succeeds if it arrives before the create-call response.
3. Search API, proxy, APM, trace, and drain outputs for `vav_callback_claim=`.
4. Confirm every occurrence is redacted and no raw callback URL was retained.
5. Treat any raw claim found in a log as a secret disclosure: restrict the log,
   expire it under the applicable retention process, and investigate the
   upstream capture configuration before enabling production traffic.
