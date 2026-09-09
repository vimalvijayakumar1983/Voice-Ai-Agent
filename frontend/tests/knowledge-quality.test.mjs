import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/knowledge.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');

test('readable text and fact count never imply an agent-ready source', () => {
  const section = page.slice(page.indexOf('function SourcesSection'), page.indexOf('function AgentBinding'));
  assert.match(section, /quality_status === 'sample_checks_passed'/);
  assert.match(section, /Extracted · not verified/);
  assert.match(section, /Needs repair/);
  assert.match(section, /quality_issues/);
  assert.doesNotMatch(section, /Company facts available|Text searchable|company_fact_count/);
});

test('single-company ownership is editable independently from organizational scope', () => {
  assert.match(page, /Company this knowledge belongs to/);
  assert.match(page, /Leave blank for mixed-company knowledge/);
  assert.equal((page.match(/owner_company: String\(form.get\('owner_company'\)/g) || []).length, 2);
  assert.match(api, /owner_company\?: string \| null/);
});
