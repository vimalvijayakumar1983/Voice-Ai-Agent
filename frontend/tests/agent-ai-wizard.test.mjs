import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const agentsSource = readFileSync(new URL('../src/pages/agents.tsx', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
const wizardSource = readFileSync(new URL('../src/components/AgentAIWizard.tsx', import.meta.url), 'utf8');

test('Create with AI opens a genuine review-first OpenAI wizard', () => {
  assert.match(layoutSource, /'\/agents\?create=ai'/);
  assert.match(apiSource, /\/api\/v1\/agents\/ai-draft/);
  assert.match(wizardSource, /Generate reviewable draft/);
  assert.match(wizardSource, /review every field before anything is saved/i);
  assert.match(agentsSource, /Review the AI-generated draft/);
  assert.match(agentsSource, /aiDraftValues\(generatedDraft\)/);
});
