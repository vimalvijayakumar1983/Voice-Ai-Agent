import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('source reports are distinct literal text, not generated speech or executable HTML', () => {
  const calls = readFileSync(new URL('../src/pages/calls.tsx', import.meta.url), 'utf8');
  assert.match(calls, /speaker === 'source'/);
  assert.match(calls, /Original MCP response/);
  assert.match(calls, /delivery_state/);
  assert.match(calls, /<pre[^>]+>\{content\}<\/pre>/);
  assert.match(calls, /Playback may have been interrupted/);
  assert.doesNotMatch(calls, /dangerouslySetInnerHTML/);
});
