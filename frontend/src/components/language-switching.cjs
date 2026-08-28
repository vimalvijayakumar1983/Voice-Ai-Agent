'use strict';

function languageSwitchingState(supportedLanguages, automatic = true) {
  const enabled = automatic && Array.isArray(supportedLanguages) && supportedLanguages.length > 1;
  return {
    language_switching_enabled: enabled,
    language_switching_mode: enabled ? 'automatic' : 'disabled',
  };
}

function normalizeLanguageSwitching(values) {
  const automatic = Boolean(
    values?.language_switching_enabled || values?.language_switching_mode === 'automatic',
  );
  return {
    ...values,
    ...languageSwitchingState(values?.supported_languages, automatic),
  };
}

function withValidSwitching(values, previousLanguageCount) {
  if (!Array.isArray(values?.supported_languages) || values.supported_languages.length < 2) {
    return { ...values, ...languageSwitchingState(values?.supported_languages, false) };
  }
  const justBecameMultilingual = previousLanguageCount < 2;
  const automatic = justBecameMultilingual || values.language_switching_mode === 'automatic';
  return { ...values, ...languageSwitchingState(values.supported_languages, automatic) };
}

function switchingStatus(values) {
  if (!Array.isArray(values?.supported_languages) || values.supported_languages.length < 2) {
    return 'Add at least one more language to enable automatic switching.';
  }
  if (values.language_switching_enabled) {
    return 'Same-call switching will be published. Validate this exact language combination in the Playground before activation.';
  }
  return 'Multiple languages are configured, but the agent will stay in the primary language during each call.';
}

module.exports = {
  languageSwitchingState,
  normalizeLanguageSwitching,
  switchingStatus,
  withValidSwitching,
};
