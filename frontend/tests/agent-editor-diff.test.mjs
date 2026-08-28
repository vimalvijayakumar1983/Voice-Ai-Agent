import test from 'node:test';
import assert from 'node:assert/strict';

import editorDiff from '../src/components/agent-editor-diff.cjs';

const { agentEditorPatch } = editorDiff;

test('agent edit patch contains only fields whose persisted value changed', () => {
  const original = {
    name: 'Receptionist',
    description: 'Answers calls',
    voice_id: 'aanya',
    supported_languages: ['en', 'hi'],
    language_switching_enabled: true,
  };
  const current = { ...original, name: 'Front desk concierge' };

  assert.deepEqual(agentEditorPatch(original, current), { name: 'Front desk concierge' });
});

test('agent edit patch preserves explicit empty, null, false, and zero values', () => {
  const original = {
    greeting_message: 'Hello',
    fallback_message: 'Please wait',
    language_switching_enabled: true,
    temperature: 0.7,
  };
  const current = {
    greeting_message: '',
    fallback_message: null,
    language_switching_enabled: false,
    temperature: 0,
  };

  assert.deepEqual(agentEditorPatch(original, current), current);
});

test('equal arrays do not create a redundant agent patch', () => {
  const original = { supported_languages: ['en', 'hi'] };
  const current = { supported_languages: ['en', 'hi'] };
  assert.deepEqual(agentEditorPatch(original, current), {});
});
