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

test('crawl progress treats excluded non-content pages as terminal and auditable', () => {
  assert.match(knowledgeSource, /page\.status === 'skipped'/);
  assert.match(knowledgeSource, /non-content page/);
  assert.match(knowledgeSource, /crawl\.indexed_count \+ crawl\.failed_count \+ excluded\.length/);
});
