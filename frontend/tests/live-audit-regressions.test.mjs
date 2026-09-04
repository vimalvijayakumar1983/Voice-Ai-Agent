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
const callsSource = readFileSync(new URL('../src/pages/calls.tsx', import.meta.url), 'utf8');

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

test('Inworld runtime exposes native delivery modes with honest cost guidance', () => {
  assert.match(runtimeSource, /id="runtime-delivery-mode"/);
  assert.match(runtimeSource, /value="balanced">Balanced · recommended default/);
  assert.match(runtimeSource, /value="creative">Creative · more expressive/);
  assert.match(runtimeSource, /value="stable">Stable · most predictable/);
  assert.match(runtimeSource, /no separate feature fee/);
  assert.match(runtimeSource, /normal synthesized-character usage still applies/);
  assert.match(runtimeSource, /LiveKit native dynamic turn detection/);
  assert.match(runtimeSource, /Transport, SIP, and model usage remain billable/);
  assert.match(apiSource, /tts_delivery_mode: 'stable' \| 'balanced' \| 'creative'/);
  assert.match(apiSource, /stt_model: 'auto' \| 'assemblyai\/u3-rt-pro' \| 'soniox\/stt-rt-v4'/);
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
  assert.match(agentsSource, /LiveKit · Native Inworld Realtime available/);
  assert.match(agentsSource, /if \(runtime\.enabled && runtime\.status === 'active'\) return 'Runtime active'/);
  assert.match(agentsSource, /if \(runtime\.status === 'inactive'\) return 'Runtime inactive'/);
  assert.match(agentsSource, /Serving or provider synced/);
});

test('knowledge approval reports the immutable voice-recognition artifact', () => {
  assert.match(apiSource, /speech_lexicon:/);
  assert.match(knowledgeSource, /Voice recognition artifact/);
  assert.match(knowledgeSource, /critical-name coverage/);
  assert.match(knowledgeSource, /pinned to this approved source revision/);
  assert.match(knowledgeSource, /awaiting its versioned voice-recognition backfill/);
});

test('knowledge release rollback is audited compare-and-swap, not a pointer rewrite', () => {
  assert.match(apiSource, /listKnowledgeReleases/);
  assert.match(apiSource, /expected_current_revision_id: string \| null/);
  assert.match(apiSource, /reactivateKnowledgeRelease/);
  assert.match(knowledgeSource, /Restore a previous VAV release/);
  assert.match(knowledgeSource, /Incident or rollback reason/);
  assert.match(knowledgeSource, /selected\.serving_revision\?\.revision_id \|\| null/);
  assert.match(knowledgeSource, /matching voice-recognition artifact move together/);
});

test('native Inworld runtime exposes a typed agent-level single-pass canary', () => {
  assert.match(apiSource, /knowledge_turn_mode: 'tool_loop' \| 'single_pass_experimental'/);
  assert.match(runtimeSource, /id="runtime-knowledge-turn-mode"/);
  assert.match(runtimeSource, /Grounded tool loop · control/);
  assert.match(runtimeSource, /Single pass · experimental canary/);
  assert.match(runtimeSource, /Agent-level A\/B warning/);
  assert.match(runtimeSource, /this is not a per-call split/);
  assert.match(runtimeSource, /one approved evidence lookup/);
  assert.match(runtimeSource, /one tool-free reply/);
  assert.match(runtimeSource, /knowledge_turn_mode: voiceRuntime === 'inworld_realtime'/);
});

test('diagnostic recording is opt-in, governed, and does not imply capture or playback', () => {
  assert.match(apiSource, /diagnostic_recording_mode: 'off' \| 'livekit_egress_explicit_consent'/);
  assert.match(runtimeSource, /id="runtime-diagnostic-recording"/);
  assert.match(runtimeSource, /value="off">Off · safe default/);
  assert.match(runtimeSource, /Request LiveKit diagnostic capture · explicit consent/);
  assert.match(runtimeSource, /does not start LiveKit Egress, store audio, or make VAV playback available/);
  assert.match(runtimeSource, /absence is not consent/);
  assert.match(runtimeSource, /encrypted regional storage, retention, deletion, and access auditing/);
  assert.match(callsSource, /LiveKit recording state/);
  assert.match(callsSource, /LiveKit recording blocker/);
  assert.match(callsSource, /A saved operator request is not consent/);
  assert.match(callsSource, /legacy access rule never authorizes LiveKit capture/);
});

test('call diagnostics separate recognition, retrieval, grounding, and session latency', () => {
  assert.match(callsSource, /Realtime session connection/);
  assert.match(callsSource, /Participant active → LiveKit server speaking/);
  assert.match(callsSource, /Worker entry → LiveKit server speaking/);
  assert.match(callsSource, /Runtime admission → LiveKit server speaking \(legacy\)/);
  assert.match(callsSource, /Last knowledge search/);
  assert.match(callsSource, /Recognition model/);
  assert.match(callsSource, /Unsupported factual responses/);
  assert.match(callsSource, /trace\.grounding_outcome/);
  assert.match(callsSource, /Server-observed response latency p95/);
  assert.match(callsSource, /Latency sample size/);
  assert.match(callsSource, /Latency not measured/);
  assert.match(callsSource, /SIP\/RTP arrival at the caller/);
  assert.match(callsSource, /not a provider bill/);
  assert.match(callsSource, /Provider acknowledgement on this call/);
  assert.match(callsSource, /Not captured; readiness is a separate probe/);
  assert.doesNotMatch(callsSource, /provider acceptance checked by readiness/);
  assert.match(callsSource, /Critical-name coverage/);
  assert.match(callsSource, /Wrong-script repairs/);
  assert.match(callsSource, /Last exact-fact path/);
  assert.match(callsSource, /wrong-script clarification/);
  assert.match(callsSource, /Knowledge turn policy/);
  assert.match(callsSource, /Last single-pass orchestration/);
  assert.match(callsSource, /single_pass_total_ms/);
  assert.match(callsSource, /response_action/);
});
