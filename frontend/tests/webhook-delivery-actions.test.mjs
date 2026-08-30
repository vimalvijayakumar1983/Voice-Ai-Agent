import test from 'node:test';
import assert from 'node:assert/strict';

import webhookActions from '../src/lib/webhook-delivery-actions.cjs';

const { webhookReplayAvailability, webhookUndeliveredResultLabel } = webhookActions;

test('failed deliveries are replayable only while the destination is active', () => {
  assert.deepEqual(webhookReplayAvailability(true, 'failed'), {
    enabled: true,
    reason: null,
  });
  assert.deepEqual(webhookReplayAvailability(false, 'failed'), {
    enabled: false,
    reason: 'Activate the destination before replaying.',
  });
});

test('non-failed deliveries remain ineligible for replay', () => {
  for (const status of ['pending', 'sent']) {
    assert.deepEqual(webhookReplayAvailability(true, status), {
      enabled: false,
      reason: 'Only failed deliveries can be replayed.',
    });
  }
});

test('durable queue sentinels are presented as recovery states, not internal codes', () => {
  assert.equal(webhookUndeliveredResultLabel('queue_pending'), 'Awaiting worker result');
  assert.equal(
    webhookUndeliveredResultLabel('queue_unavailable'),
    'Waiting for automatic queue recovery',
  );
  assert.equal(webhookUndeliveredResultLabel('http_503'), 'http_503');
});
