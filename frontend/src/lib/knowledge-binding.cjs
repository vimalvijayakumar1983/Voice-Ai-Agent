'use strict';

const VAV_NATIVE_KNOWLEDGE_PROVIDERS = new Set(['sarvam', 'elevenlabs', 'inworld']);

function isVavNativeKnowledgeProvider(provider) {
  return VAV_NATIVE_KNOWLEDGE_PROVIDERS.has(provider);
}

function canBindKnowledgeAgent(knowledgeBase, agent) {
  if (!knowledgeBase || !agent) return false;
  if (knowledgeBase.approval_status === 'approved') return true;
  return Boolean(
    knowledgeBase.serving_revision
      && isVavNativeKnowledgeProvider(agent.voice_provider),
  );
}

function knowledgeBindingGuidance(knowledgeBase) {
  if (knowledgeBase?.approval_status === 'approved') return null;
  if (knowledgeBase?.serving_revision) {
    return 'Draft changes are pending. VAV-native agents can use the retained live release; Smallest.ai requires the draft to be approved and provider-indexed.';
  }
  return 'Approve this knowledge base to publish its first live release before binding an agent.';
}

module.exports = {
  canBindKnowledgeAgent,
  isVavNativeKnowledgeProvider,
  knowledgeBindingGuidance,
};
