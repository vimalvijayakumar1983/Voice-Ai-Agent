import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const playgroundSource = readFileSync(new URL('../src/pages/playground.tsx', import.meta.url), 'utf8');
const billingSource = readFileSync(new URL('../src/pages/billing.tsx', import.meta.url), 'utf8');
const runtimeSource = readFileSync(new URL('../src/components/RuntimeControlPanel.tsx', import.meta.url), 'utf8');
const knowledgeSource = readFileSync(new URL('../src/pages/knowledge.tsx', import.meta.url), 'utf8');
const settingsSource = readFileSync(new URL('../src/pages/settings.tsx', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
const agentsSource = readFileSync(new URL('../src/pages/agents.tsx', import.meta.url), 'utf8');

test('phone playground names the selected agent instead of a hard-coded agent', () => {
  assert.match(playgroundSource, /selected\?\.name \|\| 'this agent'/);
  assert.doesNotMatch(playgroundSource, /test Customer Support\./);
});

test('billing headline reports measurable line-item coverage', () => {
  assert.match(billingSource, /Measurable line-item coverage/);
  assert.match(billingSource, /summary\.full_cost_coverage/);
  assert.match(billingSource, /partial or unpriced/);
});

test('billing can filter direct LiveKit SIP and browser WebRTC lanes', () => {
  assert.match(billingSource, /<option value="livekit_sip">LiveKit SIP<\/option>/);
  assert.match(billingSource, /<option value="livekit_webrtc">LiveKit WebRTC<\/option>/);
  assert.match(billingSource, /value === 'livekit_webrtc'\) return 'LiveKit WebRTC'/);
  assert.match(apiSource, /provider\?: 'twilio' \| 'smallest' \| 'livekit_sip' \| 'livekit_webrtc' \| ''/);
});

test('phone diagnostics identify the selected transport and response engine', () => {
  assert.match(playgroundSource, /telephony_provider === 'livekit_sip' \? 'LiveKit SIP'/);
  assert.match(playgroundSource, /llm_provider === 'inworld' \? 'Inworld Router response engine'/);
});

test('runtime readiness tests the values currently visible in the form', () => {
  assert.match(runtimeSource, /hasUnsavedChanges/);
  assert.match(runtimeSource, /await api\.updateRuntimeProfile\(agent\.id, payload\(\)\)/);
  assert.match(runtimeSource, /Save & test readiness/);
});

test('Inworld speech can use direct OpenAI tools without losing the Router option', () => {
  assert.match(runtimeSource, /value="openai:gpt-4o-mini"/);
  assert.match(runtimeSource, /value="inworld:auto"/);
  assert.match(runtimeSource, /llm_provider: llmProvider/);
  assert.doesNotMatch(runtimeSource, /llm_provider: inworldRuntime \? 'inworld'/);
  assert.match(playgroundSource, /selectedRuntimeProfile\?\.llm_provider === 'openai' \? 'OpenAI' : 'Inworld Router'/);
});

test('knowledge health explains when every agent already has access', () => {
  assert.match(knowledgeSource, /All agents have knowledge access/);
  assert.doesNotMatch(knowledgeSource, /detail=\{`\$\{agents\.length - boundAgents\} available`\}/);
});

test('tenant SIP routes never collect platform LiveKit or carrier credentials', () => {
  assert.doesNotMatch(settingsSource, /id="livekit-(?:url|key|secret)"/);
  assert.doesNotMatch(settingsSource, /id="sip-(?:user|password|number)"/);
  assert.doesNotMatch(apiSource, /livekit_api_(?:key|secret): string/);
  assert.doesNotMatch(apiSource, /\binbound_number: string/);
});

test('VAV-managed agents show runtime lifecycle instead of Smallest draft state', () => {
  assert.match(agentsSource, /agentStateLabel\(agent, runtimeProfiles\[agent\.id\]\)/);
  assert.match(agentsSource, /if \(runtime\.enabled && runtime\.status === 'active'\) return 'Runtime active'/);
  assert.match(agentsSource, /if \(runtime\.status === 'inactive'\) return 'Runtime inactive'/);
  assert.match(agentsSource, /Serving or provider synced/);
});
