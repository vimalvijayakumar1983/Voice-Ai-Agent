# LiveKit + direct Inworld production runtime

## Locked responsibility split

| Layer | Owner | Responsibility |
| --- | --- | --- |
| Carrier | Customer e& SIP trunk | DID, inbound/outbound PSTN minutes, CLI and local regulatory obligations |
| Realtime transport | LiveKit Cloud | SIP ingress/egress, rooms, media, dispatch, interruption transport and diagnostics |
| Speech and reasoning | Inworld, called directly by VAV | Streaming STT, OpenAI-compatible Router LLM and TTS 2 |
| Product control plane | VAV | Agent policy, approved knowledge retrieval, CRM actions, limits, transcript, audit and cost report |

VAV does not use LiveKit Inference in this lane. This prevents a second AI-services billing path and preserves one encrypted Inworld credential per workspace. Smallest and ElevenLabs are not part of this production lane. Sarvam/Twilio remains unchanged as a rollback route until the pilot passes.

## One-minute inbound example

1. A patient calls the clinic's e& DID.
2. e& sends the SIP call to the LiveKit SIP endpoint.
3. The verified inbound trunk and dispatch rule create a room and dispatch the generic `vav-inworld` worker.
4. The worker resolves exactly one active tenant/agent from the trusted LiveKit trunk ID plus called DID, then opens a VAV call ledger row. Inbound metadata never selects an agent.
5. Inworld STT streams the caller's speech. For any business fact, the LLM must call `search_approved_knowledge`; the tool searches only the approved knowledge base bound to that agent.
6. Inworld Router generates the response and Inworld TTS 2 speaks it through the LiveKit room.
7. On shutdown, VAV saves transcript turns, token/character/audio usage, SIP IDs, duration and provider labels. The cost report converts each measurable component into USD and AED.

Outbound is the same after connection. VAV creates a LiveKit SIP participant through the recorded outbound trunk; the AI costs are the same for the same duration and usage. Only the customer's e& carrier tariff normally differs by destination. That carrier invoice is a direct customer cost and is deliberately excluded from VAV margin.

## Browser playground path

The VAV playground can dispatch the same `vav-inworld` worker through LiveKit
WebRTC without placing a PSTN call. The operator grants microphone permission
first; VAV then creates a durable browser-call row and returns a short-lived,
room-scoped LiveKit token that can publish microphone audio and subscribe to
the agent. Tenant, agent and call selection come only from VAV's signed dispatch
envelope. Browser participant metadata is never trusted for routing.
Each Start attempt also carries a random `Idempotency-Key`. An authenticated
retry can re-mint only the remaining lifetime of the same issued room token;
it cannot create a duplicate call or extend the original join deadline, and
VAV never persists the bearer token itself.

The browser and phone paths intentionally share the agent prompt, approved
knowledge, Inworld STT/Router/TTS configuration, tools, limits, transcript,
usage and finalization code. They do not prove the same things:

- **Test in browser** validates VAV, LiveKit WebRTC, the worker, Inworld and
  knowledge retrieval without consuming an e& carrier minute.
- **Call assigned number** validates the real DID, e& SIP trunk, LiveKit SIP
  ingress, caller audio and carrier routing.
- **Place outbound test call** validates the outbound trunk, caller ID and
  destination routing and therefore must remain consent- and DNC-gated.

Browser calls are recorded as `livekit_webrtc`, not `livekit_sip`. Cost reports
include their metered Inworld usage and fixed-worker allocation gap, but never
add the LiveKit third-party SIP line item or an e& carrier charge. An unused
token or interrupted connection is terminalized by the same stale-session
recovery controls and cannot reserve concurrency indefinitely.

## Reference cost for a 60-second connected call

Assumptions: LiveKit third-party SIP overage, conservative public on-demand Inworld rates, 1,000 TTS characters, 1,000 LLM input tokens, 500 output tokens, an explicitly selected GPT-4o mini Router route, and no e& carrier charge.

| Component | USD | AED at 3.6725 |
| --- | ---: | ---: |
| LiveKit third-party SIP | 0.004000 | 0.014690 |
| Inworld STT ($0.15/hour) | 0.002500 | 0.009181 |
| Inworld TTS 2 ($25/1M characters) | 0.025000 | 0.091813 |
| Router LLM (GPT-4o mini example) | 0.000450 | 0.001652 |
| **Estimated variable total** | **0.031950** | **0.117336** |

The agent worker is a custom Railway deployment, so LiveKit Cloud's deployed-agent session fee is not charged. Its fixed Railway hosting cost is not included in the variable total and leaves call-level pricing **partial** until an operator supplies an allocation. If recording is enabled, add the current LiveKit recording rate (the VAV rate card currently uses $0.005/min). Plan subscriptions, taxes, negotiated discounts and the customer's e& bill are reconciled separately. Router model `auto` is also reported as **partial pricing** until provider usage identifies the actual routed model; VAV does not substitute a GPT model or present a proxy as invoice-complete cost.

Public references: [LiveKit pricing](https://livekit.io/pricing), [Inworld pricing](https://inworld.ai/pricing), [Inworld Router OpenAI compatibility](https://docs.inworld.ai/router/openai-compatibility), and [LiveKit Inworld plugin](https://docs.livekit.io/agents/models/tts/plugins/inworld/).

## Railway service contract

`livekit-agent` is a separate, private, always-on Railway service. It must use the same Git commit and backend image as the API, but it must never inherit the API or Celery start command.

| Setting | Required value |
| --- | --- |
| Source | The same GitHub repository and production branch as `api` |
| Root directory | `/backend` |
| Builder | `backend/Dockerfile` |
| Start command | `python -m app.livekit_runtime.worker start` |
| Health path | `/` |
| Replicas | At least one always-on replica |
| Public domain | None |
| Restart policy | On failure, with bounded retries |

Do not add a repository-level `railway.toml` for this service. Railway applies repository configuration across services, while this monorepo requires different roots, start commands, health paths and pre-deploy behavior for API, Celery, frontend and LiveKit. Configure the service explicitly in Railway.

Railway injects `PORT`. The VAV worker passes that port to LiveKit Agents' built-in HTTP server. `GET /` returns `503` after a failed LiveKit connection and `200 OK` while the worker is healthy. `GET /worker` returns private worker metadata including `agent_name`, active jobs and SDK version. Do not set `PORT` manually and do not expose either endpoint on a public domain.

### Full production variable contract

The LiveKit process imports VAV's shared production settings and decrypts tenant-owned provider credentials from PostgreSQL. An Inworld voice can use direct OpenAI for reliable VAV knowledge and action tool calls while retaining Inworld STT/TTS; Inworld Router remains selectable only when its live tool-calling readiness gate passes. The worker therefore needs more than the four LiveKit values. Copy or reference the following values from the running API/worker configuration; never copy them through chat, logs or source control.

| Variable group | Variables | Rule |
| --- | --- | --- |
| Data plane | `DATABASE_URL`, `REDIS_URL` | Use Railway references to the existing managed services |
| Environment | `APP_ENV`, `BASE_URL`, `CORS_ORIGINS`, `TRUST_RAILWAY_PROXY_HEADERS`, `REGISTRATION_MODE` | Use the exact production values already accepted by API startup |
| Encryption | `SECRET_KEY`, `INTEGRATION_ENCRYPTION_KEY` | Must be identical to API/worker; the integration key decrypts workspace Inworld and OpenAI keys |
| Shared startup gate | `SMALLEST_API_KEY`, `SMALLEST_WEBHOOK_SECRET`, `SMALLEST_WEBHOOK_ID` | Required by the current shared production validator even though this lane never calls Smallest |
| Conditional bootstrap | `BOOTSTRAP_OWNER_EMAIL` | Required only while `REGISTRATION_MODE=bootstrap` |
| LiveKit | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_AGENT_NAME` | Same project on API and worker; agent name is exactly `vav-inworld` |
| Frontend browser CSP | `LIVEKIT_BROWSER_CONNECT_ORIGIN` | Optional exact self-hosted LiveKit `wss://` or `https://` origin; omit for LiveKit Cloud and never place credentials in it |
| API worker probe | `LIVEKIT_WORKER_HEALTH_URL` | Private HTTP origin and port of `livekit-agent`, with no path; on Railway use `http://${{livekit-agent.RAILWAY_PRIVATE_DOMAIN}}:${{livekit-agent.PORT}}`; set on API only and never expose publicly |
| Inworld | `INWORLD_API_KEY` | Optional platform fallback; prefer the encrypted workspace credential in VAV Settings |
| OpenAI | `OPENAI_API_KEY` | Optional platform fallback for direct OpenAI LLM routes. The worker first loads the encrypted tenant OpenAI credential from PostgreSQL and fails closed if that active credential cannot be decrypted; the key is never put in dispatch metadata, browser tokens, frontend variables, or logs. |
| Operations | `LIVEKIT_LOG_LEVEL=info`, `LIVEKIT_NUM_IDLE_PROCESSES=1`, `VAV_RELEASE_SHA` | Keep registration evidence, cap Railway prewarming until load-tested, and record the exact deployed commit |

The LiveKit project URL, key, and secret are platform-owned server variables on `api` and `livekit-agent`; tenants neither submit nor store copies. Tenant configuration contains only the e& SIP URI and LiveKit trunk/dispatch identifiers. `INTEGRATION_ENCRYPTION_KEY` must match across API, Celery and LiveKit; a mismatch makes saved provider route records and Inworld credentials unreadable and must be treated as a failed deployment.

### Deploy and verify registration

1. Commit the complete backend/frontend release and pass CI on a feature branch.
2. Merge it into the branch already tracked by Railway production. Wait for API `/ready`, Celery and frontend to report successful deployments.
3. Create `livekit-agent` only after that production commit contains `app.livekit_runtime.worker`; otherwise the service will boot an older image with no worker module.
4. Configure the service contract and variables above. Do not assign a public domain.
5. Confirm Railway process and LiveKit registration separately:

   ```text
   railway service status -s livekit-agent -e production
   railway logs -s livekit-agent -e production --lines 100 --filter "registered worker"
   ```

   The first command must report `SUCCESS`. The logs must include `registered worker` with `agent_name=vav-inworld`, a worker ID and the expected LiveKit region. A successful container without this registration record is not ready.
6. Confirm the Railway health check remains green after the registration log. A transient `200` during initial connection is not enough on its own; the health check, registration log and VAV route check are one release gate.
7. Set `LIVEKIT_WORKER_HEALTH_URL` on API to the private worker origin, then in VAV save the workspace Inworld credential and LiveKit route and run **Test readiness**. It must verify the exact project credentials, inbound trunk, optional outbound trunk, dispatch rule, assigned DIDs, plus live `/` and `/worker` health with the expected agent name.
8. Run one browser WebRTC test, one controlled inbound call and one controlled outbound call before assigning customer traffic. Confirm transcript, provider IDs, channel, direction, duration and cost components in VAV.

Do not use `railway up` from an uncommitted working tree for production. The three GitHub-backed application services and `livekit-agent` should all point to an auditable commit so rollback and release attribution remain deterministic.

## Provisioning order

1. Create one LiveKit project for this VAV deployment. Put `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `LIVEKIT_AGENT_NAME=vav-inworld` on the API and the always-on LiveKit worker. Never put them in `NEXT_PUBLIC_*` variables.
2. Connect the customer e& SIP trunk to LiveKit. Create the inbound trunk, optional outbound trunk, and a dispatch rule for the DID.
3. Configure the inbound dispatch rule to target agent name `vav-inworld` and explicitly include every assigned DID. Inbound routing resolves the only active VAV runtime matching the verified trunk ID plus called DID; it never trusts a static dispatch-rule `agent_id` or caller/SIP metadata. Outbound dispatch metadata is created server-side and remains call-scoped.
4. In VAV Settings, connect the direct Inworld key and record only the customer e& SIP URI plus returned LiveKit trunk/dispatch IDs. The shared LiveKit project credentials remain server-only environment variables.
5. Create an Inworld-voice agent, attach and approve its knowledge base, choose the LiveKit/Inworld runtime, assign the E.164 DID and save.
6. Run **Test readiness**. Activation remains disabled for missing searchable knowledge, credentials, exact project match, route IDs, assigned DID coverage, live worker health/name, number uniqueness or unavailable voice.
7. Activate only after the browser test plus real inbound and outbound test calls pass the acceptance matrix below.

## Pilot acceptance matrix

- At least 20 inbound and 20 outbound calls for each enabled language/accent (initially en-GB, ar-AE and hi-IN).
- At least 20 browser WebRTC sessions per enabled language/accent, with the
  same golden questions used for the phone pilot.
- 100% correct tenant and agent routing; no cross-agent knowledge result.
- At least 95% correct answers on a versioned golden-question set; zero invented prices, doctors, policies or medical claims.
- Barge-in, silence, voicemail, DTMF, transfer, hang-up and carrier rejection cases end with the correct call status and SIP identifier.
- p95 first response and end-of-turn latency are recorded and accepted against the customer scenario before broad rollout.
- VAV metered cost differs from provider invoice by no more than 5% for measurable usage; plan fees, taxes and e& charges are reconciled separately.
- Call recording stays off until consent, retention, access, residency and deletion requirements are approved.

## Failure and rollback

- A missing/invalid Inworld key, unavailable voice, mismatched LiveKit project, absent trunk/dispatch ID or unreadable knowledge source blocks readiness and disables activation.
- Inbound routing fails closed unless the verified trunk ID plus called DID resolves exactly one active LiveKit + Inworld runtime. Outbound routing fails closed unless VAV-created dispatch metadata contains both a valid `agent_id` and `call_id`.
- A Railway deployment is unhealthy when `/` returns `503`, the process repeatedly restarts, the `registered worker` record is absent, or the record names the wrong agent/project/region. Do not bypass these checks by removing the health path.
- Keep Sarvam/Twilio credentials, profiles and the previous production commit unchanged during the pilot.
- To roll back call traffic, first deactivate the LiveKit/Inworld profile, verify the prior Sarvam/Twilio profile is ready, and only then repoint the DID. Never repoint the DID to an untested route.
- After traffic has left the lane, stop or scale down `livekit-agent`, revert the release commit on the Railway-tracked branch, and wait for API, Celery and frontend health before closing the incident. This release adds no database migration, so rollback does not require destructive schema work.
- Retain encrypted LiveKit/Inworld credentials and route identifiers until rollback verification is complete. Deleting them early removes evidence and makes a controlled retry harder.
- Record the failed deployment ID, Git SHA, worker registration ID, last successful call ID and rollback time in the incident log.
- Do not mark the integration production-ready merely because credentials save successfully. A completed test call plus the acceptance matrix is the release gate.
