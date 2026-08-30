export interface LanguageSwitchingConfiguration {
  supported_languages: string[];
  language_switching_enabled: boolean;
  language_switching_mode: 'disabled' | 'automatic';
}

export interface LanguageSwitchingState {
  language_switching_enabled: boolean;
  language_switching_mode: 'disabled' | 'automatic';
}

export function languageSwitchingState(
  supportedLanguages: string[],
  automatic?: boolean,
): LanguageSwitchingState;
export function normalizeLanguageSwitching<T extends LanguageSwitchingConfiguration>(values: T): T;
export function withValidSwitching<T extends LanguageSwitchingConfiguration>(
  values: T,
  previousLanguageCount: number,
): T;
export function switchingStatus(values: LanguageSwitchingConfiguration): string;
