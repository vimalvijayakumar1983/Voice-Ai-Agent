import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/knowledge.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');

test('website, PDF and text expose the same processing control', () => {
  assert.equal((page.match(/<ProcessingModeField \/>/g) || []).length, 3);
  assert.match(api, /JSON.stringify\(\{ name, content, processing_mode: processingMode \}\)/);
  assert.match(api, /form.append\('processing_mode', processingMode\)/);
});

test('existing sources can compile in place with explicit cost and approval guidance', () => {
  assert.match(page, /Structure with AI/);
  assert.match(page, /may incur extraction charges/);
  assert.match(api, /sourceId\}\/compile/);
  assert.match(page, /no duplicate document was created/);
});

test('source review is lazy, includes evidence and does not render source HTML', () => {
  assert.match(page, /if \(event.currentTarget.open\) void loadPreview\(\)/);
  assert.match(page, /<blockquote>\{fact.evidence\}<\/blockquote>/);
  assert.match(page, /Original extracted text/);
  assert.match(api, /sourceId\}\/preview/);
  const review = page.slice(page.indexOf('function SourceReview'), page.indexOf('function SourcesSection'));
  assert.doesNotMatch(review, /dangerouslySetInnerHTML/);
});

test('ingestion does not reset a synthetic event or erase content after an error', () => {
  const handlers = page.slice(page.indexOf('const uploadPdf'), page.indexOf('const removeSource'));
  assert.doesNotMatch(handlers, /currentTarget.reset\(\)/);
  assert.match(page, /Compiling and validating/);
  assert.match(page, /Extracting, compiling and indexing/);
});
