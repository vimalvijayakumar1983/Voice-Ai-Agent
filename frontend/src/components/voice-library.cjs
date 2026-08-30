'use strict';

const VOICE_STATUS_ORDER = {
  compatible: 0,
  unknown: 1,
  incompatible: 2,
  unavailable: 3,
};

function normalizedLanguage(code) {
  return String(code || '').trim().toLowerCase().replaceAll('_', '-');
}

function languageMatches(capability, requested) {
  const normalizedCapability = normalizedLanguage(capability);
  const normalizedRequested = normalizedLanguage(requested);
  if (!normalizedCapability || !normalizedRequested) return false;
  return normalizedCapability === normalizedRequested
    || normalizedCapability.startsWith(`${normalizedRequested}-`)
    || normalizedRequested.startsWith(`${normalizedCapability}-`);
}

function missingVoiceLanguages(voice, selectedLanguages) {
  const capabilities = Array.isArray(voice?.languages) ? voice.languages : [];
  if (capabilities.length === 0) return [];
  return selectedLanguages.filter(
    (requested) => !capabilities.some((capability) => languageMatches(capability, requested)),
  );
}

function voiceTier(voice) {
  if (voice?.source === 'cloned') return 'Cloned';
  const pool = String(voice?.voice_pool || voice?.voice_tier || voice?.tier || '').trim().toLowerCase();
  if (pool === 'pro') return 'Pro';
  if (pool === 'standard') return 'Standard';
  const model = String(voice?.synthesizer_model || '').toLowerCase();
  const reason = String(voice?.unavailability_reason || '').toLowerCase();
  if (model.includes('pro') || reason.includes('pro')) return 'Pro';
  if (model) return 'Provider-routed';
  return 'Unverified';
}

function voicePreviewAvailability(voice) {
  if (!voice?.synthesizer_model) {
    return {
      available: false,
      reason: voice?.unavailability_reason || 'No verified Atoms synthesizer model is available.',
    };
  }
  if (voice.unavailability_reason) {
    return { available: false, reason: voice.unavailability_reason };
  }
  if (voice.source === 'cloned') {
    return { available: true, reason: null };
  }
  const pool = String(voice.voice_pool || voice.voice_tier || voice.tier || '').trim().toLowerCase();
  if (!['standard', 'pro'].includes(pool)) {
    return {
      available: false,
      reason: 'Preview unavailable because the provider voice pool could not be verified.',
    };
  }
  return { available: true, reason: null };
}

function voiceCompatibility(voice, selectedLanguages) {
  if (!voice) {
    return {
      status: 'unknown',
      missingLanguages: [...selectedLanguages],
      reason: 'This voice is not present in the selected provider catalog.',
    };
  }
  if (!voice.synthesizer_model) {
    return {
      status: 'unavailable',
      missingLanguages: [],
      reason: voice.unavailability_reason || 'No verified Atoms synthesizer model is available.',
    };
  }
  if (!Array.isArray(voice.languages) || voice.languages.length === 0) {
    return {
      status: 'unknown',
      missingLanguages: [...selectedLanguages],
      reason: 'The selected provider does not provide language coverage for this voice.',
    };
  }
  const missingLanguages = missingVoiceLanguages(voice, selectedLanguages);
  if (missingLanguages.length > 0) {
    return {
      status: 'incompatible',
      missingLanguages,
      reason: 'This voice does not cover every selected agent language.',
    };
  }
  return {
    status: 'compatible',
    missingLanguages: [],
    reason: 'Catalog metadata covers every selected agent language.',
  };
}

function voiceSearchText(voice) {
  return [
    voice?.name,
    voice?.id,
    voice?.accent,
    voice?.gender,
    voice?.age,
    voice?.synthesizer_model,
    voiceTier(voice),
    ...(voice?.languages || []),
    ...(voice?.use_cases || []),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function reconcileVoiceSelection(voiceId, voices, selectedLanguages) {
  if (!voiceId) {
    return { voiceId: '', removed: false, voice: undefined, compatibility: null };
  }
  const voice = voices.find((item) => item.id === voiceId);
  const compatibility = voiceCompatibility(voice, selectedLanguages);
  if (compatibility.status === 'compatible') {
    return { voiceId, removed: false, voice, compatibility };
  }
  return { voiceId: '', removed: true, voice, compatibility };
}

function normalizedVoiceConfiguration(configuration) {
  const supportedLanguages = Array.from(new Set(
    (configuration?.supported_languages || []).map(normalizedLanguage).filter(Boolean),
  )).sort();
  const switchingEnabled = Boolean(configuration?.language_switching_enabled);
  return {
    voiceProvider: String(configuration?.voice_provider || 'smallest').trim().toLowerCase(),
    voiceId: String(configuration?.voice_id || '').trim(),
    language: normalizedLanguage(configuration?.language),
    supportedLanguages,
    switchingEnabled,
    switchingMode: configuration?.language_switching_mode
      || (switchingEnabled ? 'automatic' : 'disabled'),
  };
}

function voiceConfigurationChanged(original, current) {
  const left = normalizedVoiceConfiguration(original);
  const right = normalizedVoiceConfiguration(current);
  return JSON.stringify(left) !== JSON.stringify(right);
}

function voiceConfigurationGuard({ editorMode, catalogAvailable, original, current }) {
  const changed = editorMode === 'create' || voiceConfigurationChanged(original, current);
  if (catalogAvailable) return { allowed: true, changed, reason: null };
  if (editorMode === 'edit' && !changed) {
    return {
      allowed: true,
      changed: false,
      reason: 'Existing voice and language configuration will be preserved.',
    };
  }
  return {
    allowed: false,
    changed,
    reason: editorMode === 'create'
      ? 'Wait for the provider catalog before creating a safely validated agent.'
      : 'Voice or language changes cannot be validated while the provider catalog is unavailable.',
  };
}

function filterAndSortVoices(voices, options) {
  const {
    selectedLanguages = [],
    query = '',
    status = 'compatible',
    tier = 'all',
    gender = 'all',
    accent = 'all',
  } = options || {};
  const normalizedQuery = String(query).trim().toLowerCase();

  return voices
    .map((voice) => ({ voice, compatibility: voiceCompatibility(voice, selectedLanguages) }))
    .filter(({ voice, compatibility }) => {
      if (normalizedQuery && !voiceSearchText(voice).includes(normalizedQuery)) return false;
      if (status !== 'all' && compatibility.status !== status) return false;
      if (tier !== 'all' && voiceTier(voice).toLowerCase() !== tier) return false;
      if (gender !== 'all' && String(voice.gender || '').toLowerCase() !== gender) return false;
      if (accent !== 'all' && String(voice.accent || '') !== accent) return false;
      return true;
    })
    .sort((left, right) => {
      const statusDifference = VOICE_STATUS_ORDER[left.compatibility.status]
        - VOICE_STATUS_ORDER[right.compatibility.status];
      if (statusDifference !== 0) return statusDifference;
      if (left.voice.source !== right.voice.source) return left.voice.source === 'cloned' ? -1 : 1;
      return String(left.voice.name).localeCompare(String(right.voice.name));
    });
}

module.exports = {
  filterAndSortVoices,
  languageMatches,
  missingVoiceLanguages,
  reconcileVoiceSelection,
  voiceConfigurationChanged,
  voiceConfigurationGuard,
  voiceCompatibility,
  voicePreviewAvailability,
  voiceTier,
};
