export function valuesEqual(left: unknown, right: unknown): boolean;
export function agentEditorPatch<T extends object>(
  original: T,
  current: T,
): Partial<T>;
export function agentUpdateNotice(name: string, syncStatus: string): string;
export function requiresSmallestDeprovision(
  agent: { provider_agent_id?: string | null; voice_provider?: string },
  patch: { voice_provider?: string },
): boolean;
