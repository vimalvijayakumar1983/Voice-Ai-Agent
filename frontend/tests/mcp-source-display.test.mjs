import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('unspoken MCP analysis drafts are not rendered as conversation turns', () => {
  const calls = readFileSync(new URL('../src/pages/calls.tsx', import.meta.url), 'utf8');
  assert.match(calls, /turns\.filter\(\(turn\) => !\['analysis', 'analysis_candidate'\]/);
});

test('source reports are distinct literal text, not generated speech or executable HTML', () => {
  const calls = readFileSync(new URL('../src/pages/calls.tsx', import.meta.url), 'utf8');
  assert.match(calls, /speaker === 'source'/);
  assert.match(calls, /Original MCP response/);
  assert.match(calls, /delivery_state/);
  assert.match(calls, /<pre[^>]+>\{content\}<\/pre>/);
  assert.match(calls, /Playback may have been interrupted/);
  assert.doesNotMatch(calls, /dangerouslySetInnerHTML/);
});

test('prepared narration is separate from the unmodified source and not labelled a recording', () => {
  const calls = readFileSync(new URL('../src/pages/calls.tsx', import.meta.url), 'utf8');
  assert.match(calls, /presentation_text/);
  assert.match(calls, /Prepared spoken answer/);
  assert.match(calls, /not a recording transcript/);
  assert.match(calls, /<details open=\{!presentation\}>/);
  assert.match(calls, /<p>\{presentation\}<\/p>/);
});
