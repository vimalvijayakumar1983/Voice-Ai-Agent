# VAV Voice AI: World-Class Platform Blueprint

**Document status:** implementation blueprint  
**Baseline reviewed:** `codex/smallest-ai-platform` at `8c0aa8e56ea4ede4d20cac3e15d9b2c241911c48`  
**Assessment date:** 27 August 2026  
**Primary provider:** Smallest.ai Atoms and Waves  
**Target markets:** UAE first, India next, with WhatsApp-native customer journeys  

## 1. Executive decision

VAV Voice AI should be built as a complete voice-agent operating system, not as a thin screen over one provider. Smallest.ai should remain the first-class execution provider because the current product already uses its voice catalog, agent branches, web-call sessions, outbound calls, signed webhooks, transcripts, recordings, and post-call analytics. The VAV control plane must nevertheless own the canonical agent, workflow, policy, deployment, conversation, campaign, and billing records.

This creates two advantages:

1. VAV can exploit Smallest-native capabilities immediately, including Atoms branches and revisions, Waves voices, Playbooks, speech controls, web sessions, telephony, and provider analytics.
2. VAV is not limited by any one provider's language, channel, residency, price, or feature constraints. Unsupported capabilities can be hidden, rejected clearly, or routed through another approved adapter without changing the product model.

The current application is a credible foundation, but not yet a world-class product. Agent creation and editing are the strongest area. Workflows, integrations, settings, campaigns, compliance, conversation review, authentication, enterprise administration, and live operations need substantial completion. The target information architecture is the 14-module system in Section 4.

## 2. Evidence and benchmark

### 2.1 Current product evidence

The baseline includes:

- a multi-tenant FastAPI API, PostgreSQL models, Alembic migrations, Redis/Celery workers, and a Next.js console;
- email/password registration and login, JWT access/refresh token issuance, four coarse user roles, tenant-scoped queries, invitations, and API-key creation endpoints;
- local agent CRUD, five predefined templates, editable prompt/voice/language settings, a live Smallest voice catalog, cloned-voice discovery, 20 currently observed language codes, explicit provision/publish actions, and provider sync status;
- Smallest web-call session tokens and a browser voice playground;
- controlled outbound call initiation and signed Smallest lifecycle webhook ingestion;
- basic call records, recordings, transcripts, summaries, sentiment/disposition fields, campaigns, DNC/consent records, workflows, integrations, usage, billing plans, and aggregate analytics at API/data-model level;
- an operator console for overview, agents, playground, calls, campaigns, compliance, integrations, billing, settings, and workflows.

Important limitations are recorded in Section 12. A model or endpoint existing in the repository is not counted as a completed product module unless its user journey, security controls, tests, observability, and operational failure paths are also complete.

### 2.2 Market benchmark

The target capability set was benchmarked against official documentation for:

- [Smallest.ai Atoms](https://docs.smallest.ai/voice-agents/platform/create-agent/build-your-agent): branches/revisions, Playbooks, prompt scoring, granular speech controls, knowledge, tools, phone/SIP deployment, campaigns, post-call metrics, and per-turn observability;
- [ElevenLabs ElevenAgents](https://elevenlabs.io/docs/eleven-agents/overview): graph workflows, tools, testing, experiments, live monitoring, omnichannel deployment, privacy controls, and enterprise administration;
- [Bland AI](https://docs.bland.ai/tutorials/personas): Personas, Conversational Pathways, memory, campaigns, warm transfer, Testbed/evaluations, outcomes, guardrails, and dedicated infrastructure controls;
- [Synthflow](https://docs.synthflow.ai/getting-started): visual flow design, actions, workflows, cross-channel memory, automated simulations, campaigns, analytics, subaccounts, and white-label administration.

Provider voice and language counts change. The application must load the live catalog and capability metadata instead of treating marketing counts as contracts. At the assessment date, the integrated Smallest Waves catalog returned 234 voices and 20 language codes. This is the current provider result, not a promise that every human language is available.

### 2.3 Definition of “world-class”

The product qualifies as world-class only when it can reliably support the full lifecycle below:

1. Design an agent, voice, knowledge, tools, policy, and workflow.
2. Test it with deterministic and simulated scenarios.
3. Review and approve an immutable version.
4. Deploy by environment and channel with a controlled rollout.
5. Operate inbound, outbound, web, mobile, SIP, and WhatsApp journeys.
6. Monitor active calls, intervene safely, and recover from provider failures.
7. Review conversations with evidence, outcomes, QA, cost, and latency.
8. Improve through evaluations, experiments, regression gates, and rollback.
9. Govern consent, retention, access, audit, redaction, residency, and spend.
10. Integrate with business systems without exposing credentials or coupling workflows to one provider.

## 3. Product and architecture principles

1. **Smallest-native, provider-neutral.** Use Smallest features deeply through an adapter; never place provider-specific IDs or request shapes at the center of the product domain.
2. **One canonical source of truth.** VAV owns the draft, version, deployment, policy, and audit history. Provider state is a binding and a reconciled projection.
3. **Capabilities are discovered.** Language, voice, transfer, SIP, WhatsApp, streaming, residency, versioning, and tool support are described by provider capabilities and validated before deployment.
4. **Drafts do not change production.** Every production change follows draft, validation, evaluation, approval, immutable version, deployment, and observable rollout.
5. **Safety precedes reach.** Consent, DNC, calling windows, disclosure, identity verification, data minimization, and transfer policies are enforced centrally before calls or messages are scheduled.
6. **The event history is evidence.** Provider webhooks, tool executions, state changes, policy decisions, human interventions, and exports are immutable and traceable.
7. **Omnichannel context is shared deliberately.** Voice, WhatsApp, chat, SMS, and human support may share a customer timeline and approved memory, while channel-specific consent and retention rules remain separate.
8. **Failures are explicit.** The UI must distinguish empty data, permission denial, validation failure, provider failure, stale projection, and temporary unavailability.
9. **A modular monolith first.** Keep transactional business logic in clear FastAPI bounded contexts and Celery workers initially. Use an outbox and stable internal contracts so high-scale or security-sensitive services can be extracted later.
10. **No unverifiable claim.** The agent may only state that an action succeeded after a tool/provider response confirms it; the product may only advertise a language or feature when the selected deployment supports it.

## 4. Target 14-module information architecture

| # | Module | Primary objects | Main user jobs |
|---:|---|---|---|
| 1 | Command Center | alerts, tasks, incidents, readiness, KPIs | Understand platform health, risk, performance, and required action |
| 2 | Agent Studio | agents, drafts, versions, deployments, voice profiles | Design, edit, approve, publish, roll back, and compare agents |
| 3 | Templates & Marketplace | system templates, workspace templates, vertical packs | Start from governed reusable agents, workflows, policies, and evaluations |
| 4 | Knowledge & Memory | knowledge bases, sources, documents, indexes, memories | Ground answers, refresh content, inspect citations, and retain allowed context |
| 5 | Tools & Integrations | tools, connectors, credentials, MCP servers, webhooks | Let agents safely read or act in CRM, ERP, calendar, help desk, and custom APIs |
| 6 | Workflows & Playbooks | graphs, nodes, edges, subagents, procedures | Orchestrate deterministic and AI-led journeys with clear routing and fallbacks |
| 7 | Testing & Evaluations | test suites, personas, simulations, rubrics, runs | Prove task success, safety, audio quality, and regression readiness before release |
| 8 | Channels, Numbers & Routing | phone numbers, SIP trunks, widgets, apps, routes | Deploy agents to PSTN, SIP, web, mobile, chat, SMS, and WhatsApp |
| 9 | Live Operations | active sessions, queues, interventions, incidents | Monitor live conversations, transfer, take over, throttle, or stop safely |
| 10 | Conversations & Quality | recordings, transcripts, timelines, outcomes, reviews | Search evidence, review quality, assign coaching, and correct outcomes |
| 11 | Campaigns, Contacts & Audiences | contacts, segments, consent, campaigns, recipients | Import, suppress, schedule, execute, and optimize high-volume outreach |
| 12 | Analytics & Intelligence | metrics, funnels, cohorts, topics, costs, exports | Measure business outcome, quality, latency, reliability, and unit economics |
| 13 | Compliance, Security & Audit | policies, consent, DNC, retention, audit, incidents | Govern access and communications and produce defensible evidence |
| 14 | Workspace, Team, Billing & Developer Admin | tenants, environments, users, roles, keys, plans, usage | Operate the SaaS workspace, enterprise identity, limits, APIs, and commercial model |

### 4.1 Recommended navigation

- **Home:** Command Center
- **Build:** Agent Studio; Templates; Knowledge & Memory; Tools & Integrations; Workflows & Playbooks; Testing & Evaluations
- **Operate:** Channels & Routing; Live Operations; Conversations & Quality; Campaigns & Audiences
- **Insights:** Analytics & Intelligence
- **Govern:** Compliance, Security & Audit
- **Manage:** Workspace, Team, Billing & Developer Admin

Global search must return agents, versions, conversations, contacts, campaigns, tools, workflows, incidents, and audit events, with permission-aware results. Notifications must link to a specific failed deployment, policy block, provider incident, evaluation regression, budget threshold, campaign event, or review assignment.

## 5. Module requirements

### 5.1 Command Center

**Required experience**

- Role-specific views for owner, administrator, builder, operator, analyst, reviewer, developer, and billing administrator.
- Time range, environment, agent, provider, channel, campaign, team, language, country, and outcome filters.
- KPIs: conversations, connected rate, task success, transfer rate, containment, average handling time, customer satisfaction, latency, errors, concurrency, minutes, total cost, and cost per successful outcome.
- Readiness cards for untested drafts, drifted provider bindings, unacknowledged incidents, expiring credentials, failed webhooks, consent violations, DNC suppressions, budget thresholds, and unhealthy knowledge sources.
- Drill-down from every metric to the filtered conversations or deployment events that produced it.
- Saved views, scheduled reports, anomaly alerts, and alert ownership/acknowledgement.

**Acceptance criteria**

- No metric is presented without its source, aggregation rule, timezone, and freshness timestamp.
- Empty, loading, partial, delayed, and failed states are visually distinct.
- Dashboard totals reconcile with conversation-level data for the same filters.

### 5.2 Agent Studio

Each agent opens a dedicated workspace with these tabs:

1. **Overview:** status, owner, purpose, environments, channels, last version, health, cost, and recent changes.
2. **Prompt & Persona:** system prompt, goals, boundaries, tone, first message, fallback, disclosures, variables, pronunciation, and generated prompt review.
3. **Voice & Languages:** live catalog, preview, cloned/custom voices, primary/additional languages, language-specific first messages and voices, speed, similarity/consistency where supported, background sound, denoising, endpointing, VAD, barge-in, silence, and unclear-speech behavior.
4. **Conversation Behavior:** maximum duration, idle reminders, interruption rules, voicemail handling, DTMF, identity verification, transfer policy, business hours, fallback, and termination conditions.
5. **Knowledge:** attached sources, indexing state, retrieval settings, citations, freshness, and evaluation coverage.
6. **Tools:** system, HTTP, client, MCP, and connector tools; authentication; inputs; approvals; timeouts; retries; and test console.
7. **Workflow / Playbook:** entry workflow, specialist subagents, routing, shared variables, and failure path.
8. **Deployment:** environments, phone numbers, widgets, WhatsApp channel, traffic allocation, provider binding, and configuration diff.
9. **Test & Evaluate:** test cases, simulations, production regressions, rubric scores, and approval status.
10. **Versions:** branch/draft history, immutable versions, diff, label, author, approval, rollout, rollback, and provider reconciliation.
11. **Analytics:** outcome, quality, latency, cost, language, tool, route, and version performance.
12. **Settings:** ownership, tags, archive, duplicate, export/import, and deletion policy.

**Lifecycle**

`draft -> validating -> evaluation_required -> awaiting_approval -> approved -> deploying -> deployed -> superseded | rolled_back | failed`

**Requirements**

- Autosave with optimistic concurrency and a visible conflict-resolution experience.
- Immutable versions containing the complete resolved configuration and referenced component versions.
- Environment promotion from development to staging to production; no production edit in place.
- Percentage or rule-based rollout where the provider/channel supports it; otherwise VAV assigns traffic before session creation.
- One-click rollback to a previously healthy deployment without modifying its immutable version.
- Provider drift detection and a choice to import, overwrite, or ignore external changes.
- Capability validation before save and again before deployment.
- Clear ownership of secrets: agent configuration contains secret references, never secret values.

### 5.3 Templates & Marketplace

**Template types**

- agent, prompt, workflow, tool bundle, policy profile, evaluation suite, campaign, and full vertical solution pack;
- built-in VAV templates and private workspace templates;
- future verified partner templates, disabled until review/signing controls exist.

**Initial vertical packs**

- receptionist and routing;
- customer support and complaint escalation;
- lead qualification and meeting booking;
- appointment booking/rescheduling/cancellation;
- payment reminder and collections;
- order status and delivery coordination;
- healthcare appointment administration, excluding clinical advice by default;
- automotive service booking/follow-up;
- property enquiry qualification;
- restaurant reservation and event confirmation.

**Requirements**

- Semantic version, owner, supported channels/languages/providers, dependencies, required credentials, required policy profile, and release notes.
- “Use template” creates editable copies; upstream updates never silently change deployed agents.
- Template validation includes test suites and sample data with no real PII.
- Workspace admins can approve who may publish a shared template.

### 5.4 Knowledge & Memory

**Sources**

- text, PDF, DOCX, Markdown, HTML, CSV, approved website crawl, sitemap, authenticated connector, CRM/help-desk records, and API-fed records;
- scheduled refresh, incremental change detection, source ownership, and last successful sync;
- antivirus/content-type validation and document-level access rules.

**Retrieval**

- parse -> normalize -> classify -> redact -> chunk -> embed -> index -> evaluate -> publish;
- hybrid keyword/vector retrieval, metadata filters, reranking, locale-aware search, citation capture, and answerability threshold;
- development and production indexes so unreviewed content is not exposed to production agents;
- retrieval trace showing query, selected chunks, score, source revision, latency, and token/cost contribution.

**Memory**

- explicit memory profiles: none, session only, contact history, approved facts, open tasks, and organization-defined fields;
- customer identity resolution across phone, WhatsApp, CRM contact, and web user IDs;
- provenance, confidence, expiry, consent, correction, deletion, and per-channel visibility for every retained fact;
- sensitive classes such as health, finance, authentication, children, and payment data disabled unless a reviewed use case explicitly enables them.

**Acceptance criteria**

- Every grounded answer can identify the source revision used.
- Deleted/expired content is absent from production retrieval within the documented deletion SLA.
- A retrieval regression suite passes before a new index becomes active.

### 5.5 Tools & Integrations

**Tool types**

- system tools: end call, language switch, DTMF, voicemail decision, transfer, state update, knowledge search;
- HTTP/API tools with JSON Schema inputs/outputs and templated mappings;
- client tools for browser/mobile experiences;
- managed connector actions for CRM, ERP, calendar, help desk, payments, messaging, and identity;
- MCP servers with an allow-listed tool inventory;
- sandboxed code only after an isolated runtime and security review are available.

**Initial connectors**

- VAV CRM and WhatsApp CRM;
- FEPY Magento/OMS order and customer context;
- Microsoft Dynamics, Salesforce, HubSpot, Zoho, Zendesk, Freshdesk, Intercom;
- Microsoft 365/Google Calendar and Cal.com;
- Twilio/SIP carriers, Meta WhatsApp Business Platform, email and SMS;
- Stripe or approved UAE payment-link provider; no raw card handling by the voice agent;
- generic signed webhooks and REST/OpenAPI import.

**Runtime requirements**

- OAuth or vaulted credential reference, tenant/environment scope, least privilege, rotation, and revocation.
- JSON Schema validation, idempotency key, timeout, retry policy, circuit breaker, rate limit, and response size limit.
- Read-only versus mutating classification. Mutating/high-risk tools may require deterministic confirmation or human approval.
- Tool result is recorded with redacted inputs/outputs, duration, status, attempt, provider request ID, and policy decision.
- Test console uses synthetic data and cannot call production credentials without an explicit approved mode.
- Webhook destinations support signature rotation, replay protection, delivery logs, manual replay, and dead-letter handling.

### 5.6 Workflows & Playbooks

**Node library**

- start, message, AI conversation, specialist agent, knowledge search, collect/validate field, tool call, condition, switch, loop with cap, wait, business-hours check, authentication, consent, DTMF, webhook, transfer, WhatsApp/SMS follow-up, human approval, set state, subflow, outcome, and end.

**Engine requirements**

- Directed graph with explicit edges, versioned node schemas, entry node, terminal paths, and validation for unreachable nodes/cycles.
- Deterministic conditions for compliance and transactions; model-led routing only within approved bounds.
- Shared typed state plus node-scoped variables, provenance, defaults, and redaction classification.
- Reusable subflows and specialist-agent routing with recursion/depth limits.
- Compensation/fallback paths for tool timeout, transfer failure, provider outage, and invalid input.
- Visual builder, JSON representation, diff, import/export, simulator, start-at-node tests, and trace replay.
- Atoms Playbooks adapter where supported; VAV engine remains canonical and records provider-specific lowering warnings.

**Acceptance criteria**

- Publishing is blocked when any terminal path lacks a safe end/fallback.
- The simulator can execute every deterministic branch with mocked tools.
- Production traces identify every node, transition reason, state mutation, and duration.

### 5.7 Testing & Evaluations

**Test modes**

- browser voice, text, real phone, web/mobile widget, and WhatsApp sandbox;
- deterministic next-response, tool-call, route, extraction, transfer, DTMF, disclosure, and end-condition tests;
- multi-turn persona simulations with accent, language, noise, interruption, silence, ambiguity, anger, prompt injection, and unavailable-tool variations;
- audio evaluation for intelligibility, clipping, pronunciation, speed, barge-in, latency, and language consistency;
- production-conversation replay with tools mocked and PII redacted;
- A/B or champion/challenger version evaluation.

**Scorecards**

- task completion, factual grounding, policy compliance, correct tool use, correct routing, identity verification, customer effort, tone, interruption handling, latency, audio quality, and cost;
- pass/fail critical criteria and weighted non-critical criteria;
- model judge results always retain rubric version, evidence spans, confidence, and reviewer override.

**Minimum production gate**

- 100% pass on critical safety, consent, disclosure, authentication, and destructive-action cases;
- at least 95% pass on the use case's designated must-pass functional suite;
- no unresolved severity-1 or severity-2 regression;
- latency and cost within the approved budget under load;
- human approval from the agent owner and policy owner for regulated/high-risk use cases.

Thresholds are release policies, not marketing claims; each tenant may adopt stricter requirements.

### 5.8 Channels, Numbers & Routing

**Channels**

- Smallest-managed phone numbers, imported numbers, SIP trunks/PBX/BYOC, inbound and outbound PSTN;
- web voice widget, web chat, JavaScript/React, iOS, Android, React Native, and Flutter integration;
- WhatsApp Business calling and messaging, voice notes, SMS, and approved email follow-up;
- future provider adapters exposed only after contract and quality certification.

**Inventory and routing**

- number ownership, country, capabilities, verification, emergency restrictions, caller ID, environment, provider binding, monthly cost, health, and assignment history;
- inbound route by number, business hours, language, contact segment, CRM state, IVR input, campaign, and weighted agent version;
- outbound number pool, geographic matching, reputation, per-number limits, suppression, and fallback;
- warm/cold transfer, queue, whisper, summary, hold audio, no-answer fallback, and return-to-agent behavior;
- failover must be policy-aware: never silently move a call to a provider or region the tenant has not approved.

**Capability rules**

- UI and API reject unsupported channel/language combinations before deployment.
- A language listed by one provider is not automatically considered validated for every accent, industry, or channel.
- Numbers and SIP changes require administrator permission and an audit event.

### 5.9 Live Operations

**Required console**

- Active calls/messages with agent, version, provider, channel, direction, language, contact, duration, latency, current workflow node, last tool, transfer state, and policy warnings.
- Permission-controlled listen, live transcript, supervisor whisper, cold/warm transfer, human takeover, return to AI, send message, and end session.
- Queue and concurrency visibility by tenant, agent, campaign, provider, and number.
- Emergency stop at campaign, agent, channel, number, tenant, and platform levels.
- Alerting for elevated error rate, latency, silence, stuck tool, failed transfer, provider degradation, spend spike, policy breach, and number reputation risk.
- Incident timeline with owner, acknowledgement, mitigation, resolution, and affected conversations.

**Technical requirements**

- Realtime gateway using authenticated WebSocket/SSE subscriptions with tenant and role enforcement.
- Intervention commands are idempotent, time-bounded, acknowledged by the provider, and fully audited.
- If monitoring is unsupported for a provider/channel, the action is not shown.

### 5.10 Conversations & Quality

**Conversation workspace**

- Search and filters across conversation ID, phone/contact, agent, version, campaign, channel, provider, date, language, status, disposition, outcome, sentiment, tags, reviewer, policy event, tool, latency, and cost.
- Recording player synchronized to diarized transcript, with speed, waveform, bookmarks, redacted segments, and download permission.
- Timeline of speech turns, interruptions, silence, workflow nodes, tools, transfers, provider events, policy decisions, and human interventions.
- Summary, structured extracted fields, outcome, confidence/reasoning, citations, action items, follow-up, cost breakdown, and component latency.
- Saved views, bulk tagging, reviewer assignment, comments, issue creation, export, and retention/legal-hold status.
- Correction flow for transcript, disposition, outcome, and extracted fields while preserving original values and audit history.

**Quality program**

- Sampling rules by risk, agent, version, outcome, low confidence, complaint, language, and random percentage.
- Reusable scorecards, calibration sessions, dual review, disagreement resolution, coaching, and trend reporting.
- Failed reviews can generate a redacted regression test linked to the corrective version.

### 5.11 Campaigns, Contacts & Audiences

**Campaign builder**

- Goal, agent/version, channel, audience, calling window, contact timezone, start/end, concurrency, rate, retry, voicemail, number pool, variables, success criteria, stop conditions, follow-up, cost estimate, test call, and approval.
- CSV/XLSX import with preview, encoding detection, field mapping, E.164 normalization, duplicate resolution, error rows, and reusable mappings.
- Contacts and segments from CRM/connector, with immutable source snapshot at launch.
- Suppression before scheduling and again immediately before each attempt: tenant DNC, regulatory DNC integration where applicable, revoked consent, invalid number, frequency cap, quiet hours, prior success, complaint, and custom exclusion.
- Recipient state, attempt history, next eligible time, provider call ID, outcome, spend, and reason for skip/failure.
- Pause, resume, cancel, emergency stop, approval withdrawal, and safe retry.
- Number pools, rate shaping, provider concurrency, queue fairness, and reputation protection.

**State model**

`draft -> validating -> awaiting_approval -> scheduled -> running <-> paused -> completing -> completed | cancelled | blocked | failed`

Recipient state:

`pending -> suppressed | queued -> initiating -> connected -> succeeded | retry_eligible | exhausted | failed | cancelled`

**Critical correction to the current system**

Campaign execution must resolve the deployed provider/channel from the selected agent version. The current worker is Twilio-specific even when the main agent flow is Smallest-native. No campaign should launch until this mismatch is removed and DNC/consent/calling-window checks are transactional with reservation of the attempt.

### 5.12 Analytics & Intelligence

**Metric families**

- volume and connection: attempts, connections, answer rate, abandon, voicemail, completion;
- business outcome: booking, qualified lead, resolved issue, payment commitment, order completion, transfer result, conversion value;
- experience: containment, transfer, repeat contact, customer effort, sentiment, CSAT, interruptions, silence;
- quality: evaluation score, policy violation, hallucination/grounding, tool correctness, reviewer score;
- performance: end-of-turn to first audio, ASR, orchestration, LLM, tool, TTS, transfer, webhook, post-processing latency;
- economics: provider cost, telephony, ASR/LLM/TTS, tool cost, storage, cost/minute, cost/conversation, cost/successful outcome, margin;
- reliability: provider errors, tool failures, webhook lag, drift, retries, duplicate events, queue delay, concurrency saturation;
- content: intents, topics, objections, complaints, unmet demand, knowledge gaps, language/accent usage, workflow path.

**Requirements**

- Metric dictionary with formula, numerator/denominator, exclusions, owner, timezone, freshness, and version.
- Conversation-level lineage for every aggregate.
- Custom outcomes and extracted dimensions with backfill/versioning.
- Cohorts and comparison by agent version, experiment, channel, provider, campaign, language, region, time, and contact segment.
- CSV/JSON export, scheduled delivery, warehouse destination, and governed BI access.
- Anomaly detection creates an inspectable alert; it never changes a deployment automatically without an approved policy.

### 5.13 Compliance, Security & Audit

**Policy engine**

- Policy profiles by tenant, use case, country/region, channel, direction, contact type, data class, and environment.
- Enforce consent purpose/status/evidence, DNC, frequency cap, permitted hours, recording disclosure, AI disclosure, identity verification, vulnerable-customer escalation, payment-data restrictions, retention, and export rules.
- Policies return `allow`, `deny`, or `require_approval`, plus a stable reason code and evidence.
- Rules are versioned and effective-dated; a campaign captures the policy version used for each decision.

**Security and evidence**

- Fine-grained RBAC and resource scope, SSO/SCIM/MFA for enterprise, break-glass workflow, session/device management, and service accounts.
- Immutable audit stream for authentication, data access, configuration, approvals, deployments, interventions, exports, credential actions, and deletions.
- Consent ledger, DNC registry, data-subject request workflow, legal hold, retention jobs, redaction, and deletion certificates.
- Guardrails for prompt injection, forbidden content, manipulation, PII leakage, unsafe advice, unsupported claims, tool authorization, and disclosure.
- Incident and exception register with expiry and approval; no permanent silent bypass.

### 5.14 Workspace, Team, Billing & Developer Admin

**Workspace and enterprise administration**

- Organization -> workspace/subaccount -> environment hierarchy.
- Custom roles with resource/action scopes, groups, invitations, approval policies, ownership, and access reviews.
- Development/staging/production environment isolation for credentials, provider bindings, data, limits, and deployments.
- Custom domain, branding, locale, timezone, currency, support contacts, and white-label controls for authorized plans.

**Developer platform**

- API keys/service accounts with scopes, environment, expiry, IP restrictions, rotation, last use, and revocation.
- OAuth apps, signed webhook endpoints, event subscriptions, replay, logs, rate limits, OpenAPI docs, SDKs, examples, and an API playground.
- Sandbox tenant with synthetic contacts and test numbers.

**Billing and usage**

- Plans, trials, entitlements, agent/channel/concurrency limits, included units, metered usage ledger, provider cost, markup, credits, tax, invoices, payment methods, and overage policy.
- Budget by workspace, agent, campaign, provider, and month; threshold notifications and configurable hard stop.
- UAE AED and VAT-ready invoices; India INR/GST support should be added only with local tax review.
- Usage records must be idempotent and reconcilable to provider invoices and conversation evidence.

## 6. Canonical data model

### 6.1 Cross-cutting rules

- Use UUID primary keys and UTC timestamps; retain original provider timestamps and offsets.
- Every tenant-owned record contains `tenant_id`; environment-bound records also contain `environment_id`.
- Platform catalogs are explicitly separated from tenant data.
- Provider identifiers use `(provider, external_type, external_id)` unique bindings rather than columns added to domain records.
- Secrets are references to a secret manager, never JSON values in application tables or API responses.
- Mutable records use `version`/ETag optimistic concurrency.
- Immutable records include content hash, actor, source, and correlation ID.
- High-volume events, transcript segments, and usage ledgers are time-partitioned.
- Soft-delete is not a substitute for retention/deletion; deletion jobs and legal holds are explicit.

### 6.2 Core entities

| Bounded context | Required entities |
|---|---|
| Identity | `organizations`, `workspaces`, `environments`, `users`, `groups`, `roles`, `permissions`, `memberships`, `service_accounts`, `api_keys`, `sessions` |
| Agent lifecycle | `agents`, `agent_drafts`, `agent_versions`, `agent_deployments`, `deployment_traffic_rules`, `provider_bindings`, `provider_snapshots`, `sync_operations`, `approvals` |
| Voice/catalog | `provider_capabilities`, `voice_catalog_snapshots`, `voice_profiles`, `pronunciation_dictionaries`, `language_profiles` |
| Knowledge/memory | `knowledge_bases`, `knowledge_sources`, `documents`, `document_revisions`, `chunks`, `index_releases`, `sync_runs`, `memory_profiles`, `contact_memories` |
| Tools/integrations | `tool_definitions`, `tool_versions`, `integration_connections`, `credential_references`, `tool_executions`, `webhook_endpoints`, `webhook_deliveries`, `connector_sync_runs` |
| Workflows | `workflows`, `workflow_versions`, `workflow_nodes`, `workflow_edges`, `workflow_runs`, `node_runs`, `state_mutations` |
| Channels | `channels`, `phone_numbers`, `sip_trunks`, `routing_rules`, `number_assignments`, `transfer_destinations`, `widgets`, `whatsapp_accounts` |
| Conversations | `conversations`, `participants`, `conversation_events`, `media_artifacts`, `transcript_segments`, `summaries`, `outcome_definitions`, `outcome_results`, `extracted_fields`, `interventions` |
| Contacts/campaigns | `contacts`, `contact_identities`, `consent_records`, `dnc_entries`, `audiences`, `audience_members`, `campaigns`, `campaign_recipients`, `contact_attempts`, `suppression_decisions` |
| Evaluation/QA | `test_suites`, `test_cases`, `simulation_personas`, `evaluation_rubrics`, `test_runs`, `test_results`, `qa_scorecards`, `qa_reviews`, `review_assignments`, `quality_issues` |
| Analytics/usage | `metric_definitions`, `metric_facts`, `usage_ledger`, `cost_ledger`, `budgets`, `alerts`, `saved_views`, `report_schedules` |
| Governance | `policy_profiles`, `policy_versions`, `policy_decisions`, `audit_events`, `retention_rules`, `legal_holds`, `deletion_jobs`, `security_incidents`, `exceptions` |
| Commercial | `plans`, `entitlements`, `subscriptions`, `invoices`, `payments`, `credits`, `provider_invoice_reconciliations` |

### 6.3 Key immutable snapshots

An `agent_version` must resolve and hash:

- prompt/persona and all first/fallback/disclosure messages;
- voice/language/audio configuration;
- workflow version;
- tool versions and permission policy;
- knowledge index release;
- memory and policy profile versions;
- outcome/extraction definitions;
- channel-specific overrides;
- provider adapter version and lowered provider configuration.

This prevents a deployed version from changing because a linked tool, workflow, prompt, or knowledge source was edited later.

### 6.4 Event taxonomy

Publish domain events through a transactional outbox. Initial event names:

- `agent.draft.updated`, `agent.version.created`, `agent.version.approved`;
- `deployment.requested`, `deployment.succeeded`, `deployment.failed`, `deployment.rolled_back`, `provider.drift.detected`;
- `conversation.queued`, `conversation.started`, `conversation.connected`, `conversation.ended`, `conversation.processing_completed`;
- `transcript.segment.created`, `tool.execution.completed`, `transfer.completed`, `intervention.executed`;
- `outcome.computed`, `qa.review.completed`, `evaluation.regression.detected`;
- `campaign.started`, `campaign.recipient.suppressed`, `campaign.completed`;
- `consent.granted`, `consent.revoked`, `dnc.added`, `policy.decision.denied`;
- `usage.recorded`, `budget.threshold_reached`, `webhook.delivery.failed`.

Every event includes `event_id`, `event_version`, `occurred_at`, `tenant_id`, `environment_id`, `actor`, `correlation_id`, `causation_id`, `resource`, and a redacted payload. Consumers must be idempotent.

## 7. Service architecture

### 7.1 Initial deployment shape

Keep three deployable units until load or isolation requires extraction:

1. **API/control plane:** FastAPI modules, authentication, CRUD, validation, policy checks, deployment orchestration, and query APIs.
2. **Worker/data plane:** Celery queues for provider operations, campaigns, knowledge ingestion, post-call processing, evaluations, exports, retention, reconciliation, and notifications.
3. **Realtime gateway:** may start inside the API for web-call/session events, but should be separable for live operations, WebSocket/SSE fan-out, and intervention commands.

Use PostgreSQL as the transactional source, Redis for queues/ephemeral coordination, S3-compatible object storage for recordings/documents/exports, `pgvector` initially for retrieval, a managed secret store for credentials, and OpenTelemetry for traces/metrics/log correlation. Add ClickHouse or a governed warehouse when event volume or analytical query isolation justifies it.

### 7.2 Bounded services and responsibilities

| Service/module | Responsibilities |
|---|---|
| Identity & Tenant | sessions, SSO/SCIM, membership, roles, resource scope, environment context |
| Agent & Release | drafts, component resolution, validation, versions, approvals, deployments, rollback |
| Provider Registry | adapter registration, capability discovery, catalog snapshots, provider health, routing eligibility |
| Knowledge | source sync, parsing, redaction, indexing, retrieval, citations, deletion |
| Tool Runtime | schema validation, authorization, credential resolution, execution, retries, logs |
| Workflow Engine | graph validation, state, node execution, subflows, trace, compensation |
| Channel & Routing | numbers/SIP/widgets/WhatsApp, inbound/outbound route resolution, transfer |
| Conversation Ingest | webhook inbox, media/transcript/event normalization, idempotent state machine |
| Live Operations | active-session state, realtime subscriptions, supervisor commands, incidents |
| Campaign Orchestrator | audiences, suppression, scheduling, concurrency, provider dispatch, retry |
| Intelligence & QA | post-call extraction, outcomes, summaries, evaluations, review queues |
| Analytics & Metering | metric facts, latency, usage/cost ledger, budgets, exports, reconciliation |
| Compliance | policy evaluation, consent/DNC, retention, redaction, legal hold, audit |
| Integration Hub | OAuth/connectors, sync, external webhooks, delivery/replay/dead letters |

### 7.3 Reliability patterns

- Transactional outbox for domain events; inbox table for provider/external events.
- Fast webhook acknowledgement after signature/replay validation and durable enqueue; heavy processing is asynchronous.
- Idempotency keys for all mutating provider/tool/campaign operations.
- Exponential retry with jitter only for retry-safe errors; dead-letter queues and operator replay.
- Circuit breaker and provider health score; no automatic cross-provider failover without data/residency/policy approval.
- Scheduled reconciliation for deployments, active calls, usage, numbers, and campaign recipients.
- Per-tenant/provider concurrency semaphores and queue fairness.
- Backpressure and emergency stop independent of provider availability.

## 8. Smallest-native/provider-neutral design

### 8.1 Provider contract

Define typed interfaces rather than calling `SmallestAIClient` from endpoint or campaign code:

```text
ProviderRegistry
  get_capabilities(context) -> ProviderCapabilities
  get_voice_catalog(filters) -> VoiceCatalogSnapshot

AgentDeploymentProvider
  validate(resolved_version) -> ValidationResult
  create_binding(version, environment) -> ProviderBinding
  deploy(binding, resolved_version, idempotency_key) -> ProviderDeployment
  get_status(binding) -> ProviderSnapshot
  rollback(binding, target_version) -> ProviderDeployment

ConversationProvider
  create_web_session(deployment, variables) -> SessionToken
  start_outbound(deployment, destination, context) -> ExternalConversation
  transfer(conversation, destination, mode) -> ProviderOperation
  terminate(conversation) -> ProviderOperation

ChannelProvider
  list_numbers() / assign_number() / configure_sip() / configure_widget()

RealtimeProvider (optional capability)
  subscribe(conversation) / whisper() / takeover() / return_to_agent()
```

`ProviderCapabilities` is versioned and includes:

- voices and languages by channel/model;
- multi-language and mid-call switching;
- custom/cloned voice and preview;
- speech/audio controls;
- branches, immutable versions, traffic split, rollback;
- server/client/MCP/system tools;
- knowledge, Playbooks/workflows, pre-call enrichment;
- PSTN, SIP, web, mobile, chat, WhatsApp, SMS;
- cold/warm transfer, DTMF, voicemail, live monitor/takeover;
- recordings, transcripts, analytics, latency events;
- regional endpoints, retention/redaction controls, concurrency, and limits.

The UI renders and validates against these capabilities. Unsupported options include a reason and an approved alternative; they are never silently ignored.

### 8.2 Smallest adapter mapping

| VAV concept | Smallest-native mapping |
|---|---|
| Provider binding | Atoms agent ID and default/live branch IDs |
| Draft | Atoms branch draft |
| Version | Atoms immutable revision plus VAV full resolved snapshot |
| Deploy | Draft update, publish/security scan, then activate approved revision/branch |
| Voice catalog | Waves `lightning-v3.1` live voice catalog plus eligible cloned voices |
| Language profile | Atoms default/supported language contract; provider constraints validated |
| Web test | Short-lived register-call token; API key remains server-side |
| Outbound call | Atoms outbound conversation API using deployment and variables |
| Lifecycle ingest | Signed pre-conversation, post-conversation, and analytics-completed webhooks |
| Workflow | Lower VAV graph to Atoms Playbooks where supported; retain VAV trace/schema |
| Tools/knowledge | Atoms tools, pre-call APIs, integrations, and knowledge bindings where supported |
| Phone/SIP/channel | Atoms managed/imported number and SIP capabilities after adapter completion |

The adapter must not treat a successful HTTP response as a completed deployment when the provider reports scanning/publishing asynchronously. Store the operation, poll or consume status, and mark deployment active only after confirmation.

### 8.3 Drift and recovery

- Store redacted provider snapshots and content hashes after every successful sync.
- Reconcile on schedule and before publishing a new version.
- Classify drift as benign metadata, importable configuration, incompatible external change, or missing remote object.
- Never create a duplicate remote agent after a partial failure. Persist the binding as soon as the remote ID exists and resume idempotently.
- Keep raw provider payloads encrypted/retained only as long as operationally necessary; expose normalized errors to users and retain provider request IDs for support.

### 8.4 Migration of current provider coupling

1. Introduce `provider_bindings`, `sync_operations`, and a provider registry.
2. Backfill existing `provider_agent_id`, branch, revision, `provider_config`, and sync status from `agents`.
3. Route manual calls and campaign calls through the same `ConversationProvider` contract.
4. Move provider state out of the canonical agent table after compatibility reads are removed.
5. Add deployment reconciliation before introducing another provider.

## 9. UAE, India, and WhatsApp differentiation

### 9.1 Product position

VAV should win through region-ready operations and business integration, not merely by offering another prompt editor. The differentiated experience is a multilingual agent that understands the customer across a phone call and WhatsApp thread, can take approved actions in VAV CRM/FEPY/enterprise systems, transfers to a human with context, and is governed for the contact's jurisdiction.

### 9.2 UAE pack

- English and Arabic-first interface/content support, with Hindi, Malayalam, Tamil, Urdu, Bengali, and other workforce/customer languages based on live provider capability.
- Do not advertise Arabic or Urdu voice support through Smallest unless the selected live catalog/channel actually supports and passes VAV accent/quality evaluations. Use an approved alternative provider adapter when required.
- Gulf Arabic and UAE English evaluation personas, local names/addresses, emirate/location pronunciation dictionaries, code-switching tests, and right-to-left transcript/UI support.
- `Asia/Dubai`, emirate/business-unit calendars, local business hours, and tenant-specific calling windows.
- UAE number/SIP carrier onboarding, approved caller identity, number reputation monitoring, and regional hosting option.
- Counsel-configured UAE privacy, telemarketing, DNC, recording, disclosure, and retention policy pack. Regulations and carrier rules are externally versioned dependencies and must be revalidated before launch; the product must not hard-code legal assumptions.
- AED budgets, provider cost, VAT-ready billing fields, and Arabic/English invoices.
- Vertical templates for Al Zaabi Group use cases: building-materials enquiry/quotation follow-up, FEPY order/service, healthcare appointment administration, automotive booking, property leads, and restaurant reservations.

### 9.3 India pack

- India language profile driven by provider capability. The current Smallest catalog covers several major Indian languages but not every Indian language; unsupported languages require another certified adapter.
- `Asia/Kolkata`, state/region-aware language preferences, Indian number formatting, transliteration, local-name/address pronunciation, and code-switching evaluation.
- Consent/DNC/calling-window and telecom registration policy pack configured from current legal/carrier requirements and reviewed locally before production. Do not encode regulatory identifiers or schedules without a maintained source and effective date.
- INR/GST commercial support after finance/tax review, India regional storage option where required, and carrier/SIP integration.
- Scale controls for large audiences: timezone-safe scheduling, regional number pools, per-language agent versions, provider rate shaping, and cost ceilings.

### 9.4 WhatsApp-native journeys

- Integrate Meta WhatsApp Business Platform through a channel adapter that reports messaging, voice calling, template, media, and region/account capabilities.
- Resolve the same customer across phone number, WhatsApp user, CRM contact, and web account while respecting purpose-specific consent.
- Share approved summary, facts, open items, and workflow state across calls, messages, and voice notes; do not blindly copy full transcripts between channels.
- Support inbound message -> AI triage -> voice call offer -> WhatsApp call -> human handoff -> written summary and next action.
- Support phone call -> consented WhatsApp quotation/payment link/appointment confirmation -> reply capture -> workflow continuation.
- Human handoff includes identity, intent, summary, language, verified fields, actions already attempted, current workflow node, and recommended next step.
- Campaign rules distinguish service, authentication, utility, and marketing journeys using current Meta/account eligibility. The adapter blocks unavailable or unapproved actions rather than trying an undocumented fallback.
- Unified reporting compares phone, WhatsApp calls, messages, voice notes, and mixed-channel resolution.

## 10. Security, privacy, compliance, and safety requirements

### 10.1 Production-blocking issues from the baseline

The following are release blockers, not future enhancements:

- Browser access/refresh tokens must not remain as long-lived `localStorage` credentials. Use secure, `HttpOnly`, `SameSite`, rotated session cookies or an equivalent reviewed BFF pattern.
- Implement token refresh, logout, session revocation, route protection, password recovery, email verification/invite acceptance, and brute-force/rate-limit controls.
- Remove hard-coded user/workspace identity from the UI and enforce role/permission checks in both API and interface.
- Replace plaintext integration credential fields in `Integration.config` with secret-manager references and redacted API responses.
- API keys created by the system must have scopes, expiry, rotation/revocation and actual authenticated use; storing only a password-style hash requires a lookup-safe key identifier.
- Validate signed provider callbacks with timestamp/replay protection where the provider supports it; persist an idempotent webhook inbox before processing.
- Enforce DNC, consent, calling window, frequency, and spend policy before every outbound attempt, including campaign retries.
- Complete audit events for authentication, credential, policy, version, deployment, campaign, export, deletion, and live-intervention actions.

### 10.2 Control requirements

| Area | Required controls |
|---|---|
| Authentication | MFA, SSO/OIDC/SAML for enterprise, secure recovery, session/device view, revocation, rate limiting, breached-password controls |
| Authorization | fine-grained action/resource RBAC, tenant and environment scope, row-level tests, least privilege, separation of duties, approval workflow |
| Tenant isolation | tenant key on all records, query enforcement, storage prefixes, cache/queue namespaces, cross-tenant security tests, enterprise dedicated option |
| Secrets | managed vault/KMS, envelope encryption, no browser/provider key exposure, redaction, rotation, access audit, short-lived credentials where possible |
| Data protection | TLS in transit, encryption at rest, field-level encryption for sensitive identifiers, backup encryption, controlled exports and signed URLs |
| Media/PII | recording default per policy, consent/disclosure, transcript/audio redaction, download permission, watermark/audit, retention/deletion/legal hold |
| API/webhooks | schemas, rate limits, idempotency, HMAC/signature, timestamp/replay defense, egress allow-list, SSRF defense, payload limits, dead-letter/replay |
| Tool safety | schema validation, allow-listed hosts/methods, scoped credentials, confirmation/approval for high-risk actions, sandbox, output sanitization |
| AI safety | prompt-injection defense, content/policy guardrails, grounding, identity gates, no secret disclosure, deterministic transactional confirmation |
| Telephony abuse | verified destinations/use case, velocity and spend limits, anomaly detection, high-risk country controls, caller-ID policy, emergency stop |
| SDLC | protected branches, reviews, secret/dependency/container scanning, SBOM, signed builds, migration tests, SAST/DAST, penetration test |
| Operations | OpenTelemetry, immutable security logs, alerting/on-call, incident response, provider status, backup/restore drills, capacity tests |

### 10.3 Compliance posture

Build controls and evidence toward ISO 27001 and SOC 2 readiness. Support configurable privacy workflows for UAE PDPL, India DPDP, and GDPR where applicable. Add HIPAA, PCI, or other regulated modes only after the architecture, contracts, provider eligibility, and operating procedures are reviewed; never imply certification from feature presence.

Data processing inventory, subprocessor list, data-flow diagrams, retention schedule, DPIA/risk assessment, incident process, data-subject request workflow, and customer-facing privacy/security documentation are release deliverables.

## 11. Non-functional requirements and SLOs

| Quality | Launch target | Enterprise target |
|---|---:|---:|
| Control-plane availability | 99.9% monthly | 99.95% monthly |
| Authenticated read API latency | p95 < 300 ms excluding provider calls | p95 < 200 ms |
| Authenticated write API latency | p95 < 500 ms excluding async work | p95 < 350 ms |
| Webhook durable acknowledgement | p95 < 250 ms | p95 < 150 ms |
| Voice end-of-turn to first agent audio | p50 < 500 ms, p95 < 900 ms under certified provider/network conditions | same, with provider-specific SLO |
| Barge-in stop response | p95 < 250 ms where provider supports it | p95 < 200 ms |
| Analytics freshness | < 5 minutes for operational dashboards | < 1 minute for live operations |
| Recovery point objective | <= 5 minutes | <= 1 minute where contracted |
| Recovery time objective | <= 60 minutes | <= 30 minutes where contracted |
| Accessibility | WCAG 2.2 AA | WCAG 2.2 AA |

Additional requirements:

- Every voice latency trace separates ASR, endpointing, orchestration, model first token, tool, TTS first byte, network, and playback.
- At-least-once events and webhook deliveries are deduplicated; conversation state never regresses because events arrived out of order.
- Load tests cover tenant limits, provider quotas, 2x expected peak, campaign bursts, and graceful shedding.
- Backups are restore-tested quarterly; production migrations use expand/migrate/contract and a rehearsed rollback/roll-forward plan.
- Web/mobile interfaces reflow to 320px, support keyboard and screen reader operation, reduced motion, named inputs, accessible dialogs, data alternatives to charts, and 44px touch targets.
- Product telemetry must never collect raw secrets, full payment data, or unredacted sensitive conversation content by default.

## 12. Current-versus-target coverage matrix

Status definitions: **Foundation** means a meaningful end-to-end portion works; **Partial** means APIs/data/UI exist but the user journey is incomplete; **Scaffold** means mostly data model, endpoint, or static UI; **Missing** means no credible product journey exists.

| # | Module | Current status and evidence | Target gap | Planned release |
|---:|---|---|---|---|
| 1 | Command Center | **Partial.** Overview shows basic calls, minutes, completion, handling time and readiness. | Drill-down, reliable loading/error states, filters, alert ownership, business outcomes, spend, latency, provider/knowledge health. | R1, expanded R4 |
| 2 | Agent Studio | **Foundation.** Create/edit, five templates, live 234-voice/20-language catalog at assessment time, primary/additional languages, Smallest provision/sync and browser test. | Dedicated studio tabs, full audio/behavior controls, knowledge/tools/routing, immutable VAV versions, approvals, environments, rollout, drift, rollback, agent analytics. | R1-R2 |
| 3 | Templates & Marketplace | **Partial.** Five built-in code templates can initialize editable agents. | Persistent/versioned workspace templates, policy/evaluation dependencies, vertical packs, import/export, approvals, verified partner model. | R2, expanded R5 |
| 4 | Knowledge & Memory | **Scaffold.** `KnowledgeBase` model and list/create API store text/URL/file-style content. No production ingestion/RAG lifecycle or console. | Connectors, parsing, index releases, citations/retrieval trace, refresh/deletion, evaluation, memory profiles, identity resolution, provider binding. | R2 |
| 5 | Tools & Integrations | **Scaffold.** Generic integration CRUD exists; console cards/configuration are effectively static. | Vaulted credentials, tool schemas/runtime, OAuth, MCP, managed connectors, test console, execution logs, approvals, webhook replay. | R2-R3 |
| 6 | Workflows & Playbooks | **Scaffold.** Workflow/node model and CRUD/list surface exist; creation/editing in console is blocked/inert and model is linear. | Versioned graph/edges, visual editor, full node library, subflows/subagents, simulator, traces, fallback validation, Atoms Playbooks lowering. | R2 |
| 7 | Testing & Evaluations | **Partial.** Browser voice playground with session variables, mute and live transcript. | Text/phone/WhatsApp tests, tool/debug/latency views, suites, simulation personas, audio and policy rubrics, production replay, release gates, experiments. | R2, expanded R4 |
| 8 | Channels, Numbers & Routing | **Partial.** Smallest web sessions and outbound calls exist; legacy Twilio code and basic inbound webhook exist. | Number inventory/assignment, Smallest phone/SIP adapter, channel deployments, routing, transfer, widget/mobile, WhatsApp, capability-aware failover. | R3 |
| 9 | Live Operations | **Missing.** No active-session console or supervisor intervention plane. | Realtime active calls, transcript/listen/whisper/takeover/transfer/end, queues, incidents, emergency stops, alerts. | R3-R4 |
| 10 | Conversations & Quality | **Partial.** Call list/data model; Smallest webhook stores transcript, recording URL, summary and analytics. Transcript/summary APIs exist but are not fully used in the console. | Synchronized player/transcript/timeline, search, structured outcomes, latency/cost, tags/export, reviewer assignment, scorecards, corrections, regression creation. | R1, expanded R4 |
| 11 | Campaigns, Contacts & Audiences | **Partial.** Campaign/contact APIs, Celery execution, basic create/start/pause UI, and DNC check exist. Worker is Twilio-specific and does not complete a safe Smallest-native lifecycle. | Import/mapping, segments, consent/frequency/windows, provider-neutral dispatch, retries/voicemail, scheduling, approval/test/cost, number pools, recipient traces and analytics. | R3 |
| 12 | Analytics & Intelligence | **Partial.** Overview/timeseries, basic agent/campaign aggregates, usage and provider analytics storage. Success rate is unfinished and dashboard depth is low. | Metric dictionary/lineage, outcomes, workflow/tool/language/version analysis, latency, cost reconciliation, cohorts/topics, custom metrics, warehouse export, alerts. | R4 |
| 13 | Compliance, Security & Audit | **Partial.** DNC and consent models/endpoints, tenant filters, coarse roles, HMAC Smallest webhook. | Central policy engine, per-attempt enforcement, audit ledger, retention/redaction/DSR/legal hold, SSO/MFA/fine RBAC, replay defense, guardrails, incidents and evidence. | R0-R4 |
| 14 | Workspace, Team, Billing & Developer Admin | **Scaffold.** Tenant/user/role models, invite/API-key endpoints, billing plans/subscriptions/usage; settings/integration/billing actions are largely read-only or inert. | Secure sessions, invite acceptance, service accounts/scoped keys, environments, entitlements, checkout/invoices/budgets, webhooks/developer logs, subaccounts, white-label. | R0-R5 |

## 13. Staged implementation roadmap

Durations are planning ranges for a dedicated cross-functional team and should be recalibrated after technical discovery. Streams may overlap only when release dependencies and ownership are explicit.

### R0 — Secure and trustworthy foundation (2-3 weeks)

**Scope**

- Secure session/BFF pattern, refresh rotation, logout/revocation, route guards, invite acceptance, password recovery, dynamic identity, and role-aware UI.
- Shared loading/error/toast/dialog/form/confirmation patterns; remove or label every inert control.
- Accessibility/mobile remediation for auth, shell, drawers, tables, charts, forms, and navigation.
- Secret-manager integration; migrate integration credentials out of JSON; scoped API-key model.
- Audit event/outbox/inbox foundations, webhook replay/idempotency, correlation IDs, rate limits, and baseline OpenTelemetry.
- CI gates for migrations, backend, frontend, E2E smoke, dependency/secret/container scans.

**Acceptance criteria**

- A user can register or accept an invite, sign in, refresh, log out, recover access, and be denied actions outside their role.
- No production credential appears in browser storage, logs, database JSON, or API responses.
- Cross-tenant automated tests cover every exposed resource family.
- Every visible control performs its labelled action, explains why it is unavailable, or is removed.
- Zero open severity-1/severity-2 security findings; documented disposition for lower findings.

**Release gate**

- Security lead, product owner, and operations owner approve the threat model, access model, incident/runbook baseline, backup restore evidence, and production secrets inventory.

### R1 — Complete the current core journey (4-6 weeks)

**Scope**

- Agent Studio shell, autosave/concurrency, behavior and voice settings, versions, deployment state, provider drift/reconciliation, and rollback.
- Conversation detail with recording, transcript, summary, analytics, events, loading/error states, and export controls.
- Complete overview drill-down and truthful readiness.
- Provider registry/binding abstraction and migration of existing Smallest fields.
- Provider-neutral manual outbound dispatch; live catalog snapshot/cache and capability validation.

**Acceptance criteria**

- Builder can create, edit, validate, approve, deploy, inspect, modify, redeploy, compare, and roll back an agent without provider-dashboard intervention.
- Partial provider failure resumes without duplicate remote agents or ambiguous local state.
- A completed call is searchable and shows its recording/transcript/summary/provider events with a shared correlation ID.
- Catalog outage uses a freshness-labelled cache and prevents invalid deployment.

**Release gate**

- Provider contract suite passes; migration is exercised on a production-like copy; rollback is demonstrated; browser and phone golden-path and failure-path tests pass.

### R2 — Build intelligence: knowledge, tools, workflows, tests (6-8 weeks)

**Scope**

- Knowledge ingestion/index releases/citations/retrieval traces and an initial no/session/contact-memory model.
- Tool registry/runtime with HTTP, system and initial VAV CRM/calendar connectors; vaulted credentials and execution logs.
- Versioned workflow graph, visual editor, simulator, subflows, policy nodes, traces, and Atoms Playbooks mapping where supported.
- Test suites, tool mocks, deterministic cases, persona simulation, rubric/evaluation engine, and deployment quality gate.
- Persistent/versioned workspace templates and initial UAE vertical packs.

**Acceptance criteria**

- Agent can answer from a versioned source with citation and retrieval trace; source deletion propagates within SLA.
- Mutating tool calls require validated inputs, idempotency, authorization, confirmed result, and complete audit evidence.
- Every workflow terminal/failure path is validated and simulator-covered.
- Production deployment is blocked by failed critical evaluation cases.

**Release gate**

- Prompt-injection/tool-abuse test, retrieval quality baseline, workflow load test, and evaluation reproducibility review pass.

### R3 — Omnichannel operations and campaigns (8-10 weeks)

**Scope**

- Number/SIP inventory, assignment and routing; Smallest-native phone/SIP completion; web/mobile widgets.
- Meta WhatsApp Business messaging/calling adapter and unified customer timeline.
- Provider-neutral campaign scheduler/dispatcher, imports, segments, suppression, policy checks, retry/voicemail, number pools, cost estimate and approvals.
- Warm/cold transfer and human handoff context.
- Live Operations v1: active calls, live transcript where supported, transfer/end, queues, concurrency, emergency stop.

**Acceptance criteria**

- The same approved agent version operates on at least phone, web voice, and one WhatsApp journey with capability-specific validation.
- Every outbound attempt has an immutable allow/deny policy decision and suppression evidence.
- Pause/emergency stop prevents new campaign attempts within 10 seconds and is independently verified.
- Warm transfer passes a summary and records provider/human acknowledgement; failure follows the configured fallback.

**Release gate**

- Carrier/provider certification, WhatsApp/account eligibility, consent/DNC legal review, concurrency/load test, number reputation plan, and failover drill pass.

### R4 — Quality, analytics, governance and enterprise controls (8-10 weeks)

**Scope**

- Conversation timeline, structured outcomes, QA sampling/scorecards/review, correction history and regression creation.
- Metric dictionary, latency/cost facts, agent/version/workflow/tool/campaign/language analytics, topics, cohorts, alerts, exports and warehouse connector.
- Realtime live intervention expansion, incidents and operational alerts.
- Fine-grained RBAC, SSO/SCIM/MFA, policy engine, retention/redaction/DSR/legal hold, immutable audit evidence, data-residency controls.
- Budgets, entitlements, usage/cost reconciliation and billing workflows.

**Acceptance criteria**

- Aggregate metrics reconcile to conversation facts and provider invoices within documented tolerances.
- QA reviewer can score, correct, escalate, and turn a failure into a linked regression case.
- Enterprise admin can prove who accessed/exported/changed/deployed what and when.
- Retention deletion and legal hold produce tested, contradictory-safe outcomes.

**Release gate**

- Independent penetration test, access review, restore/DR test, privacy/compliance evidence review, metering reconciliation, and enterprise UAT pass.

### R5 — Regional scale and platform commercialization (6-8 weeks)

**Scope**

- UAE Arabic/English and India language/accent quality packs based on certified provider capabilities; RTL and transliteration completion.
- Additional provider adapter only for validated language/channel/residency gaps.
- Subaccounts, white-label/custom domain, partner templates, sandbox, SDKs/API docs, webhook developer portal.
- Advanced experiments, rollout, automated anomaly-driven recommendations, and large-scale campaign controls.
- AED/VAT and approved INR/GST commercial flows; provider margin and enterprise invoicing.

**Acceptance criteria**

- Each marketed language/channel combination passes its audio, task, policy and human-review certification suite.
- Tenant data, branding, billing and provider limits are isolated across subaccounts.
- Public API/webhook compatibility contract, deprecation policy, SDK tests and sandbox are complete.

**Release gate**

- Regional counsel/carrier/provider approval, finance/tax approval, localization QA, capacity test at 2x forecast peak, and commercial support runbooks pass.

## 14. Release governance and gates

Every release candidate, regardless of roadmap stage, must satisfy:

### Functional gate

- Acceptance criteria linked to automated or signed manual evidence.
- No inert or misleading UI controls.
- Permissions tested for allowed and denied roles.
- Provider errors, timeouts, duplicates, out-of-order events, and partial failures tested.

### Quality gate

- Unit, integration, provider contract, migration, E2E, accessibility, and regression-evaluation suites pass.
- Critical voice scenarios tested in every marketed language/channel combination.
- No unresolved severity-1 or severity-2 defect; lower defects have owner and deadline.

### Security/compliance gate

- No exposed secret or unreviewed credential scope.
- Threat-model delta reviewed; audit/retention/policy impacts tested.
- Consent, DNC, disclosure, recording, and deletion cases pass for affected use cases.
- Dependency/container/SAST/DAST results meet policy.

### Reliability/operations gate

- SLO impact, dashboards, alerts, capacity, rollback, migration, and runbook are ready.
- Trace from user action to provider operation/webhook is demonstrable.
- Feature flag or rollback path exists for high-impact change.

### Commercial/support gate

- Entitlement, metering, limits, provider cost, and customer-facing documentation are correct.
- Support can identify provider request IDs, replay safe operations, and explain user-facing errors without database access.

## 15. Engineering work packages and dependency order

| Order | Work package | Depends on | Unlocks |
|---:|---|---|---|
| 1 | Secure identity/session, environment context, fine-grained authorization foundation | current auth | all enterprise and write journeys |
| 2 | Outbox/inbox, audit, correlation, provider registry/bindings | PostgreSQL migration | reliable deploy, webhooks, integrations, campaigns |
| 3 | Agent component/version/deployment model | 1-2 | Studio, approvals, rollback, experiments |
| 4 | Conversation event model and detailed workspace | 2 | live ops, QA, analytics, support |
| 5 | Secret vault and tool runtime | 1-2 | connectors, actions, workflow tools |
| 6 | Knowledge index releases | 2-3 | grounded agents and retrieval evaluation |
| 7 | Workflow version graph/engine | 3, 5-6 | Playbooks, complex journeys, simulations |
| 8 | Evaluation/test runner | 3, 5-7 | deployment gates and quality claims |
| 9 | Channel/number/routing abstraction | 2-3 | SIP, WhatsApp, live operations |
| 10 | Policy engine and contact identity/consent ledger | 1-2 | safe campaigns and cross-channel context |
| 11 | Provider-neutral campaign orchestrator | 3, 9-10 | scalable outbound operations |
| 12 | Realtime gateway/live operations | 4, 9 | supervisor intervention and incidents |
| 13 | Metric/usage/cost facts and warehouse export | 2, 4, 11 | analytics, billing, budgets |
| 14 | Enterprise admin/commercial/regional packs | all relevant foundations | GA and scale |

## 16. Migration plan from the existing schema

1. Add `organizations`/`environments` while treating each existing tenant as one organization, one workspace, and a default production environment.
2. Add provider registry/binding/operation tables. Backfill Smallest IDs/config/sync state from `agents`; maintain compatibility reads during one release.
3. Add `agent_drafts`, immutable `agent_versions`, component references, deployments and approvals. Create an initial version from every existing agent and mark its current provider mapping explicitly.
4. Normalize workflows into immutable versions/nodes/edges while retaining a migration adapter for current positional nodes.
5. Replace raw knowledge content with source/document/revision/index entities; preserve existing entries as text sources.
6. Create secret references, migrate credentials through an operator-controlled one-time job, redact `Integration.config`, and rotate any credential whose exposure cannot be disproved.
7. Introduce `conversations`/events/media/transcript segments and map current `calls`, transcripts and summaries. Keep read compatibility until the console uses the new APIs.
8. Introduce contacts/identities, audiences, suppression decisions and attempts; map campaign contacts. Disable legacy Twilio-only execution after provider-neutral dispatch passes parity tests.
9. Create usage/cost ledgers and backfill from current usage/call records with a clearly labelled source and reconciliation status.
10. Use expand/migrate/verify/contract. Never combine destructive column removal with the release that first writes the replacement model.

## 17. Verification strategy

**Automated layers**

- Domain unit tests for state machines, policy, metric formulas and version resolution.
- Database integration tests with real PostgreSQL for tenant isolation, constraints, outbox/inbox and migrations.
- Provider contract tests with recorded/sanitized fixtures and mocked error/timeout/out-of-order paths; no paid call in CI.
- Connector/tool tests for schema, SSRF, secret redaction, idempotency and approval.
- Frontend component and accessibility tests; Playwright E2E for every critical lifecycle.
- Audio golden tests and human language certification for marketed languages/accents.
- Evaluation regression suites with deterministic tool mocks and repeated probabilistic runs.
- Load/soak/chaos tests for calls, campaigns, webhooks, queues, provider throttling and storage.
- Security tests for cross-tenant access, role bypass, injection, replay, credential leakage, export, and deletion.

**Minimum end-to-end scenarios**

1. Create from template -> edit -> attach knowledge/tool/workflow -> test -> approve -> deploy -> web call -> review -> rollback.
2. Inbound phone -> identify language/contact -> knowledge answer -> tool action -> warm transfer -> WhatsApp summary.
3. Import audience -> normalize -> suppress DNC/revoked/quiet-hours -> test call -> approve -> launch -> pause -> resume -> outcome report.
4. Provider timeout after remote creation -> retry without duplicate -> reconcile -> complete deployment.
5. Out-of-order/duplicate signed webhooks -> one correct conversation timeline and usage record.
6. Contact revokes consent during a campaign -> queued future attempts are suppressed and auditable.
7. Knowledge source is deleted -> production index updates -> answer no longer retrieves deleted material -> deletion evidence produced.
8. Cross-tenant user attempts direct-object access/export/live intervention -> denied and audited.

## 18. Decisions required before R1

1. Confirm the initial customer segment: internal Al Zaabi Group operating platform, external SaaS, or both from the first release. The blueprint supports both, but subaccounts, billing, branding, and support priorities differ.
2. Confirm the first three production use cases and their measurable success outcomes. Recommended: AI receptionist/routing, customer support/order status, and lead qualification/appointment booking.
3. Approve whether WhatsApp calling/messaging is an R3 launch dependency or a later differentiator.
4. Approve the initial data-region strategy for UAE and India and whether enterprise dedicated environments are commercially required.
5. Select the identity provider, secret manager, object storage, observability stack, and analytics warehouse standard.
6. Define human ownership: product, voice/conversation design, backend, frontend, telephony, ML/evaluation, security, compliance, data, QA, SRE, and customer operations.
7. Authorize provider/legal/commercial discovery for numbers, SIP, WhatsApp eligibility, call recording, DNC/consent, data processing, and regional invoicing.

## 19. Success scorecard

The executive scorecard should use source actuals and agreed targets for:

- deployment lead time and rollback time;
- evaluation pass rate and production regression rate;
- connected rate, task success, containment, transfer success, and repeat-contact rate;
- p50/p95 conversational latency and interruption success;
- tool success, provider error, webhook lag, and drift incidents;
- policy blocks, confirmed violations, DNC/consent exceptions, and deletion SLA;
- cost per conversation and cost per successful business outcome;
- active tenants, activated agents, deployed channels, minutes, retention, and gross margin;
- time from failed conversation to reviewed issue to regression test to corrected version.

Targets must be set per use case, channel, language, and provider after baseline measurement. A single blended “AI success rate” would hide operational risk and should not be used as the primary KPI.

---

This blueprint is the target contract for product, design, engineering, security, data, QA, operations, finance, and compliance. New work should map to one of the 14 modules, identify its canonical data owner, state its provider capability requirements, include measurable acceptance criteria, and pass the release gates above.
