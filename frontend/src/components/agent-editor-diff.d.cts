export function valuesEqual(left: unknown, right: unknown): boolean;
export function agentEditorPatch<T extends object>(
  original: T,
  current: T,
): Partial<T>;
export function agentUpdateNotice(name: string, syncStatus: string): string;
