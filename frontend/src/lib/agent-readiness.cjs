'use strict';

function isAgentCallReady(agent) {
  return Boolean(
    agent
      && agent.is_active
      && agent.provider_agent_id
      && agent.provider_revision_id
      && agent.last_synced_at,
  );
}

function agentTestReadinessMessage(agent) {
  if (!agent?.provider_agent_id) return 'Provision this agent before testing.';
  if (!agent.is_active) return 'Activate this agent before testing.';
  if (agent.sync_status === 'provider_scanning') {
    return 'Wait for Smallest.ai revision and security checks to finish before testing.';
  }
  return 'Publish and verify this agent before testing.';
}

function providerActionNotice(name, action, status) {
  if (status === 'error') {
    return {
      type: 'error',
      text: `${name} could not be verified on Smallest.ai. Review the provider status before retrying.`,
    };
  }
  if (status === 'synced') {
    return {
      type: 'success',
      text: action === 'provision'
        ? `${name} is provisioned and published on Smallest.ai.`
        : `${name} has been verified through the Smallest.ai versioning workflow.`,
    };
  }
  if (status === 'provider_scanning' || status === 'publishing') {
    return {
      type: 'info',
      text: `${name} is awaiting Smallest.ai revision and security checks. Use Check status before retrying.`,
    };
  }
  if (status === 'publish_unknown' || status === 'provision_unknown') {
    return {
      type: 'info',
      text: `${name}'s provider result is not confirmed yet. Check status or use the guarded resolution flow before retrying.`,
    };
  }
  if (status === 'provisioning') {
    return {
      type: 'info',
      text: `${name} is still being provisioned on Smallest.ai. Use Check status before retrying.`,
    };
  }
  if (status === 'dirty') {
    return {
      type: 'info',
      text: `${name}'s interrupted provider update is safe to publish again.`,
    };
  }
  return {
    type: 'info',
    text: `${name}'s Smallest.ai status is ${String(status).replaceAll('_', ' ')}.`,
  };
}

module.exports = {
  agentTestReadinessMessage,
  isAgentCallReady,
  providerActionNotice,
};
