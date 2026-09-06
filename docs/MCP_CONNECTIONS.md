# MCP connections: initial read-only release

## User flow

Open **Integrations → MCP connections → Add MCP connection**. Enter a company
scope label, public HTTPS Streamable HTTP endpoint, optional bearer credential,
and a 2–30-second operation timeout. Endpoints and credentials use the existing
encrypted, write-only integration storage. Leaving the credential or URL blank
on edit preserves it. Save does not contact the server or grant any tools.

**Test connection & discover tools** performs MCP initialize and paginated
tools/list through the official Python SDK. It does not execute any business
tool, sample an LLM, launch a local process, or open a resource URL. The result
shows last-test time, handshake/discovery milliseconds, and a bounded catalog.
This is not end-to-end voice latency or a continuous health monitor.

An owner/admin must explicitly select read-only tools, attest to public-safe
data, and select eligible agents. Tool schemas and descriptions are available
for review as escaped text. Unknown or write-capable tools are blocked. Server
annotations are not a security guarantee: use a trusted server with a genuinely
read-only, company-scoped credential. A company label is a routing label, NOT
row-level ERP authorization. No private customer/patient/receivables tools should
be approved before a verified-customer authorization layer exists.

## Runtime boundary

Supported: enabled LiveKit + Inworld realtime **tool-loop** profiles. MCP tools
are added alongside approved-knowledge tools, without changing voice models,
turn timing, company KB bindings, or existing agent configuration. Runtime checks
tenant, active connection, agent grant, tool grant, catalog fingerprint, and
profile eligibility before each execution. Authorization/configuration is checked
again before returning results. New grants need a new call. Revocation prevents
subsequent lookups; it cannot cancel an ERP request already executing remotely.

Single-pass deliberately generates with tools disabled. It is NOT silently
switched to tool-loop. Single-pass, pipeline, Sarvam, Smallest and other unsupported
profiles are visibly ineligible in the permission picker. Use an explicitly
configured QA tool-loop agent before enabling a production agent.

Calls expose up to 20 approved MCP tools and at most 50 lookup attempts. Responses
are capped at 12,000 characters and tagged as untrusted data. Per-call runtime
metadata contains integration/tool identifiers, duration and success/failure;
it excludes credentials, arguments and customer result bodies. These tool timings
are not caller end-of-speech-to-audio latency.

This initial adapter establishes an MCP session for each lookup and rediscovers
the tool schema before executing. This prioritizes authorization and schema-change
checks over optimistic session reuse. Benchmark a real server before claiming
latency or introducing connection pooling; no low-latency promise is made here.

## Network and failure handling

Only public HTTPS Streamable HTTP is supported. No local stdio, arbitrary shell,
legacy SSE endpoint, private address, environment proxy, redirect, or query-string
credential. DNS is checked and pinned before connections with original-host TLS
verification. Response streams are bounded to 1 MB, compression is rejected,
catalogs are capped at 100 tools/200 KB/10 pages. Schemas must be self-contained
without references or regex constraints; schema depth/size and argument size are
bounded. A bounded operation timeout includes discovery, result reading and
session cleanup. There is no automatic retry of tools/call.

One operator-reviewed MCP-only exception allows exactly
`https://mcp-trading.13-232-147-135.sslip.io/mcp`. Its entire DNS answer set must
remain `13.232.147.135`; a changed answer fails closed for operator review.
Other sslip.io hosts, paths, ports, query strings and webhook/HIS/CRM uses remain
blocked. TLS verification, address pinning and redirect rejection still apply.
This exception does not grant tools or supply a bearer credential.

Configuration/credential/company changes invalidate discovery and grants. Discovery
failure clears grants. Changed tool descriptions, annotations or schemas remove
approval and are rejected at execution until reviewed again. Concurrent edits
during discovery return 409 rather than overwriting a newer configuration. Errors
shown to users/model are generic and omit server exception details.

## Not included

OAuth login, local MCP tunnels, private-network bridges, private record access,
booking/payment/ERP writes, caller identity verification, campaigns, CRM/HIS
transaction workflows and scheduled recalls are separate features. This is a
connection manager and an approved public-data read-tool adapter, not a completed
receivables or appointment automation system. No existing connection is created
or live customer data accessed by deploying this code.

## Verification

Unit/API tests cover credentials, server-owned discovery state, RBAC/tenant grants,
runtime compatibility, safe transport using the real SDK against a mocked HTTP
server, schema changes, read/write boundaries and failures. Frontend lint/build
and static contract tests cover wiring and disclosures. These are not tests of
the customer's Hermes endpoint or paid speech calls; obtain its endpoint and
securely configured credentials for that next acceptance test.
