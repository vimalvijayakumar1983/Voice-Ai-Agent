import test from 'node:test';
import assert from 'node:assert/strict';

import voiceLibrary from '../src/components/voice-library.cjs';

const {
  filterAndSortVoices,
  languageMatches,
  missingVoiceLanguages,
  reconcileVoiceSelection,
  voiceConfigurationChanged,
  voiceConfigurationGuard,
  voiceCompatibility,
  voicePreviewAvailability,
  voiceTier,
} = voiceLibrary;

function voice(overrides = {}) {
  return {
    id: 'aanya',
    name: 'Aanya',
    languages: ['en', 'hi'],
    accent: 'Indian',
    gender: 'female',
    age: 'young',
    use_cases: ['support'],
    synthesizer_model: 'waves_lightning_v3_1',
    unavailability_reason: null,
    source: 'catalog',
    ...overrides,
  };
}

test('compatibility requires every selected language, not only the primary language', () => {
  const englishOnly = voice({ languages: ['en'] });

  assert.deepEqual(missingVoiceLanguages(englishOnly, ['en', 'hi']), ['hi']);
  assert.deepEqual(voiceCompatibility(englishOnly, ['en', 'hi']), {
    status: 'incompatible',
    missingLanguages: ['hi'],
    reason: 'This voice does not cover every selected agent language.',
  });
  assert.equal(voiceCompatibility(voice(), ['en', 'hi']).status, 'compatible');
});

test('Tamil uses the same catalog-wide multilingual compatibility rules', () => {
  const tamilAndEnglish = voice({ id: 'maya', languages: ['ta', 'en'] });
  const tamilOnly = voice({ id: 'selvi', languages: ['ta'] });

  assert.equal(voiceCompatibility(tamilAndEnglish, ['en', 'ta']).status, 'compatible');
  assert.deepEqual(voiceCompatibility(tamilOnly, ['en', 'ta']), {
    status: 'incompatible',
    missingLanguages: ['en'],
    reason: 'This voice does not cover every selected agent language.',
  });
  assert.equal(reconcileVoiceSelection('maya', [tamilAndEnglish], ['en', 'ta']).voiceId, 'maya');
  assert.equal(reconcileVoiceSelection('selvi', [tamilOnly], ['en', 'ta']).voiceId, '');
});

test('language matching accepts normalized BCP-47 parent and regional tags', () => {
  assert.equal(languageMatches('en-US', 'en'), true);
  assert.equal(languageMatches('pt', 'pt-BR'), true);
  assert.equal(languageMatches('en', 'hi'), false);
});

test('unavailable and unknown voices are never described as compatible', () => {
  assert.equal(
    voiceCompatibility(voice({ synthesizer_model: null, unavailability_reason: 'Model pairing unverified' }), ['en']).status,
    'unavailable',
  );
  assert.equal(
    voiceCompatibility(voice({ languages: [] }), ['en']).status,
    'unknown',
  );
  assert.equal(
    voiceCompatibility(undefined, ['en']).status,
    'unknown',
  );
});

test('catalog filtering defaults to verified language intersection and searches metadata', () => {
  const catalog = [
    voice(),
    voice({ id: 'alba', name: 'Alba', languages: ['en', 'es'], accent: 'European' }),
    voice({ id: 'mystery', name: 'Mystery', languages: [], accent: null }),
  ];

  assert.deepEqual(
    filterAndSortVoices(catalog, { selectedLanguages: ['en', 'hi'], status: 'compatible' })
      .map(({ voice: item }) => item.id),
    ['aanya'],
  );
  assert.deepEqual(
    filterAndSortVoices(catalog, { selectedLanguages: ['en'], status: 'all', query: 'european' })
      .map(({ voice: item }) => item.id),
    ['alba'],
  );
});

test('catalog sorting places safe voices before unknown and incompatible entries', () => {
  const catalog = [
    voice({ id: 'unknown', name: 'Unknown', languages: [] }),
    voice({ id: 'missing', name: 'Missing', languages: ['en'] }),
    voice({ id: 'ready', name: 'Ready' }),
  ];

  assert.deepEqual(
    filterAndSortVoices(catalog, { selectedLanguages: ['en', 'hi'], status: 'all' })
      .map(({ voice: item }) => item.id),
    ['ready', 'unknown', 'missing'],
  );
});

test('changing the language set clears a previously selected incompatible voice', () => {
  const catalog = [voice({ id: 'alba', name: 'Alba', languages: ['en', 'es'] })];
  const result = reconcileVoiceSelection('alba', catalog, ['en', 'hi']);

  assert.equal(result.removed, true);
  assert.equal(result.voiceId, '');
  assert.equal(result.voice.name, 'Alba');
  assert.deepEqual(result.compatibility.missingLanguages, ['hi']);
});

test('voice tier distinguishes standard, pro, cloned, and unverified entries', () => {
  assert.equal(voiceTier(voice()), 'Provider-routed');
  assert.equal(voiceTier(voice({ voice_pool: 'standard' })), 'Standard');
  assert.equal(voiceTier(voice({ voice_pool: 'pro' })), 'Pro');
  assert.equal(voiceTier(voice({ synthesizer_model: 'waves_lightning_v3_1_pro' })), 'Pro');
  assert.equal(voiceTier(voice({ source: 'cloned' })), 'Cloned');
  assert.equal(voiceTier(voice({ synthesizer_model: null })), 'Unverified');
});

test('voice preview is enabled only for provider-verified standard or pro pools', () => {
  assert.equal(voicePreviewAvailability(voice({ voice_pool: 'standard' })).available, true);
  assert.equal(voicePreviewAvailability(voice({ voice_pool: 'pro' })).available, true);
  assert.equal(voicePreviewAvailability(voice({ voice_pool: 'unknown' })).available, false);
  assert.equal(voicePreviewAvailability(voice({ voice_pool: 'cloned', source: 'cloned' })).available, false);
  assert.equal(voicePreviewAvailability(voice({ voice_pool: 'standard', synthesizer_model: null })).available, false);
});

const storedConfiguration = {
  name: 'Receptionist',
  voice_id: 'aanya',
  language: 'en',
  supported_languages: ['en', 'hi'],
  language_switching_enabled: true,
  language_switching_mode: 'automatic',
};

test('an unchanged stored voice configuration may survive a catalog outage', () => {
  const reorderedButEquivalent = {
    ...storedConfiguration,
    name: 'Executive concierge',
    supported_languages: ['hi', 'en', 'en'],
  };

  assert.equal(voiceConfigurationChanged(storedConfiguration, reorderedButEquivalent), false);
  assert.deepEqual(
    voiceConfigurationGuard({
      editorMode: 'edit',
      catalogAvailable: false,
      original: storedConfiguration,
      current: reorderedButEquivalent,
    }),
    {
      allowed: true,
      changed: false,
      reason: 'Existing voice and language configuration will be preserved.',
    },
  );
});

test('voice or language mutations fail closed while the catalog is unavailable', () => {
  for (const current of [
    { ...storedConfiguration, voice_id: 'alba' },
    { ...storedConfiguration, supported_languages: ['en', 'hi', 'ml'] },
    { ...storedConfiguration, supported_languages: ['en', 'hi', 'ta'] },
    { ...storedConfiguration, language_switching_enabled: false, language_switching_mode: 'disabled' },
  ]) {
    const result = voiceConfigurationGuard({
      editorMode: 'edit',
      catalogAvailable: false,
      original: storedConfiguration,
      current,
    });
    assert.equal(result.allowed, false);
    assert.equal(result.changed, true);
  }

  assert.equal(
    voiceConfigurationGuard({
      editorMode: 'create',
      catalogAvailable: false,
      original: storedConfiguration,
      current: storedConfiguration,
    }).allowed,
    false,
  );
});
