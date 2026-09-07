import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const source = readFileSync('src/components/McpConnections.tsx', 'utf8');
const api = readFileSync('src/lib/api.ts', 'utf8');
const integrations = readFileSync('src/pages/integrations.tsx', 'utf8');

test('MCP management uses authenticated integration API, not browser-to-MCP networking', () => {
  assert.match(source, /api\.testMcpConnection/);
  assert.match(api, /integrations\/\$\{id\}\/mcp\/test/);
  assert.doesNotMatch(source, /fetch\(/);
  assert.match(source, /type="password"/);
  assert.doesNotMatch(source, /defaultValue=\{[^}]*credential/);
});

test('MCP grants are explicit and unsupported runtime/write tools disabled', () => {
  assert.match(source, /disabled=\{!tool\.read_only\}/);
  assert.match(source, /disabled=\{!agent\.eligible\}/);
  assert.match(source, /public_data_approved/);
  assert.match(source, /not call latency/);
  assert.match(source, /single-pass receptionist remains unchanged/);
  assert.match(source, /schema/);
  assert.doesNotMatch(source, /dangerouslySetInnerHTML/);
});

test('MCP is separate from appointment connector cards', () => {
  assert.match(integrations, /<McpConnections canManage=\{canManage\}/);
  assert.match(integrations, /\['his_api', 'vav_crm', 'google_sheets'\]\.includes/);
});

test('private MCP requires explicit staff and upstream scope review without automatic grants', () => {
  assert.match(source, /value="private_staff"/);
  assert.match(source, /option\.private_eligible/);
  assert.match(source, /name="allowed_user_ids"/);
  assert.match(source, /name="upstream_scope_approved"/);
  assert.match(source, /name="private_data_approved"/);
  assert.match(source, /conversation text is stored in VAV/);
  assert.match(source, /Changing mode clears existing permissions/);
  assert.match(api, /integrations\/mcp\/staff/);
});
