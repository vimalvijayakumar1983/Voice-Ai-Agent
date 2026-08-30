export type VoiceCompatibilityStatus = 'compatible' | 'incompatible' | 'unknown' | 'unavailable';

export interface VoiceCatalogLike {
  id: string;
  name: string;
  languages: string[];
  accent: string | null;
  gender: string | null;
  age: string | null;
  use_cases: string[];
  synthesizer_model: string | null;
  unavailability_reason: string | null;
  source: 'catalog' | 'cloned';
  voice_pool?: 'standard' | 'pro' | 'cloned' | 'unknown' | null;
  voice_tier?: 'standard' | 'pro' | 'cloned' | 'unknown' | null;
  tier?: 'standard' | 'pro' | 'unknown' | null;
}

export interface VoiceCompatibility {
  status: VoiceCompatibilityStatus;
  missingLanguages: string[];
  reason: string;
}

export interface VoiceConfigurationLike {
  voice_id: string;
  language: string;
  supported_languages: string[];
  language_switching_enabled: boolean;
  language_switching_mode: 'disabled' | 'automatic';
}

export function languageMatches(capability: string, requested: string): boolean;
export function missingVoiceLanguages(voice: VoiceCatalogLike, selectedLanguages: string[]): string[];
export function voiceCompatibility(
  voice: VoiceCatalogLike | undefined,
  selectedLanguages: string[],
): VoiceCompatibility;
export function voiceTier(voice: VoiceCatalogLike): 'Standard' | 'Pro' | 'Cloned' | 'Provider-routed' | 'Unverified';
export function voicePreviewAvailability(
  voice: VoiceCatalogLike | undefined,
): { available: boolean; reason: string | null };
export function reconcileVoiceSelection(
  voiceId: string,
  voices: VoiceCatalogLike[],
  selectedLanguages: string[],
): {
  voiceId: string;
  removed: boolean;
  voice: VoiceCatalogLike | undefined;
  compatibility: VoiceCompatibility | null;
};
export function voiceConfigurationChanged(
  original: VoiceConfigurationLike,
  current: VoiceConfigurationLike,
): boolean;
export function voiceConfigurationGuard(options: {
  editorMode: 'create' | 'edit';
  catalogAvailable: boolean;
  original: VoiceConfigurationLike;
  current: VoiceConfigurationLike;
}): { allowed: boolean; changed: boolean; reason: string | null };
export function filterAndSortVoices<T extends VoiceCatalogLike>(
  voices: T[],
  options: {
    selectedLanguages?: string[];
    query?: string;
    status?: VoiceCompatibilityStatus | 'all';
    tier?: 'standard' | 'pro' | 'cloned' | 'provider-routed' | 'unverified' | 'all';
    gender?: string;
    accent?: string;
  },
): Array<{ voice: T; compatibility: VoiceCompatibility }>;
