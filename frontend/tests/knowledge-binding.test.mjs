import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import knowledgeBinding from '../src/lib/knowledge-binding.cjs';

const { canBindKnowledgeAgent, knowledgeBindingGuidance } = knowledgeBinding;

const liveRevision = { revision_id: 'revision-live-1' };

function knowledgeBase(overrides = {}) {
  return {
    approval_status: 'draft',
    serving_revision: liveRevision,
    ...overrides,
  };
}

function agent(voiceProvider) {
  return { id: `agent-${voiceProvider}`, voice_provider: voiceProvider };
}

test('a retained live revision remains bindable to every VAV-native voice provider', () => {
  const draftWithLiveRevision = knowledgeBase();

  for (const provider of ['sarvam', 'elevenlabs', 'inworld']) {
    assert.equal(
      canBindKnowledgeAgent(draftWithLiveRevision, agent(provider)),
      true,
      `${provider} should bind to the retained live release`,
    );
  }
});

test('Smallest.ai remains approval-gated while native agents retain live access', () => {
  const draftWithLiveRevision = knowledgeBase();

  assert.equal(canBindKnowledgeAgent(draftWithLiveRevision, agent('smallest')), false);
  assert.match(knowledgeBindingGuidance(draftWithLiveRevision), /Smallest\.ai requires the draft to be approved/);
});

test('a first draft without a live revision cannot bind any provider', () => {
  const firstDraft = knowledgeBase({ serving_revision: null });

  for (const provider of ['smallest', 'sarvam', 'elevenlabs', 'inworld']) {
    assert.equal(
      canBindKnowledgeAgent(firstDraft, agent(provider)),
      false,
      `${provider} must wait for the first approved release`,
    );
  }
  assert.match(knowledgeBindingGuidance(firstDraft), /publish its first live release/);
});

test('an approved knowledge base can bind native and Smallest.ai agents', () => {
  const approved = knowledgeBase({ approval_status: 'approved' });

  for (const provider of ['smallest', 'sarvam', 'elevenlabs', 'inworld']) {
    assert.equal(canBindKnowledgeAgent(approved, agent(provider)), true);
  }
  assert.equal(knowledgeBindingGuidance(approved), null);
});

test('Knowledge Studio applies provider-aware eligibility to both controls', () => {
  const knowledgeSource = readFileSync(
    new URL('../src/pages/knowledge.tsx', import.meta.url),
    'utf8',
  );

  assert.match(knowledgeSource, /disabled=\{busy \|\| eligible\.length === 0\}/);
  assert.match(knowledgeSource, /disabled=\{!canBindKnowledgeAgent\(selected, agent\)\}/);
  assert.match(knowledgeSource, /!canBindKnowledgeAgent\(selected, selectedAgent\)/);
  assert.doesNotMatch(
    knowledgeSource,
    /aria-label="Agent to bind"[^>]*approval_status !== 'approved'/,
  );
});
