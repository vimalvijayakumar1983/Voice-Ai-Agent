import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import conversationUi from '../src/lib/conversation-ui.cjs';

const {
  parseTestVariables,
  reduceTranscriptState,
  sessionErrorGuidance,
  transcriptLanguage,
} = conversationUi;

test('cumulative transcript deltas update one live turn and settle once', () => {
  const initial = { turns: [], live: null };
  const firstDelta = reduceTranscriptState(initial, { type: 'delta', role: 'user', text: 'I need' });
  const cumulativeDelta = reduceTranscriptState(firstDelta, { type: 'delta', role: 'user', text: 'I need an appointment' });
  assert.equal(cumulativeDelta.turns.length, 0);
  assert.deepEqual(cumulativeDelta.live, { role: 'user', text: 'I need an appointment' });

  const settled = reduceTranscriptState(cumulativeDelta, {
    type: 'settled',
    id: 1,
    role: 'user',
    text: 'I need an appointment',
  });
  assert.deepEqual(settled, {
    turns: [{ id: 1, role: 'user', text: 'I need an appointment' }],
    live: null,
  });
});

test('test variables accept scalar JSON objects and reject unsafe shapes', () => {
  assert.deepEqual(parseTestVariables('{"customer":"Vimal","priority":2,"vip":true}'), {
    customer: 'Vimal',
    priority: 2,
    vip: true,
  });
  assert.throws(() => parseTestVariables('["not","an","object"]'), /JSON object/);
  assert.throws(() => parseTestVariables('{"profile":{"tier":"gold"}}'), /string, number, or boolean/);
  assert.throws(() => parseTestVariables('{"_vav_call_id":"override"}'), /reserved/);
});

test('session errors provide permission and network recovery guidance', () => {
  assert.equal(
    sessionErrorGuidance(new DOMException('Permission denied', 'NotAllowedError')).title,
    'Microphone permission is blocked',
  );
  assert.match(
    sessionErrorGuidance(new Error('WebSocket connection failed')).message,
    /fresh single-use token/,
  );
});

test('transcript language is shown only when the provider supplied it', () => {
  assert.equal(transcriptLanguage({ language: 'hi-IN' }), 'hi-IN');
  assert.equal(transcriptLanguage({ metadata: { detected_language: 'ar-AE' } }), 'ar-AE');
  assert.equal(transcriptLanguage({ text: 'hello' }), '');
});

test('conversation recording UI uses authenticated blobs with explicit cleanup', () => {
  const callsPage = readFileSync(new URL('../src/pages/calls.tsx', import.meta.url), 'utf8');
  const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');

  assert.match(callsPage, /api\.getCallRecording\(callId\)/);
  assert.match(callsPage, /URL\.createObjectURL\(audio\)/);
  assert.match(callsPage, /URL\.revokeObjectURL/);
  assert.match(callsPage, /preload="none"/);
  assert.doesNotMatch(callsPage, /provider_recording_url/);
  assert.doesNotMatch(apiSource, /provider_recording_url:\s*string/);
});
