import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const knowledgeSource = readFileSync(
  new URL('../src/pages/knowledge.tsx', import.meta.url),
  'utf8',
);

test('ready website sources retain an explicit re-extract and re-index action', () => {
  assert.match(knowledgeSource, /const repairable = isWebsite;/);
  assert.match(knowledgeSource, /isReady \? 'Refresh page' : 'Repair page'/);
  assert.match(knowledgeSource, /re-index its searchable content/);
});
