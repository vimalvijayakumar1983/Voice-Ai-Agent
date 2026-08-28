import test from 'node:test';
import assert from 'node:assert/strict';

import readiness from '../src/lib/agent-readiness.cjs';

const { agentTestReadinessMessage, isAgentCallReady, providerActionNotice } = readiness;

const readyAgent = {
  is_active: true,
  sync_status: 'synced',
  provider_agent_id: 'agent-123',
  provider_revision_id: 'revision-123',
  last_synced_at: '2026-08-28T00:00:00Z',
};

test('testing requires an active agent with a published provider revision', () => {
  assert.equal(isAgentCallReady(readyAgent), true);

  for (const missingField of ['provider_agent_id', 'provider_revision_id', 'last_synced_at']) {
    assert.equal(
      isAgentCallReady({ ...readyAgent, [missingField]: null }),
      false,
      `${missingField} must be present`,
    );
  }

  assert.equal(isAgentCallReady({ ...readyAgent, is_active: false }), false);
  assert.equal(
    isAgentCallReady({ ...readyAgent, sync_status: 'dirty' }),
    true,
    'a dirty local draft may still test its last published revision',
  );
  assert.equal(isAgentCallReady(undefined), false);
});

test('readiness guidance explains provider scanning and local drafts', () => {
  assert.equal(
    agentTestReadinessMessage({ ...readyAgent, sync_status: 'provider_scanning' }),
    'Wait for Smallest.ai revision and security checks to finish before testing.',
  );
  assert.equal(
    agentTestReadinessMessage({ ...readyAgent, provider_agent_id: null }),
    'Provision this agent before testing.',
  );
  assert.equal(
    agentTestReadinessMessage({ ...readyAgent, is_active: false }),
    'Activate this agent before testing.',
  );
});

test('provider notices never report incomplete or failed states as success', () => {
  assert.equal(providerActionNotice('Agent', 'sync', 'synced').type, 'success');
  assert.equal(providerActionNotice('Agent', 'sync', 'error').type, 'error');

  for (const status of [
    'publishing',
    'provider_scanning',
    'publish_unknown',
    'provisioning',
    'provision_unknown',
    'dirty',
  ]) {
    assert.equal(providerActionNotice('Agent', 'sync', status).type, 'info', status);
  }
});
