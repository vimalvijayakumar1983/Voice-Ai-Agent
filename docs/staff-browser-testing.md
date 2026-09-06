# Staff browser-only agents

This is an opt-in policy for authenticated workspace owners and administrators.
Existing agent defaults and the accepted single-pass receptionist are unchanged.

## Setup

1. Create an Inworld voice agent and open its Runtime controls.
2. Select **Staff browser only**. Use the native realtime **tool-loop** route.
3. Select either an approved knowledge base plus tools, or **MCP tools only**.
4. Save the policy. No phone number or SIP trunk is required for a browser test.
5. Open the browser test. Credential, worker, budget and capacity checks still run.
6. In Integrations, review and grant individual permitted MCP tools explicitly.
   Start a new call after changing tool grants.

An empty tool grant is valid for an audio/setup test. The agent must say that
data access is not enabled; it must not fabricate ERP answers.

## Boundaries

- The policy forbids phone activation, assigned phone numbers and recording.
- Browser admission uses the authenticated user, not caller-provided variables.
- The durable call stores its initiating user. Worker admission and MCP execution
  recheck the user's active owner/admin role and tenant, and the current policy.
- MCP permission and role checks run again after a lookup, before releasing data.
- Staff call details, transcripts, recordings and summaries are hidden from other
  workspace roles. Automatic call-completed webhook delivery is suppressed.
- **This does not enable private financial, customer or patient MCP records.**
  The existing public-data approval and read-only tool contract still applies.
  A separate governed private-data integration is required for those records.
- Disabling browser access blocks new sessions and later MCP tool requests.
  It does not promise immediate termination of an already connected audio room.

## Verification

Backend: `pytest tests/test_staff_browser.py tests/test_mcp_connections.py
tests/test_livekit_browser_session.py tests/test_realtime_runtime.py tests/test_calls.py`.
Run frontend lint, session-boundary tests and production build as well.

Release checks must include PostgreSQL CI and deployment health. Then verify the
real browser can join and receive greeting audio. Do not report live audio quality
or ERP lookup readiness based only on mocked backend tests.
