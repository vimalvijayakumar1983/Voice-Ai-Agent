'use strict';

function isAgentCallReady(agent, runtimeProfile) {
  if (agent?.voice_provider === 'sarvam') {
    return Boolean(agent.is_active && runtimeProfile?.enabled && runtimeProfile?.status === 'active');
  }
  return Boolean(
    agent
      && agent.is_active
      && agent.provider_agent_id
      && agent.provider_revision_id
      && agent.last_synced_at
      && agent.sync_status === 'synced',
  );
}

function agentTestReadinessMessage(agent, runtimeProfile) {
  if (agent?.voice_provider === 'sarvam') {
    if (!agent.is_active) return 'Activate this agent before testing.';
    if (!runtimeProfile) return 'Load the VAV runtime profile before testing.';
    if (runtimeProfile.blockers?.length) return runtimeProfile.blockers.join(' ');
    return 'Activate the VAV realtime runtime before testing.';
  }
  if (!agent?.provider_agent_id) return 'Provision this agent before testing.';
  if (!agent.is_active) return 'Activate this agent before testing.';
  if (agent.sync_status === 'provider_scanning') {
    return 'Wait for Smallest.ai revision and security checks to finish before testing.';
  }
  if (agent.sync_status === 'dirty') {
    return 'Publish and verify this agent\'s current changes before testing.';
  }
  return 'Publish and verify this agent before testing.';
}

function isProviderConfigCorrection(agent) {
  return Boolean(
    agent
      && agent.sync_status === 'error'
      && agent.provider_config?.publish?.phase === 'provider_config_mismatch',
  );
}

function providerActionLabel(agent) {
  if (agent?.sync_status === 'synced') return 'In sync';
  if (isProviderConfigCorrection(agent)) return 'Verify correction';
  if (['publishing', 'provider_scanning', 'publish_unknown'].includes(agent?.sync_status)) {
    return 'Check status';
  }
  return 'Publish';
}

function providerActionNotice(name, action, status, reconciliationOnly = false) {
  if (reconciliationOnly && status === 'synced') {
    return {
      type: 'success',
      text: `${name}'s Smallest.ai correction was verified without a VAV publish.`,
    };
  }
  if (reconciliationOnly && status === 'error') {
    return {
      type: 'error',
      text: `${name}'s Smallest.ai correction could not be verified; no VAV publish was attempted.`,
    };
  }
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
  isProviderConfigCorrection,
  isAgentCallReady,
  providerActionLabel,
  providerActionNotice,
};
