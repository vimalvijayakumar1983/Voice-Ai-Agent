'use strict';

function webhookReplayAvailability(isActive, deliveryStatus) {
  if (deliveryStatus !== 'failed') {
    return { enabled: false, reason: 'Only failed deliveries can be replayed.' };
  }
  if (!isActive) {
    return { enabled: false, reason: 'Activate the destination before replaying.' };
  }
  return { enabled: true, reason: null };
}

function webhookUndeliveredResultLabel(lastError) {
  if (lastError === 'queue_pending') return 'Awaiting worker result';
  if (lastError === 'queue_unavailable') return 'Waiting for automatic queue recovery';
  return lastError || 'Awaiting worker result';
}

module.exports = { webhookReplayAvailability, webhookUndeliveredResultLabel };
