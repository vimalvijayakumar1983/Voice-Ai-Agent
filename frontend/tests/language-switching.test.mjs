import test from 'node:test';
import assert from 'node:assert/strict';

import languageSwitching from '../src/components/language-switching.cjs';

const {
  languageSwitchingState,
  normalizeLanguageSwitching,
  switchingStatus,
  withValidSwitching,
} = languageSwitching;

function configuration(overrides = {}) {
  return {
    supported_languages: ['en'],
    language_switching_enabled: false,
    language_switching_mode: 'disabled',
    ...overrides,
  };
}

test('adding Tamil preserves the full language set and enables automatic switching', () => {
  const result = withValidSwitching(configuration({ supported_languages: ['en', 'ta'] }), 1);

  assert.deepEqual(result.supported_languages, ['en', 'ta']);
  assert.equal(result.language_switching_enabled, true);
  assert.equal(result.language_switching_mode, 'automatic');
});

test('Tamil multilingual templates use the same automatic switching defaults', () => {
  assert.deepEqual(languageSwitchingState(['ta', 'en']), {
    language_switching_enabled: true,
    language_switching_mode: 'automatic',
  });
});

test('stored Tamil multilingual switching remains enabled after normalization', () => {
  const result = normalizeLanguageSwitching(configuration({
    supported_languages: ['ta', 'en'],
    language_switching_enabled: true,
    language_switching_mode: 'automatic',
  }));

  assert.equal(result.language_switching_enabled, true);
  assert.equal(result.language_switching_mode, 'automatic');
  assert.match(switchingStatus(result), /Same-call switching will be published/);
});

test('single-language configurations cannot retain automatic switching', () => {
  const result = normalizeLanguageSwitching(configuration({
    supported_languages: ['ta'],
    language_switching_enabled: true,
    language_switching_mode: 'automatic',
  }));

  assert.equal(result.language_switching_enabled, false);
  assert.equal(result.language_switching_mode, 'disabled');
});
