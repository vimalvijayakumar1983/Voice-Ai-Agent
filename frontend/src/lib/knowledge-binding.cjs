'use strict';

const VAV_NATIVE_KNOWLEDGE_PROVIDERS = new Set(['sarvam', 'elevenlabs', 'inworld']);

function isVavNativeKnowledgeProvider(provider) {
  return VAV_NATIVE_KNOWLEDGE_PROVIDERS.has(provider);
}

function canBindKnowledgeAgent(knowledgeBase, agent) {
  if (!knowledgeBase || !agent) return false;
  if (!isVavNativeKnowledgeProvider(agent.voice_provider)) return false;
  if (knowledgeBase.approval_status === 'approved') return true;
  return Boolean(knowledgeBase.serving_revision);
}

function knowledgeBindingGuidance(knowledgeBase) {
  if (knowledgeBase?.approval_status === 'approved') return null;
  if (knowledgeBase?.serving_revision) {
    return 'Draft changes are pending. Bound VAV-native agents keep using the retained live release until you approve the draft.';
  }
  return 'Approve this knowledge base to publish its first live release before binding an agent. Only VAV-native agents (Inworld, Sarvam, ElevenLabs) retrieve VAV knowledge.';
}

module.exports = {
  canBindKnowledgeAgent,
  isVavNativeKnowledgeProvider,
  knowledgeBindingGuidance,
};
