import test from 'node:test';
import assert from 'node:assert/strict';

import readiness from '../src/lib/agent-readiness.cjs';

const {
  agentTestReadinessMessage,
  isAgentCallReady,
  isProviderConfigCorrection,
  providerActionLabel,
  providerActionNotice,
} = readiness;

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
    false,
    'a dirty local draft must not test a stale published revision',
  );
  assert.equal(isAgentCallReady(undefined), false);
});

test('ElevenLabs agents use VAV phone runtime readiness without Smallest provisioning', () => {
  const runtime = { enabled: true, status: 'active', blockers: [] };
  const agent = {
    voice_provider: 'elevenlabs',
    is_active: true,
    provider_agent_id: null,
    provider_revision_id: null,
    last_synced_at: null,
    sync_status: 'local_only',
  };

  assert.equal(isAgentCallReady(agent, runtime), true);
  assert.equal(isAgentCallReady(agent, { ...runtime, enabled: false }), false);
  assert.equal(
    agentTestReadinessMessage(agent, { ...runtime, blockers: ['Connect ElevenLabs.'] }),
    'Connect ElevenLabs.',
  );
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
  assert.equal(
    agentTestReadinessMessage({ ...readyAgent, sync_status: 'dirty' }),
    'Publish and verify this agent\'s current changes before testing.',
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

test('only configuration mismatches use the reconciliation-only action', () => {
  const recoverable = {
    ...readyAgent,
    sync_status: 'error',
    provider_config: { publish: { phase: 'provider_config_mismatch' } },
  };

  assert.equal(isProviderConfigCorrection(recoverable), true);
  assert.equal(providerActionLabel(recoverable), 'Verify correction');
  assert.deepEqual(providerActionNotice('Agent', 'sync', 'synced', true), {
    type: 'success',
    text: "Agent's Smallest.ai correction was verified without a VAV publish.",
  });
  assert.deepEqual(providerActionNotice('Agent', 'sync', 'error', true), {
    type: 'error',
    text: "Agent's Smallest.ai correction could not be verified; no VAV publish was attempted.",
  });
  assert.equal(
    isProviderConfigCorrection({
      ...recoverable,
      provider_config: { publish: { phase: 'security_failed' } },
    }),
    false,
  );
  assert.equal(
    providerActionLabel({
      ...recoverable,
      provider_config: { publish: { phase: 'security_failed' } },
    }),
    'Publish',
  );
  assert.equal(
    providerActionLabel({ ...readyAgent, sync_status: 'provider_scanning' }),
    'Check status',
  );
});
