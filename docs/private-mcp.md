# Private staff MCP access

This opt-in mode provides read-only ERP lookups in authenticated owner/admin browser calls. Existing public-safe MCP connections and receptionist agents retain their current policy. This is not phone-caller authentication, patient-record access, or permission to execute ERP writes.

## Activation

1. Obtain an upstream bearer credential actually restricted to the intended company and read-only operations. A company label and MCP readOnlyHint are not security boundaries.
2. Review the processing/retention arrangement: relevant results reach the configured Inworld voice/LLM service, LiveKit carries audio, and VAV stores conversation text. This feature does not establish regulatory compliance or change provider retention.
3. Edit the connection to Private ERP. Changing mode clears existing grants and approvals. Changing credential, endpoint or company also requires rediscovery.
4. Select reviewed read-only tools, named active owners/admins, and an eligible staff-browser-only, MCP-only native Inworld tool-loop agent. Phone activation, assigned numbers and recording must be off. One connection per private agent is required; separate agents isolate companies.
5. Explicitly confirm the scope and processing approvals and save permissions. Start a new browser call. Update any setup-only greeting/prompt to describe the approved capability without claiming unsupported operations.

No production tool grants or attestations are enabled by deploying this code.

## Enforcement and evidence

Browser admission pins the connection and authenticated user before token issuance. Each tool checks current tenant, call, user role/activity, selected user, agent, connection and tool grants before execution and after the result. Old public sessions cannot acquire private access. Revoked results are withheld; already spoken data cannot be recalled.

Private lookup audit records contain actor, call, connection, tool/schema identifiers and outcome, not arguments or ERP results. An unavailable audit store blocks the lookup. Permission changes are audited. Private call detail/transcript access is restricted to the initiating owner/admin. Automatic post-call AI analysis, manual reanalysis and external call webhooks are disabled; provider inference for the live conversation still occurs.

## Test and release gates

Run `pytest tests/test_private_mcp.py tests/test_mcp_connections.py tests/test_staff_browser.py` plus the full backend suite and frontend lint/test/build. Tests use synthetic data and mocked upstream calls, not production ERP records. Verify company-scoped credentials separately against an authorised fixture before live financial queries. Deploy capability with no grant changes, then perform a staff browser canary using one explicitly approved bounded lookup.

## Limits

Only named workspace owners/admins are supported initially. No interactive OAuth, per-customer phone identity, write operations, or multi-company private session. Upstream company/operation enforcement remains essential even when tools declare themselves read-only. VAV stored transcripts are not deleted by revoking a connection; existing retention policy still applies.
