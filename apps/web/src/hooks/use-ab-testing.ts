import { useApiQuery, useApiMutation } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

export interface Variant {
  id: string;
  name: string;
  promptText: string;
  trafficPercentage: number;
  isControl: boolean;
}

export interface Experiment {
  id: string;
  tenantId: string;
  name: string;
  description?: string;
  agentId: string;
  status: 'DRAFT' | 'RUNNING' | 'COMPLETED' | 'APPLIED';
  trafficSplitMethod: 'PERCENTAGE' | 'ROUND_ROBIN' | 'STRATIFIED';
  variants: Variant[];
  successCriteria: string;
  minimumSampleSize: number;
  confidenceThreshold: number;
  startedAt?: string;
  completedAt?: string;
  winnerId?: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
}

export interface VariantResult {
  variantId: string;
  name: string;
  isControl: boolean;
  sampleSize: number;
  successCount: number;
  successRate: number;
  avgDuration: number;
  avgSentiment: number;
  conversionCount: number;
  conversionRate: number;
  totalCalls: number;
}

export interface ExperimentResults {
  experimentId: string;
  status: string;
  variants: VariantResult[];
  statisticalSignificance: boolean;
  pValue: number;
  confidenceLevel: number;
  recommendedWinner: string | null;
  minimumSampleReached: boolean;
  totalSamples: number;
}

export interface CreateExperimentData {
  name: string;
  description?: string;
  agentId: string;
  trafficSplitMethod?: string;
  variants: Array<{
    name: string;
    promptText: string;
    trafficPercentage: number;
    isControl?: boolean;
  }>;
  successCriteria?: string;
  minimumSampleSize?: number;
  confidenceThreshold?: number;
}

// ============================================================================
// Hooks
// ============================================================================

export function useExperiments() {
  return useApiQuery<{ experiments: Experiment[] }>(
    ['ab-tests'],
    '/ab-tests',
  );
}

export function useExperiment(id: string) {
  return useApiQuery<Experiment>(
    ['ab-tests', id],
    `/ab-tests/${id}`,
    { enabled: !!id },
  );
}

export function useExperimentResults(id: string, enabled = true) {
  return useApiQuery<ExperimentResults>(
    ['ab-tests', id, 'results'],
    `/ab-tests/${id}/results`,
    {
      enabled: enabled && !!id,
      refetchInterval: enabled ? 10000 : false, // Poll every 10s when active
    },
  );
}

export function useCreateExperiment() {
  return useApiMutation<Experiment, CreateExperimentData>(
    '/ab-tests',
    'post',
    [['ab-tests']],
  );
}

export function useUpdateExperiment(id: string) {
  return useApiMutation<Experiment, Partial<CreateExperimentData>>(
    `/ab-tests/${id}`,
    'patch',
    [['ab-tests'], ['ab-tests', id]],
  );
}

export function useDeleteExperiment(id: string) {
  return useApiMutation<void, void>(
    `/ab-tests/${id}`,
    'delete',
    [['ab-tests']],
  );
}

export function useStartExperiment(id: string) {
  return useApiMutation<Experiment, void>(
    `/ab-tests/${id}/start`,
    'post',
    [['ab-tests'], ['ab-tests', id]],
  );
}

export function useStopExperiment(id: string) {
  return useApiMutation<Experiment, void>(
    `/ab-tests/${id}/stop`,
    'post',
    [['ab-tests'], ['ab-tests', id], ['ab-tests', id, 'results']],
  );
}

export function useApplyWinner(id: string) {
  return useApiMutation<Experiment, void>(
    `/ab-tests/${id}/apply-winner`,
    'post',
    [['ab-tests'], ['ab-tests', id]],
  );
}
