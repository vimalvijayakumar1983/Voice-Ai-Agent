import {
  Injectable,
  Logger,
  NotFoundException,
  BadRequestException,
} from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';

// ============================================================================
// Types
// ============================================================================

export enum ExperimentStatus {
  DRAFT = 'DRAFT',
  RUNNING = 'RUNNING',
  COMPLETED = 'COMPLETED',
  APPLIED = 'APPLIED',
}

export enum TrafficSplitMethod {
  PERCENTAGE = 'PERCENTAGE',
  ROUND_ROBIN = 'ROUND_ROBIN',
  STRATIFIED = 'STRATIFIED',
}

export interface Variant {
  id: string;
  name: string;
  promptText: string;
  trafficPercentage: number;
  isControl: boolean;
}

export interface VariantMetrics {
  variantId: string;
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
  status: ExperimentStatus;
  variants: Array<VariantMetrics & { name: string; isControl: boolean }>;
  statisticalSignificance: boolean;
  pValue: number;
  confidenceLevel: number;
  recommendedWinner: string | null;
  minimumSampleReached: boolean;
  totalSamples: number;
}

export interface Experiment {
  id: string;
  tenantId: string;
  name: string;
  description?: string;
  agentId: string;
  status: ExperimentStatus;
  trafficSplitMethod: TrafficSplitMethod;
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

export interface CreateExperimentDto {
  name: string;
  description?: string;
  agentId: string;
  trafficSplitMethod?: TrafficSplitMethod;
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

export interface UpdateExperimentDto {
  name?: string;
  description?: string;
  variants?: Array<{
    id?: string;
    name: string;
    promptText: string;
    trafficPercentage: number;
    isControl?: boolean;
  }>;
  trafficSplitMethod?: TrafficSplitMethod;
  successCriteria?: string;
  minimumSampleSize?: number;
  confidenceThreshold?: number;
}

// ============================================================================
// Service
// ============================================================================

@Injectable()
export class AbTestingService {
  private readonly logger = new Logger(AbTestingService.name);
  private readonly EXPERIMENT_PREFIX = 'ab:experiment:';
  private readonly METRICS_PREFIX = 'ab:metrics:';
  private readonly ASSIGNMENT_PREFIX = 'ab:assign:';
  private readonly INDEX_PREFIX = 'ab:index:';
  private readonly TTL = 86400 * 90; // 90 days

  constructor(
    private readonly prisma: PrismaService,
    private readonly redis: RedisService,
  ) {}

  // --------------------------------------------------------------------------
  // CRUD
  // --------------------------------------------------------------------------

  async create(
    tenantId: string,
    userId: string,
    dto: CreateExperimentDto,
  ): Promise<Experiment> {
    this.validateVariants(dto.variants);

    const id = `exp_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;
    const now = new Date().toISOString();

    const variants: Variant[] = dto.variants.map((v, i) => ({
      id: `var_${id}_${i}`,
      name: v.name,
      promptText: v.promptText,
      trafficPercentage: v.trafficPercentage,
      isControl: v.isControl ?? i === 0,
    }));

    const experiment: Experiment = {
      id,
      tenantId,
      name: dto.name,
      description: dto.description,
      agentId: dto.agentId,
      status: ExperimentStatus.DRAFT,
      trafficSplitMethod: dto.trafficSplitMethod || TrafficSplitMethod.PERCENTAGE,
      variants,
      successCriteria: dto.successCriteria || 'success_rate',
      minimumSampleSize: dto.minimumSampleSize || 100,
      confidenceThreshold: dto.confidenceThreshold || 0.95,
      createdBy: userId,
      createdAt: now,
      updatedAt: now,
    };

    await this.saveExperiment(experiment);

    // Initialize metrics
    for (const variant of variants) {
      await this.initializeVariantMetrics(experiment.id, variant.id);
    }

    this.logger.log(`Created experiment ${id} for tenant ${tenantId}`);
    return experiment;
  }

  async findAll(tenantId: string): Promise<Experiment[]> {
    const indexKey = `${this.INDEX_PREFIX}${tenantId}`;
    const ids = (await this.redis.getJson<string[]>(indexKey)) || [];

    const experiments: Experiment[] = [];
    for (const id of ids) {
      const exp = await this.redis.getJson<Experiment>(
        `${this.EXPERIMENT_PREFIX}${id}`,
      );
      if (exp && exp.tenantId === tenantId) {
        experiments.push(exp);
      }
    }

    return experiments.sort(
      (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime(),
    );
  }

  async findById(tenantId: string, id: string): Promise<Experiment> {
    const experiment = await this.redis.getJson<Experiment>(
      `${this.EXPERIMENT_PREFIX}${id}`,
    );
    if (!experiment || experiment.tenantId !== tenantId) {
      throw new NotFoundException(`Experiment ${id} not found`);
    }
    return experiment;
  }

  async update(
    tenantId: string,
    id: string,
    dto: UpdateExperimentDto,
  ): Promise<Experiment> {
    const experiment = await this.findById(tenantId, id);

    if (experiment.status === ExperimentStatus.RUNNING) {
      throw new BadRequestException('Cannot update a running experiment');
    }

    if (dto.name !== undefined) experiment.name = dto.name;
    if (dto.description !== undefined) experiment.description = dto.description;
    if (dto.trafficSplitMethod !== undefined) experiment.trafficSplitMethod = dto.trafficSplitMethod;
    if (dto.successCriteria !== undefined) experiment.successCriteria = dto.successCriteria;
    if (dto.minimumSampleSize !== undefined) experiment.minimumSampleSize = dto.minimumSampleSize;
    if (dto.confidenceThreshold !== undefined) experiment.confidenceThreshold = dto.confidenceThreshold;

    if (dto.variants) {
      this.validateVariants(dto.variants);
      experiment.variants = dto.variants.map((v, i) => ({
        id: v.id || `var_${id}_${i}`,
        name: v.name,
        promptText: v.promptText,
        trafficPercentage: v.trafficPercentage,
        isControl: v.isControl ?? i === 0,
      }));
    }

    experiment.updatedAt = new Date().toISOString();
    await this.saveExperiment(experiment);
    return experiment;
  }

  async delete(tenantId: string, id: string): Promise<void> {
    const experiment = await this.findById(tenantId, id);
    if (experiment.status === ExperimentStatus.RUNNING) {
      throw new BadRequestException('Cannot delete a running experiment. Stop it first.');
    }

    await this.redis.del(`${this.EXPERIMENT_PREFIX}${id}`);

    const indexKey = `${this.INDEX_PREFIX}${tenantId}`;
    const ids = (await this.redis.getJson<string[]>(indexKey)) || [];
    await this.redis.setJson(
      indexKey,
      ids.filter((i) => i !== id),
      this.TTL,
    );
  }

  // --------------------------------------------------------------------------
  // Experiment Lifecycle
  // --------------------------------------------------------------------------

  async startExperiment(tenantId: string, id: string): Promise<Experiment> {
    const experiment = await this.findById(tenantId, id);

    if (experiment.status !== ExperimentStatus.DRAFT) {
      throw new BadRequestException(
        `Cannot start experiment in ${experiment.status} status`,
      );
    }

    if (experiment.variants.length < 2) {
      throw new BadRequestException('Experiment must have at least 2 variants');
    }

    experiment.status = ExperimentStatus.RUNNING;
    experiment.startedAt = new Date().toISOString();
    experiment.updatedAt = new Date().toISOString();

    await this.saveExperiment(experiment);
    this.logger.log(`Started experiment ${id}`);
    return experiment;
  }

  async stopExperiment(tenantId: string, id: string): Promise<Experiment> {
    const experiment = await this.findById(tenantId, id);

    if (experiment.status !== ExperimentStatus.RUNNING) {
      throw new BadRequestException(
        `Cannot stop experiment in ${experiment.status} status`,
      );
    }

    experiment.status = ExperimentStatus.COMPLETED;
    experiment.completedAt = new Date().toISOString();
    experiment.updatedAt = new Date().toISOString();

    // Determine winner
    const results = await this.getResults(tenantId, id);
    if (results.recommendedWinner) {
      experiment.winnerId = results.recommendedWinner;
    }

    await this.saveExperiment(experiment);
    this.logger.log(`Stopped experiment ${id}, winner: ${experiment.winnerId || 'none'}`);
    return experiment;
  }

  async applyWinner(tenantId: string, id: string): Promise<Experiment> {
    const experiment = await this.findById(tenantId, id);

    if (experiment.status !== ExperimentStatus.COMPLETED) {
      throw new BadRequestException('Experiment must be completed before applying winner');
    }

    if (!experiment.winnerId) {
      throw new BadRequestException('No winner determined for this experiment');
    }

    const winnerVariant = experiment.variants.find((v) => v.id === experiment.winnerId);
    if (!winnerVariant) {
      throw new BadRequestException('Winner variant not found');
    }

    // Update the agent's system prompt with the winning variant
    await this.prisma.agent.update({
      where: { id: experiment.agentId },
      data: {
        systemPrompt: winnerVariant.promptText,
        updatedAt: new Date(),
      },
    });

    experiment.status = ExperimentStatus.APPLIED;
    experiment.updatedAt = new Date().toISOString();
    await this.saveExperiment(experiment);

    this.logger.log(
      `Applied winner variant "${winnerVariant.name}" to agent ${experiment.agentId}`,
    );
    return experiment;
  }

  // --------------------------------------------------------------------------
  // Traffic Assignment
  // --------------------------------------------------------------------------

  async assignVariant(
    experimentId: string,
    callId: string,
  ): Promise<Variant | null> {
    const experiment = await this.redis.getJson<Experiment>(
      `${this.EXPERIMENT_PREFIX}${experimentId}`,
    );
    if (!experiment || experiment.status !== ExperimentStatus.RUNNING) {
      return null;
    }

    // Check if already assigned
    const existingAssignment = await this.redis.get(
      `${this.ASSIGNMENT_PREFIX}${experimentId}:${callId}`,
    );
    if (existingAssignment) {
      return experiment.variants.find((v) => v.id === existingAssignment) || null;
    }

    let selectedVariant: Variant;

    switch (experiment.trafficSplitMethod) {
      case TrafficSplitMethod.ROUND_ROBIN: {
        const counterKey = `${this.EXPERIMENT_PREFIX}${experimentId}:counter`;
        const count = await this.redis.increment(counterKey);
        const index = (count - 1) % experiment.variants.length;
        selectedVariant = experiment.variants[index];
        break;
      }
      case TrafficSplitMethod.STRATIFIED:
      case TrafficSplitMethod.PERCENTAGE:
      default: {
        const random = Math.random() * 100;
        let cumulative = 0;
        selectedVariant = experiment.variants[experiment.variants.length - 1];
        for (const variant of experiment.variants) {
          cumulative += variant.trafficPercentage;
          if (random < cumulative) {
            selectedVariant = variant;
            break;
          }
        }
        break;
      }
    }

    // Store assignment
    await this.redis.set(
      `${this.ASSIGNMENT_PREFIX}${experimentId}:${callId}`,
      selectedVariant.id,
      this.TTL,
    );

    return selectedVariant;
  }

  // --------------------------------------------------------------------------
  // Metrics Recording
  // --------------------------------------------------------------------------

  async recordCallResult(
    experimentId: string,
    callId: string,
    metrics: {
      success: boolean;
      duration: number;
      sentiment: number;
      converted: boolean;
    },
  ): Promise<void> {
    const assignmentKey = `${this.ASSIGNMENT_PREFIX}${experimentId}:${callId}`;
    const variantId = await this.redis.get(assignmentKey);
    if (!variantId) return;

    const metricsKey = `${this.METRICS_PREFIX}${experimentId}:${variantId}`;
    const existing = (await this.redis.getJson<VariantMetrics>(metricsKey)) || {
      variantId,
      sampleSize: 0,
      successCount: 0,
      successRate: 0,
      avgDuration: 0,
      avgSentiment: 0,
      conversionCount: 0,
      conversionRate: 0,
      totalCalls: 0,
    };

    existing.totalCalls += 1;
    existing.sampleSize += 1;
    if (metrics.success) existing.successCount += 1;
    if (metrics.converted) existing.conversionCount += 1;

    // Running averages
    existing.avgDuration =
      (existing.avgDuration * (existing.sampleSize - 1) + metrics.duration) /
      existing.sampleSize;
    existing.avgSentiment =
      (existing.avgSentiment * (existing.sampleSize - 1) + metrics.sentiment) /
      existing.sampleSize;
    existing.successRate =
      existing.sampleSize > 0
        ? (existing.successCount / existing.sampleSize) * 100
        : 0;
    existing.conversionRate =
      existing.sampleSize > 0
        ? (existing.conversionCount / existing.sampleSize) * 100
        : 0;

    await this.redis.setJson(metricsKey, existing, this.TTL);

    // Check for auto-completion
    await this.checkAutoCompletion(experimentId);
  }

  // --------------------------------------------------------------------------
  // Results & Statistics
  // --------------------------------------------------------------------------

  async getResults(tenantId: string, id: string): Promise<ExperimentResults> {
    const experiment = await this.findById(tenantId, id);

    const variantResults: Array<VariantMetrics & { name: string; isControl: boolean }> = [];
    let totalSamples = 0;

    for (const variant of experiment.variants) {
      const metricsKey = `${this.METRICS_PREFIX}${id}:${variant.id}`;
      const metrics = (await this.redis.getJson<VariantMetrics>(metricsKey)) || {
        variantId: variant.id,
        sampleSize: 0,
        successCount: 0,
        successRate: 0,
        avgDuration: 0,
        avgSentiment: 0,
        conversionCount: 0,
        conversionRate: 0,
        totalCalls: 0,
      };

      totalSamples += metrics.sampleSize;
      variantResults.push({
        ...metrics,
        name: variant.name,
        isControl: variant.isControl,
      });
    }

    const minimumSampleReached = variantResults.every(
      (v) => v.sampleSize >= experiment.minimumSampleSize,
    );

    // Calculate statistical significance using chi-squared test
    const { pValue, significant } = this.chiSquaredTest(
      variantResults,
      experiment.confidenceThreshold,
      experiment.successCriteria,
    );

    // Determine winner
    let recommendedWinner: string | null = null;
    if (significant && minimumSampleReached) {
      const metricGetter = this.getMetricAccessor(experiment.successCriteria);
      const sorted = [...variantResults].sort(
        (a, b) => metricGetter(b) - metricGetter(a),
      );
      recommendedWinner = sorted[0].variantId;
    }

    return {
      experimentId: id,
      status: experiment.status,
      variants: variantResults,
      statisticalSignificance: significant,
      pValue,
      confidenceLevel: 1 - pValue,
      recommendedWinner,
      minimumSampleReached,
      totalSamples,
    };
  }

  // --------------------------------------------------------------------------
  // Statistical Tests
  // --------------------------------------------------------------------------

  private chiSquaredTest(
    variants: Array<VariantMetrics & { name: string; isControl: boolean }>,
    confidenceThreshold: number,
    criteriaField: string,
  ): { pValue: number; significant: boolean } {
    if (variants.length < 2) return { pValue: 1, significant: false };

    const total = variants.reduce((s, v) => s + v.sampleSize, 0);
    if (total === 0) return { pValue: 1, significant: false };

    const metricGetter = this.getMetricAccessor(criteriaField);

    // For success rate: use successes/failures as observed frequencies
    if (criteriaField === 'success_rate' || criteriaField === 'conversion_rate') {
      const successKey = criteriaField === 'success_rate' ? 'successCount' : 'conversionCount';
      const totalSuccesses = variants.reduce((s, v) => s + (v as any)[successKey], 0);
      const totalFailures = total - totalSuccesses;

      if (totalSuccesses === 0 || totalFailures === 0) {
        return { pValue: 1, significant: false };
      }

      let chiSquared = 0;

      for (const variant of variants) {
        const observed = (variant as any)[successKey];
        const observedFail = variant.sampleSize - observed;
        const expectedSuccess = (variant.sampleSize / total) * totalSuccesses;
        const expectedFail = (variant.sampleSize / total) * totalFailures;

        if (expectedSuccess > 0) {
          chiSquared += Math.pow(observed - expectedSuccess, 2) / expectedSuccess;
        }
        if (expectedFail > 0) {
          chiSquared += Math.pow(observedFail - expectedFail, 2) / expectedFail;
        }
      }

      const df = variants.length - 1;
      const pValue = this.chiSquaredPValue(chiSquared, df);

      return {
        pValue,
        significant: (1 - pValue) >= confidenceThreshold,
      };
    }

    // For continuous metrics, use a simplified comparison
    const values = variants.map((v) => ({
      mean: metricGetter(v),
      n: v.sampleSize,
    }));

    // Two-sample z-test approximation for the primary metric
    if (values.length === 2 && values[0].n > 0 && values[1].n > 0) {
      const diff = Math.abs(values[0].mean - values[1].mean);
      const pooledStdErr = Math.sqrt(
        (values[0].mean * (1 - values[0].mean / 100)) / values[0].n +
        (values[1].mean * (1 - values[1].mean / 100)) / values[1].n,
      );

      if (pooledStdErr > 0) {
        const z = diff / pooledStdErr;
        const pValue = 2 * (1 - this.normalCdf(z));
        return {
          pValue,
          significant: (1 - pValue) >= confidenceThreshold,
        };
      }
    }

    return { pValue: 1, significant: false };
  }

  /**
   * Approximate chi-squared p-value using Wilson-Hilferty approximation
   */
  private chiSquaredPValue(chiSq: number, df: number): number {
    if (df <= 0 || chiSq <= 0) return 1;

    // Wilson-Hilferty transformation to standard normal
    const z =
      Math.pow(chiSq / df, 1 / 3) -
      (1 - 2 / (9 * df));
    const denom = Math.sqrt(2 / (9 * df));

    if (denom === 0) return 1;

    const standardZ = z / denom;
    return 1 - this.normalCdf(standardZ);
  }

  /**
   * Standard normal CDF approximation (Abramowitz and Stegun)
   */
  private normalCdf(z: number): number {
    if (z < -8) return 0;
    if (z > 8) return 1;

    let sum = 0;
    let term = z;
    for (let i = 3; sum + term !== sum; i += 2) {
      sum += term;
      term = (term * z * z) / i;
    }

    return 0.5 + sum * this.normalPdf(z);
  }

  private normalPdf(z: number): number {
    return Math.exp(-0.5 * z * z) / Math.sqrt(2 * Math.PI);
  }

  // --------------------------------------------------------------------------
  // Helpers
  // --------------------------------------------------------------------------

  private getMetricAccessor(criteria: string): (v: VariantMetrics) => number {
    switch (criteria) {
      case 'success_rate':
        return (v) => v.successRate;
      case 'conversion_rate':
        return (v) => v.conversionRate;
      case 'avg_duration':
        return (v) => -v.avgDuration; // Lower is better
      case 'avg_sentiment':
        return (v) => v.avgSentiment;
      default:
        return (v) => v.successRate;
    }
  }

  private validateVariants(
    variants: Array<{ trafficPercentage: number; name: string }>,
  ): void {
    if (!variants || variants.length < 2) {
      throw new BadRequestException('At least 2 variants are required');
    }

    const totalPercentage = variants.reduce((s, v) => s + v.trafficPercentage, 0);
    if (Math.abs(totalPercentage - 100) > 0.1) {
      throw new BadRequestException(
        `Traffic percentages must sum to 100% (got ${totalPercentage}%)`,
      );
    }

    const names = new Set(variants.map((v) => v.name));
    if (names.size !== variants.length) {
      throw new BadRequestException('Variant names must be unique');
    }
  }

  private async saveExperiment(experiment: Experiment): Promise<void> {
    await this.redis.setJson(
      `${this.EXPERIMENT_PREFIX}${experiment.id}`,
      experiment,
      this.TTL,
    );

    // Update index
    const indexKey = `${this.INDEX_PREFIX}${experiment.tenantId}`;
    const ids = (await this.redis.getJson<string[]>(indexKey)) || [];
    if (!ids.includes(experiment.id)) {
      ids.unshift(experiment.id);
      await this.redis.setJson(indexKey, ids.slice(0, 500), this.TTL);
    }
  }

  private async initializeVariantMetrics(
    experimentId: string,
    variantId: string,
  ): Promise<void> {
    const metricsKey = `${this.METRICS_PREFIX}${experimentId}:${variantId}`;
    await this.redis.setJson<VariantMetrics>(
      metricsKey,
      {
        variantId,
        sampleSize: 0,
        successCount: 0,
        successRate: 0,
        avgDuration: 0,
        avgSentiment: 0,
        conversionCount: 0,
        conversionRate: 0,
        totalCalls: 0,
      },
      this.TTL,
    );
  }

  private async checkAutoCompletion(experimentId: string): Promise<void> {
    try {
      const experiment = await this.redis.getJson<Experiment>(
        `${this.EXPERIMENT_PREFIX}${experimentId}`,
      );
      if (!experiment || experiment.status !== ExperimentStatus.RUNNING) return;

      // Check if all variants have reached minimum sample size
      let allReached = true;
      for (const variant of experiment.variants) {
        const metricsKey = `${this.METRICS_PREFIX}${experimentId}:${variant.id}`;
        const metrics = await this.redis.getJson<VariantMetrics>(metricsKey);
        if (!metrics || metrics.sampleSize < experiment.minimumSampleSize) {
          allReached = false;
          break;
        }
      }

      if (!allReached) return;

      // All samples reached - check statistical significance
      const variantResults: Array<VariantMetrics & { name: string; isControl: boolean }> = [];
      for (const variant of experiment.variants) {
        const metricsKey = `${this.METRICS_PREFIX}${experimentId}:${variant.id}`;
        const metrics = await this.redis.getJson<VariantMetrics>(metricsKey);
        if (metrics) {
          variantResults.push({
            ...metrics,
            name: variant.name,
            isControl: variant.isControl,
          });
        }
      }

      const { significant } = this.chiSquaredTest(
        variantResults,
        experiment.confidenceThreshold,
        experiment.successCriteria,
      );

      if (significant) {
        this.logger.log(
          `Experiment ${experimentId} auto-completing: statistical significance reached`,
        );
        // Don't auto-stop, just log. Let the user decide to stop.
      }
    } catch (error) {
      this.logger.warn(`Auto-completion check failed for ${experimentId}: ${error}`);
    }
  }
}
