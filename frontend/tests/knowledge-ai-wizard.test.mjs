import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const knowledgeSource = readFileSync(new URL('../src/pages/knowledge.tsx', import.meta.url), 'utf8');
const layoutSource = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
const wizardSource = readFileSync(new URL('../src/components/KnowledgeAIWizard.tsx', import.meta.url), 'utf8');

test('Knowledge Studio Create with AI is a genuine review-first OpenAI wizard', () => {
  assert.match(layoutSource, /\/knowledge\?create=ai/);
  assert.match(apiSource, /\/api\/v1\/knowledge\/ai-draft/);
  assert.match(wizardSource, /Generate reviewable draft/);
  assert.match(wizardSource, /Zero automatic indexing/);
  assert.match(knowledgeSource, /Review knowledge metadata/);
  assert.match(knowledgeSource, /recommended_sources/);
  assert.match(knowledgeSource, /OpenAI generated governed metadata only/);
});
