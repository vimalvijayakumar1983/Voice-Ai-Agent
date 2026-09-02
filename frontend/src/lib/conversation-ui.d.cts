export type ScalarVariables = Record<string, string | number | boolean>;

export interface SessionErrorGuidance {
  title: string;
  message: string;
}

export type TranscriptRole = 'assistant' | 'user';
export interface UiTranscriptTurn { id: number; role: TranscriptRole; text: string; language?: string }
export interface UiLiveTranscript { role: TranscriptRole; text: string; language?: string }
export interface UiTranscriptState { turns: UiTranscriptTurn[]; live: UiLiveTranscript | null }
export type UiTranscriptAction =
  | { type: 'clear' }
  | { type: 'clear_live' }
  | { type: 'delta'; role: TranscriptRole; text: string; language?: string }
  | { type: 'settled'; id: number; role: TranscriptRole; text: string; language?: string };

export function parseTestVariables(text: string): ScalarVariables;
export function reduceTranscriptState(state: UiTranscriptState, action: UiTranscriptAction): UiTranscriptState;
export function sessionErrorGuidance(error: unknown): SessionErrorGuidance;
export function valueFromRecord(record: Record<string, unknown> | unknown, keys: string[]): string;
export function transcriptLanguage(turn: Record<string, unknown>): string;
