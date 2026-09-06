import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const page = readFileSync(new URL('../src/pages/knowledge.tsx', import.meta.url), 'utf8');

test('text uploads show saved background processing rather than premature completion', () => {
  assert.match(page, /Text saved\. AI processing continues in the background/);
  assert.match(page, /sourceUploadCompilation\(source\)/);
  assert.match(page, /compilation\?\.status === 'queued' \|\| compilation\?\.status === 'processing'/);
  assert.match(page, /Processing AI/);
});

test('failed original-only sources have an in-place retry action', () => {
  assert.match(page, /source\.retrieval_ready \|\| Boolean\(compilation\)/);
  assert.match(page, /disabled=\{busy \|\| compilationActive\}/);
  assert.match(page, /compilation\?\.status === 'failed' \? 'Retry processing'/);
});
