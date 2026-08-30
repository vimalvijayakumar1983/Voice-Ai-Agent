export interface AgentReadiness {
  voice_provider?: string;
  is_active: boolean;
  sync_status: string;
  provider_agent_id: string | null;
  provider_revision_id: string | null;
  last_synced_at: string | null;
  provider_config?: {
    publish?: {
      phase?: string;
    };
  } | null;
}

export interface RuntimeReadinessProfile {
  enabled: boolean;
  status: string;
  blockers?: string[];
}

export type TestReadyAgent<T extends AgentReadiness> = T & {
  is_active: true;
  provider_agent_id: string;
  provider_revision_id: string;
  last_synced_at: string;
};

export function isAgentCallReady<T extends AgentReadiness>(
  agent: T | null | undefined,
  runtimeProfile?: RuntimeReadinessProfile | null,
): boolean;

export function agentTestReadinessMessage(
  agent: AgentReadiness | null | undefined,
  runtimeProfile?: RuntimeReadinessProfile | null,
): string;

export function isProviderConfigCorrection(
  agent: AgentReadiness | null | undefined,
): boolean;

export function providerActionLabel(agent: AgentReadiness): string;

export function providerActionNotice(
  name: string,
  action: 'provision' | 'sync',
  status: string,
  reconciliationOnly?: boolean,
): { type: 'success' | 'info' | 'error'; text: string };
