import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const agentsSource = readFileSync(new URL('../src/pages/agents.tsx', import.meta.url), 'utf8');

test('published agents can be deleted with explicit provider and knowledge-base scope', () => {
  assert.match(agentsSource, /Its Smallest\.ai agent will also be archived\./);
  assert.match(agentsSource, /Its knowledge base will not be deleted\./);
  assert.match(agentsSource, /deleted from VAV and archived on Smallest\.ai/);
  assert.doesNotMatch(
    agentsSource,
    /disabled=\{Boolean\(agent\.provider_agent_id\)[^}]*\}/,
  );
});
